import asyncio
import copy
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from bson import ObjectId
from pydantic import ValidationError

from imager_config.loader import config
from src.models.image_build_params import CommonParam, ParamType
from src.models.image_group import GroupStatus
from src.infrastructure.data.repositories.image_build_repository import ImageBuildParamsRepository
from src.infrastructure.data.repositories.image_group_repository import ImageGroupRepository


def matches(document, query):
    for key, value in query.items():
        if key == '$or':
            if not any(matches(document, alternative) for alternative in value):
                return False
        elif isinstance(value, dict):
            for operator, operand in value.items():
                actual = document.get(key)
                if operator == '$nin' and actual in operand:
                    return False
                if operator == '$ne' and actual == operand:
                    return False
                if operator == '$gt' and not actual > operand:
                    return False
                if operator == '$exists' and (key in document) != operand:
                    return False
        elif document.get(key) != value:
            return False
    return True


class Cursor:
    def __init__(self, rows):
        self.rows = rows

    def sort(self, key, direction):
        self.rows.sort(key=lambda row: row.get(key, ''))
        return self

    async def to_list(self, length=None):
        return copy.deepcopy(self.rows)


class Collection:
    def __init__(self, rows=()):
        self.rows = copy.deepcopy(list(rows))

    def find(self, query):
        return Cursor([row for row in self.rows if matches(row, query)])

    async def find_one(self, query):
        return next((copy.deepcopy(row) for row in self.rows if matches(row, query)), None)

    async def update_one(self, query, update, upsert=False):
        row = next((row for row in self.rows if matches(row, query)), None)
        if row is None and upsert:
            row = dict(query, **update.get('$setOnInsert', {}))
            self.rows.append(row)
        if row is not None:
            row.update(copy.deepcopy(update.get('$set', {})))
            for key, delta in update.get('$inc', {}).items():
                row[key] = row.get(key, 0) + delta
        return SimpleNamespace(matched_count=int(row is not None))

    async def update_many(self, query, update):
        for row in self.rows:
            if matches(row, query):
                row.update(copy.deepcopy(update['$set']))

    async def delete_one(self, query):
        row = next((row for row in self.rows if matches(row, query)), None)
        if row is not None:
            self.rows.remove(row)
        return SimpleNamespace(deleted_count=int(row is not None))


def group(name='catalog', status=GroupStatus.ACTIVE):
    return {
        'imager_name': name, 'display_name': name, 'count': 2, 'status': status,
        'available_in_core': True, 'last_synced': datetime.now(timezone.utc),
    }


class PresetValidationTests(unittest.TestCase):
    def test_non_finite_fractional_boolean_and_enormous_values_are_rejected(self):
        for value in ('NaN', 'Infinity', '-Infinity', '1.5', True, '1e9999999', '-1'):
            with self.subTest(value=value), self.assertRaises(ValidationError):
                CommonParam(name='x', type=ParamType.IMAGE_SIZE, value=value)

    def test_each_parameter_uses_its_bounds(self):
        for kind, value in ((ParamType.IMAGE_SIZE, 0), (ParamType.INSET_SIZE, config.bot.cell_size_max + 1),
                            (ParamType.NOISE_LEVEL, config.bot.noise_max + 1)):
            with self.subTest(kind=kind), self.assertRaises(ValidationError):
                CommonParam(name='x', type=kind, value=value)

    def test_legacy_integral_float_is_compatible_and_normalized(self):
        preset = CommonParam(name='  Размер  ', type=ParamType.IMAGE_SIZE, value=40.0)
        self.assertEqual((preset.name, preset.value), ('Размер', 40))
        self.assertIs(type(preset.value), int)

    def test_blank_control_and_oversized_names_are_rejected(self):
        for name in (' ', 'a\nb', 'a' * (config.bot.ui_name_max_length + 1)):
            with self.subTest(name=name), self.assertRaises(ValidationError):
                CommonParam(name=name, type=ParamType.IMAGE_SIZE, value=40)


