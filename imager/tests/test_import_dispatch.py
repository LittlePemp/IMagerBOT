import asyncio
import copy
import hashlib
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from temporalio.exceptions import WorkflowAlreadyStartedError

from imager_config.loader import config
from imager_contracts import ObjectRef
from imager_contracts.catalog_import import CatalogImportRequest, import_fingerprint, make_import_submission
from src.presentation.temporal.dispatcher import dispatch_import_once, reconcile_import_once


def request():
    return CatalogImportRequest(
        import_id='import-1', actor_id=999, group_name='pack_abc', display_name='Новый пак',
        source=ObjectRef('imager', 'catalog/imports/input.zip', hashlib.sha256(b'zip').hexdigest(), 3, 'application/zip'),
    )


class ImportContractTests(unittest.TestCase):
    def test_round_trip_and_immutable_snapshot(self):
        original = request()
        document = make_import_submission(original)
        restored = CatalogImportRequest.from_dict(document['request'])
        self.assertEqual(original, restored)
        self.assertEqual(document['payload_hash'], import_fingerprint(restored))
        self.assertEqual(document['dispatch']['workflow_id'], config.pack_import.workflow_id_prefix + original.import_id)
        self.assertEqual(set(document['execution_policy']), {'import', 'failure'})
        changed = dict(document['request'], display_name='Другой пак')
        self.assertNotEqual(import_fingerprint(changed), document['payload_hash'])

    def test_invalid_names_actors_and_sources_fail_without_infrastructure(self):
        for field, value in (
            ('actor_id', True), ('actor_id', 0), ('group_name', '../outside'),
            ('display_name', ''), ('display_name', ' a '), ('display_name', '\x00a'),
            ('display_name', '\ud800'),
            ('schema_version', True),
        ):
            with self.subTest(field=field, value=repr(value)), self.assertRaises(ValueError):
                CatalogImportRequest.from_dict(dict(request().to_dict(), **{field: value}))


class ImportDispatchTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        document = make_import_submission(request())
        self.event = {
            'import_id': 'import-1', 'dispatch_token': 'lease', 'workflow_id': document['dispatch']['workflow_id'],
            'request': document['request'], 'execution_policy': document['execution_policy'],
        }
        self.repository = SimpleNamespace(
            claim_dispatch=Mock(return_value=self.event), validate_dispatch=Mock(),
            mark_dispatched=Mock(return_value=True), reject_dispatch=Mock(), defer_dispatch=Mock(),
            unfinished_jobs=Mock(return_value=[{'import_id': 'import-1', 'workflow_id': 'imager:import:import-1'}]),
            finalize_failure=Mock(),
        )
        self.client = SimpleNamespace(start_workflow=AsyncMock())

    async def test_import_uses_its_dedicated_queue_and_small_memo(self):
        self.assertTrue(await dispatch_import_once(self.repository, self.client))
        call = self.client.start_workflow.await_args
        self.assertEqual(call.args[0], 'ImportCatalogWorkflow')
        self.assertEqual(call.kwargs['task_queue'], config.pack_import.task_queue)
        self.assertNotEqual(call.kwargs['task_queue'], config.temporal.task_queue)
        self.assertEqual(call.kwargs['args'], [self.event['request'], self.event['execution_policy']])
        self.assertEqual(call.kwargs['memo']['source'], self.event['request']['source'])
        self.repository.mark_dispatched.assert_called_once()

    async def test_lost_start_ack_reuses_existing_workflow(self):
        self.client.start_workflow.side_effect = WorkflowAlreadyStartedError('id', 'ImportCatalogWorkflow')
        await dispatch_import_once(self.repository, self.client)
        self.repository.mark_dispatched.assert_called_once()
        self.repository.defer_dispatch.assert_not_called()

    async def test_invalid_inbox_never_reaches_temporal(self):
        self.repository.validate_dispatch.side_effect = ValueError('invalid')
        await dispatch_import_once(self.repository, self.client)
        self.client.start_workflow.assert_not_awaited()
        self.repository.reject_dispatch.assert_called_once()

    async def test_connection_failure_is_deferred_not_marked_started(self):
        self.client.start_workflow.side_effect = OSError('offline')
        await dispatch_import_once(self.repository, self.client)
        self.repository.defer_dispatch.assert_called_once()
        self.repository.mark_dispatched.assert_not_called()

    async def test_terminated_workflow_fences_the_import(self):
        self.client.get_workflow_handle = Mock(return_value=SimpleNamespace(
            describe=AsyncMock(return_value=SimpleNamespace(status=SimpleNamespace(name='TERMINATED'))),
        ))
        await reconcile_import_once(self.repository, self.client)
        self.repository.finalize_failure.assert_called_once_with('import-1', 'TERMINATED')
