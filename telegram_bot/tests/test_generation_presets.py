import copy
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from aiogram.methods import AnswerCallbackQuery, EditMessageText

from imager_config.loader import config
from src.application.generation_presets import (
    PROFILE_TYPES, list_generation_profiles, load_profile_defaults, seed_generation_presets,
)
from src.commands.image_generation import wizard
from src.commands.image_generation.image_generation_states import ImageGenerationStates as S
from src.models.image_build_params import CommonParam, ParamType
from src.utils.building_blocks.result import Result
from test_navigation_security import TelegramFixture


class PresetCollection:
    def __init__(self):
        self.rows = {}

    async def update_one(self, query, update, *, upsert):
        key = (query['type'], query['name'])
        self.rows.setdefault(key, {}).update(copy.deepcopy(update['$set']))
        return SimpleNamespace(matched_count=1)


class PresetSeedTests(unittest.IsolatedAsyncioTestCase):
    async def test_defaults_are_exact_and_seeding_is_idempotent_without_deleting_custom_values(self):
        recommended, profiles = load_profile_defaults()
        self.assertEqual(recommended, 'Средний')
        self.assertEqual([
            (profile['name'], *(profile['parameters'][kind.value]['value'] for kind in PROFILE_TYPES))
            for profile in profiles
        ], [('Большой', 90, 30, 40), ('Средний', 64, 30, 40), ('Маленький', 32, 30, 32)])
        collection = PresetCollection()
        collection.rows[('image_size', 'Свой')] = {'type': 'image_size', 'name': 'Свой', 'value': 42}
        self.assertEqual(await seed_generation_presets(collection), 3)
        initial = copy.deepcopy(collection.rows)
        self.assertEqual(await seed_generation_presets(collection), 3)
        self.assertEqual(collection.rows, initial)
        self.assertEqual(len(collection.rows), 10)


class GenerationPresetWizardTests(TelegramFixture):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        _, profiles = load_profile_defaults()
        self.rows = [CommonParam(**item) for profile in profiles for item in profile['parameters'].values()]

        async def get_parameters(kind):
            return Result.Success([row for row in self.rows if row.type == kind])

        self.parameters = SimpleNamespace(get_common_params=AsyncMock(side_effect=get_parameters))
        self.uow = SimpleNamespace(
            image_build_params_repository=self.parameters,
            image_group_repository=SimpleNamespace(get_groups=AsyncMock(return_value=Result.Success([
                SimpleNamespace(display_name='Животные', imager_name='animals'),
            ]))),
            user_repository=SimpleNamespace(get_generation_preferences=AsyncMock(return_value={
                'image_size': 12, 'noise_level': 1, 'inset_size': 20,
            })),
        )

    def last_view(self):
        return next(request for request in reversed(self.session.requests) if isinstance(request, EditMessageText))

    async def test_one_profile_click_sets_all_parameters_and_goes_directly_to_libraries(self):
        await wizard.choose_mode(self.callback('mode:presets'), self.state, self.uow)
        labels = [button.text for row in self.last_view().reply_markup.inline_keyboard for button in row]
        self.assertEqual(labels[:3], [
            'Большой', 'Средний', 'Маленький',
        ])
        await wizard.pick_preset(self.callback('pick:1'), self.state, self.uow)
        data = await self.state.get_data()
        self.assertEqual(tuple(data[field] for field in ('image_size', 'noise_level', 'inset_size')), (64, 30, 40))
        self.assertEqual(set(data['selected_presets']), {kind.value for kind in PROFILE_TYPES})
        self.assertTrue(all(item['name'] == 'Средний' for item in data['selected_presets'].values()))
        self.assertEqual(await self.state.get_state(), S.selecting_image_group.state)

    async def test_incomplete_profiles_are_hidden_and_changed_values_require_a_fresh_choice(self):
        self.rows.append(CommonParam(name='Неполный', type=ParamType.IMAGE_SIZE, value=72))
        self.assertEqual(len(await list_generation_profiles(self.parameters)), 3)
        await wizard.choose_mode(self.callback('mode:presets'), self.state, self.uow)
        self.rows = [row.model_copy(update={'value': 31})
                     if row.name == 'Средний' and row.type == ParamType.NOISE_LEVEL else row for row in self.rows]
        await wizard.pick_preset(self.callback('pick:1'), self.state, self.uow)
        self.uow.image_group_repository.get_groups.assert_not_awaited()
        self.assertEqual((await self.state.get_data())['selected_presets'], {})
        self.assertTrue(any(isinstance(request, AnswerCallbackQuery) and request.text == 'Пресет изменён. Обновите выбор.'
                            for request in self.session.requests))

    async def test_manual_recommendations_follow_current_medium_profile_not_old_user_preferences(self):
        recommended = {ParamType.IMAGE_SIZE: 74, ParamType.NOISE_LEVEL: 25, ParamType.INSET_SIZE: 48}
        self.rows = [row.model_copy(update={'value': recommended[row.type]}) if row.name == 'Средний' else row
                     for row in self.rows]
        for step, expected in enumerate((74, 25, 48)):
            await wizard.show_manual(self.callback('mode:manual'), self.state, self.uow, step)
            self.assertIn(f'Рекомендуемое значение: {expected}.', self.last_view().text)
        self.uow.user_repository.get_generation_preferences.assert_not_awaited()

    async def test_manual_fallback_is_64_30_40_when_medium_is_unavailable(self):
        self.rows = []
        self.assertEqual((config.generation.result_size, config.generation.noise_level,
                          config.generation.cell_size), (64, 30, 40))
        for step, expected in enumerate((64, 30, 40)):
            await wizard.show_manual(self.callback('mode:manual'), self.state, self.uow, step)
            self.assertIn(f'Рекомендуемое значение: {expected}.', self.last_view().text)

    async def test_back_from_libraries_returns_to_whole_profiles_and_legacy_buttons_refresh(self):
        await wizard.choose_mode(self.callback('mode:presets'), self.state, self.uow)
        await wizard.pick_preset(self.callback('pick:0'), self.state, self.uow)
        await wizard.back(self.callback('back'), self.state, self.uow)
        self.assertEqual(await self.state.get_state(), S.selecting_image_size.state)
        self.assertIn('Выберите пресет', self.last_view().text)
        await self.state.update_data(current_presets={'0': self.rows[0].model_dump(mode='json')})
        await wizard.pick_preset(self.callback('pick:0'), self.state, self.uow)
        self.assertIn('parameters', (await self.state.get_data())['current_presets']['0'])
