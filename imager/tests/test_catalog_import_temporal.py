import threading
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from temporalio import workflow
from temporalio.exceptions import ActivityError, ApplicationError, RetryState
from temporalio.worker.workflow_sandbox import SandboxedWorkflowRunner

from imager_contracts.catalog_import import make_import_submission
from src.application.catalog_import.errors import CatalogImportConflict, InvalidCatalogArchive
from src.presentation.temporal.import_activities import CatalogImportActivities
from src.presentation.temporal.import_workflows import ImportCatalogWorkflow
from test_catalog_import_repository import import_request, import_result


class TestCatalogImportActivity(unittest.TestCase):
    def setUp(self):
        self.repository, self.executor = Mock(), Mock()
        self.repository.claim_execution.return_value = {'already_completed': False, 'token': 'current', 'fence': 1}
        self.repository.complete.return_value = self.executor.execute.return_value = import_result()
        self.activities = CatalogImportActivities(self.repository, self.executor)
        self.policy = make_import_submission(import_request())['execution_policy']['import']
        self.heartbeat = patch('src.presentation.temporal.import_activities.activity.heartbeat').start()
        patch('src.presentation.temporal.import_activities.activity.is_cancelled', return_value=False).start()
        patch('src.presentation.temporal.import_activities.activity.info', return_value=SimpleNamespace(attempt=1)).start()
        self.addCleanup(patch.stopall)

    def tearDown(self):
        self.assertFalse([thread for thread in threading.enumerate()
                          if thread.name == 'imager-lease-heartbeat' and thread.is_alive()])

    def test_success_renews_and_completes_import_in_activity_thread(self):
        result = self.activities.import_catalog(import_request(), self.policy)
        self.assertEqual(result, import_result())
        self.repository.complete.assert_called_once_with('import_1', 'current', import_result())
        self.repository.heartbeat_execution.assert_called()
        self.heartbeat.assert_called()

    def test_completed_retry_does_not_download_or_decode_again(self):
        self.repository.claim_execution.return_value = {'already_completed': True, 'result': import_result(), 'token': None}
        self.assertEqual(self.activities.import_catalog(import_request(), self.policy), import_result())
        self.executor.execute.assert_not_called()

    def test_invalid_zip_fails_permanently_and_preserves_safe_reason(self):
        self.executor.execute.side_effect = InvalidCatalogArchive('ZIP содержит небезопасный путь.')
        with self.assertRaises(ApplicationError) as raised:
            self.activities.import_catalog(import_request(), self.policy)
        self.assertTrue(raised.exception.non_retryable)
        self.repository.fail.assert_called_once()
        self.repository.retry_execution.assert_not_called()

    def test_transient_after_claim_releases_for_retry(self):
        self.executor.execute.side_effect = OSError('MinIO unavailable')
        with self.assertRaises(ApplicationError) as raised:
            self.activities.import_catalog(import_request(), self.policy)
        self.assertFalse(raised.exception.non_retryable)
        self.repository.retry_execution.assert_called_once()
        self.repository.fail.assert_not_called()

    def test_transient_before_claim_does_not_finalize_queued_job(self):
        self.repository.claim_execution.side_effect = RuntimeError('Mongo unavailable')
        with self.assertRaises(ApplicationError) as raised:
            self.activities.import_catalog(import_request(), self.policy)
        self.assertFalse(raised.exception.non_retryable)
        self.repository.finalize_failure.assert_not_called()
        self.repository.get.assert_not_called()

    def test_permanent_preclaim_conflict_records_failure_for_matching_request(self):
        self.repository.claim_execution.side_effect = CatalogImportConflict('Библиотека уже существует.')
        self.repository.get.return_value = make_import_submission(import_request())
        with self.assertRaises(ApplicationError) as raised:
            self.activities.import_catalog(import_request(), self.policy)
        self.assertTrue(raised.exception.non_retryable)
        self.repository.finalize_failure.assert_called_once()

    def test_memory_budget_failure_is_not_repeated(self):
        self.executor.execute.side_effect = MemoryError('budget exceeded')
        with self.assertRaises(ApplicationError) as raised:
            self.activities.import_catalog(import_request(), self.policy)
        self.assertTrue(raised.exception.non_retryable)
        self.repository.fail.assert_called_once()


class TestCatalogImportWorkflow(unittest.IsolatedAsyncioTestCase):
    async def test_workflow_sandbox_imports_no_decoder_or_configuration(self):
        SandboxedWorkflowRunner().prepare_workflow(workflow._Definition.must_from_class(ImportCatalogWorkflow))

    async def test_activity_uses_pinned_policy_not_runtime_configuration(self):
        policy = make_import_submission(import_request())['execution_policy']
        policy['import']['maximum_attempts'] = 7
        execute = AsyncMock(return_value=import_result())
        with patch('src.presentation.temporal.import_workflows.workflow.execute_activity', execute):
            result = await ImportCatalogWorkflow().run(import_request(), policy)
        self.assertEqual(result, import_result())
        self.assertEqual(execute.await_args.args[0], 'import_catalog')
        self.assertEqual(execute.await_args.kwargs['args'], [import_request(), policy['import']])
        self.assertEqual(execute.await_args.kwargs['retry_policy'].maximum_attempts, 7)

    async def test_compensation_recovers_committed_catalog_after_lost_completion(self):
        error = ActivityError('import reply lost', scheduled_event_id=1, started_event_id=2,
                              identity='worker', activity_type='import_catalog', activity_id='1',
                              retry_state=RetryState.MAXIMUM_ATTEMPTS_REACHED)
        execute = AsyncMock(side_effect=[error, {'status': 'completed', 'result': import_result()}])
        with patch('src.presentation.temporal.import_workflows.workflow.execute_activity', execute):
            result = await ImportCatalogWorkflow().run(import_request(), make_import_submission(import_request())['execution_policy'])
        self.assertEqual(result, import_result())
        self.assertEqual(execute.await_args_list[1].args[0], 'record_import_failure')