class RepositoryScenarioTests(unittest.IsolatedAsyncioTestCase):
    async def test_invalid_legacy_presets_do_not_break_user_options_and_can_be_deleted(self):
        bad_id = ObjectId()
        repository = ImageBuildParamsRepository(Collection([
            {'_id': ObjectId(), 'name': 'good', 'type': 'image_size', 'value': 40.0},
            {'_id': bad_id, 'name': 'bad', 'type': 'image_size', 'value': float('nan')},
        ]))
        available = await repository.get_common_params(ParamType.IMAGE_SIZE)
        self.assertEqual([row.name for row in available.value], ['good'])
        self.assertEqual(len(await repository.get_admin_params(ParamType.IMAGE_SIZE)), 2)
        self.assertTrue(await repository.delete_by_id(str(bad_id), ParamType.IMAGE_SIZE))

    async def test_disabled_group_remains_manageable_and_sync_preserves_manual_status(self):
        repository = ImageGroupRepository(Collection([group(status=GroupStatus.INACTIVE)]))
        await repository.sync_groups([SimpleNamespace(group='catalog', count=3)])
        self.assertEqual((await repository.get_groups()).value, [])
        managed = (await repository.get_groups(status=None)).value
        self.assertEqual(len(managed), 1)
        self.assertEqual(managed[0].status, GroupStatus.INACTIVE)

    async def test_temporary_catalog_absence_does_not_disable_admin_choice(self):
        repository = ImageGroupRepository(Collection([group()]))
        await repository.sync_groups([])
        missing = await repository.get_group_by_name('catalog')
        self.assertFalse(missing.active)
        self.assertEqual(missing.status, GroupStatus.ACTIVE)
        await repository.sync_groups([SimpleNamespace(group='catalog', count=4)])
        self.assertTrue((await repository.get_group_by_name('catalog')).active)

    async def test_two_admin_edits_of_same_revision_do_not_overwrite_each_other(self):
        repository = ImageGroupRepository(Collection([group()]))
        results = await asyncio.gather(
            repository.update_group_name('catalog', 'First', expected_revision=0),
            repository.update_group_name('catalog', 'Second', expected_revision=0),
            return_exceptions=True,
        )
        self.assertEqual(sum(isinstance(result, ValueError) for result in results), 1)
        self.assertEqual((await repository.get_group_by_name('catalog')).revision, 1)

    async def test_legacy_empty_label_remains_repairable(self):
        row = group()
        row['display_name'] = ''
        repository = ImageGroupRepository(Collection([row]))
        self.assertEqual((await repository.get_groups(status=None)).value[0].display_name, 'catalog')
        await repository.update_group_name('catalog', 'Исправлено', expected_revision=0)
        self.assertEqual((await repository.get_group_by_name('catalog')).display_name, 'Исправлено')


class AdminActionTests(unittest.IsolatedAsyncioTestCase):
    async def test_rendering_next_screen_invalidates_real_previous_admin_callback(self):
        from src.commands.admin_panel import ui

        class State:
            def __init__(self):
                self.data = {}

            async def update_data(self, **values):
                self.data.update(values)

            async def get_data(self):
                return copy.deepcopy(self.data)

            async def set_state(self, value):
                pass

        state = State()
        message = SimpleNamespace(chat=SimpleNamespace(id=1), message_id=9)
        event = SimpleNamespace(from_user=SimpleNamespace(id=1), answer=AsyncMock(return_value=message))
        await ui.show(event, state, 'A', [('Назначить', 'grant', 0)], records={'0': {'user_id': 111, 'revision': 0}})
        first_keyboard = event.answer.call_args.kwargs['reply_markup']
        old_data = first_keyboard.inline_keyboard[0][0].callback_data
        home = first_keyboard.inline_keyboard[-1][0].callback_data
        self.assertTrue(home.startswith('n:'))
        self.assertEqual(state.data['_current_view']['records']['-1'], 'main_menu')
        await ui.show(event, state, 'B', [('Назначить', 'grant', 0)], records={'0': {'user_id': 222, 'revision': 0}})
        callback = SimpleNamespace(data=old_data, from_user=event.from_user, message=message, answer=AsyncMock())
        self.assertIsNone(await ui.command(callback, state))
        callback.answer.assert_awaited_once()

    async def test_revoke_uses_bound_target_and_explicit_action_not_old_toggle_flags(self):
        from src.commands.admin_panel.ui import AdminCommand
        from src.commands.admin_panel.user_management.user_actions import make_remove_admin_handler as handler
        action = AdminCommand('revoke', 0, {'user_id': 111, 'revision': 7}, {})
        repository = SimpleNamespace(change_access=AsyncMock(return_value=SimpleNamespace()))
        state = SimpleNamespace(get_data=AsyncMock(return_value={'user_id': 222, 'is_admin': False}))
        callback = SimpleNamespace(from_user=SimpleNamespace(id=1))
        with patch.object(handler, 'command', AsyncMock(return_value=action)), \
                patch.object(handler, 'render_user', AsyncMock()), \
                patch.object(handler.settings, 'telegram_admin_ids', (1,), create=True):
            await handler.make_remove_admin(callback, state, repository)
        call = repository.change_access.call_args
        self.assertEqual(call.args, (1, 111))
        self.assertEqual(call.kwargs['status'].value, 'user')
        self.assertEqual(call.kwargs['expected_revision'], 7)

    async def test_stale_callback_never_calls_access_repository(self):
        from src.commands.admin_panel.user_management.user_actions import block_unblock_handler as handler
        repository = SimpleNamespace(change_access=AsyncMock())
        with patch.object(handler, 'command', AsyncMock(return_value=None)):
            await handler.block_unblock_user(SimpleNamespace(), SimpleNamespace(), repository)
        repository.change_access.assert_not_called()

    async def test_invalidated_view_does_not_read_new_screen_records(self):
        from src.commands.admin_panel import ui
        callback = SimpleNamespace(data=ui.AdminCallback(view='old', action='grant', item=0).pack())
        state = SimpleNamespace(get_data=AsyncMock())
        with patch.object(ui, 'require_current', AsyncMock(return_value=False)):
            self.assertIsNone(await ui.command(callback, state))
        state.get_data.assert_not_awaited()

    async def test_current_nonce_cannot_turn_home_slot_into_user_mutation(self):
        from src.commands.admin_panel import ui
        callback = SimpleNamespace(data=ui.AdminCallback(view='current', action='grant', item=-1).pack(), answer=AsyncMock())
        state = SimpleNamespace(get_data=AsyncMock(return_value={
            '_current_view': {'records': {'-1': 'main_menu'}},
        }))
        with patch.object(ui, 'require_current', AsyncMock(return_value=True)):
            self.assertIsNone(await ui.command(callback, state))

    async def test_callback_size_is_independent_of_catalog_or_preset_name(self):
        from src.commands.admin_panel.ui import AdminCallback
        payload = AdminCallback(view='a' * (config.bot.ui_view_token_bytes * 2), action='group_show', item=9999).pack()
        self.assertLessEqual(len(payload.encode()), 64)

    async def test_dynamic_admin_is_checked_again_before_mutation(self):
        from src.commands.admin_panel import ui
        repository = SimpleNamespace(get_user_by_id=AsyncMock(return_value=SimpleNamespace(is_admin=False, isbanned=False)))
        with patch.object(ui.settings, 'telegram_admin_ids', (1,), create=True), self.assertRaises(ValueError):
            await ui.authorize_change(SimpleNamespace(from_user=SimpleNamespace(id=2)), repository)

    async def test_every_registered_admin_route_has_permission_filter(self):
        from aiogram import Dispatcher
        from src.commands.admin_panel.admin_panel_handler import register_handlers_admin_panel
        from src.commands.admin_panel.image_build_params.image_build_params_handler import register_handlers_image_build_params
        from src.commands.admin_panel.user_management import register_handlers_user_management_total
        from src.utils.filters.request_filters.admin_filter import AdminFilter

        dispatcher = Dispatcher()
        register_handlers_admin_panel(dispatcher)
        register_handlers_image_build_params(dispatcher, SimpleNamespace())
        register_handlers_user_management_total(dispatcher)
        handlers = dispatcher.message.handlers + dispatcher.callback_query.handlers
        self.assertGreater(len(handlers), 20)
        for handler in handlers:
            with self.subTest(handler=handler.callback):
                self.assertTrue(any(isinstance(item.callback, AdminFilter) for item in handler.filters))
        await dispatcher.storage.close()
