"""Offline tests for orchestration boundaries, without SDK/services at import."""
import ast
import asyncio
import time
import unittest
from dataclasses import asdict
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from imager_config.loader import config
from imager_contracts.workflow_versions import LEGACY_RENDER_POLICY_V1, legacy_execution_policy_v1


ROOT = Path(__file__).resolve().parents[2]


class AlreadyStarted(Exception):
    pass


class ActivityFailed(Exception):
    pass


def load(relative, namespace):
    path = ROOT / relative
    syntax = ast.parse(path.read_text(encoding='utf-8-sig'))
    syntax.body = [node for node in syntax.body if not isinstance(node, (ast.Import, ast.ImportFrom))]
    exec(compile(syntax, str(path), 'exec'), namespace)
    return namespace


def dispatcher():
    return load('src/presentation/temporal/dispatcher.py', {
        'asyncio': asyncio, 'time': time, 'config': config,
        'WorkflowAlreadyStartedError': AlreadyStarted,
        'WorkflowIDReusePolicy': SimpleNamespace(REJECT_DUPLICATE='reject-duplicate'),
        'settings': SimpleNamespace(temporal_task_queue='imager-render'),
        'presentation_logger': Mock(),
    })


def event():
    return {
        'job_id': 'job-1', 'dispatch_token': 'owner-1',
        'workflow_id': 'imager:job-1', 'execution_policy': policy(),
        'request': {'job_id': 'job-1', 'source': {'key': 'uploads/source.jpg'},
                    'parameters': {'cell_size': 40}, 'catalog_manifest': {'key': 'manifest.json'}},
    }


def policy():
    return {'render': asdict(config.temporal.render), 'failure': asdict(config.temporal.failure)}


class TestTemporalDispatch(unittest.IsolatedAsyncioTestCase):
    def repository(self):
        repository = Mock()
        repository.claim_dispatch.return_value = event()
        repository.mark_dispatched.return_value = True
        return repository

    async def test_ack_only_after_temporal_accepts_same_immutable_request(self):
        module = dispatcher()
        repository, client = self.repository(), SimpleNamespace(start_workflow=AsyncMock())
        self.assertTrue(await module['dispatch_once'](repository, client))
        repository.validate_dispatch.assert_called_once_with(event())
        repository.mark_dispatched.assert_called_once_with('job-1', 'owner-1', 'imager:job-1')
        self.assertEqual(client.start_workflow.call_args.kwargs['args'], [event()['request'], policy()])
        self.assertEqual(client.start_workflow.call_args.kwargs['id_reuse_policy'], 'reject-duplicate')
        repository.defer_dispatch.assert_not_called()

    async def test_lost_previous_start_ack_replays_as_already_started(self):
        module = dispatcher()
        repository = self.repository()
        client = SimpleNamespace(start_workflow=AsyncMock(side_effect=AlreadyStarted()))
        await module['dispatch_once'](repository, client)
        repository.mark_dispatched.assert_called_once()
        repository.defer_dispatch.assert_not_called()

    async def test_connection_failure_keeps_outbox_for_retry(self):
        module = dispatcher()
        repository = self.repository()
        client = SimpleNamespace(start_workflow=AsyncMock(side_effect=ConnectionError('offline')))
        await module['dispatch_once'](repository, client)
        repository.mark_dispatched.assert_not_called()
        repository.defer_dispatch.assert_called_once_with('job-1', 'owner-1', 'offline')

    async def test_invalid_inbox_rejected_before_temporal_start(self):
        module = dispatcher()
        repository = self.repository()
        repository.validate_dispatch.side_effect = ValueError('input changed')
        client = SimpleNamespace(start_workflow=AsyncMock())
        await module['dispatch_once'](repository, client)
        repository.reject_dispatch.assert_called_once_with('job-1', 'owner-1', 'input changed')
        client.start_workflow.assert_not_called()

    async def test_expired_dispatch_owner_is_not_logged_as_acknowledged(self):
        module = dispatcher()
        repository = self.repository()
        repository.mark_dispatched.return_value = False
        await module['dispatch_once'](repository, SimpleNamespace(start_workflow=AsyncMock()))
        module['presentation_logger'].warning.assert_called_once()

    async def test_terminal_workflow_is_reconciled_but_network_error_is_not_failure(self):
        module = dispatcher()
        repository = Mock()
        repository.unfinished_jobs.return_value = [
            {'job_id': 'job-1', 'workflow_id': 'imager:job-1'},
            {'job_id': 'job-2', 'workflow_id': 'imager:job-2'},
        ]
        handles = {
            'imager:job-1': SimpleNamespace(describe=AsyncMock(
                return_value=SimpleNamespace(status=SimpleNamespace(name='TERMINATED')))),
            'imager:job-2': SimpleNamespace(describe=AsyncMock(side_effect=ConnectionError('offline'))),
        }
        client = SimpleNamespace(get_workflow_handle=lambda key: handles[key])
        await module['reconcile_once'](repository, client)
        repository.finalize_failure.assert_called_once_with('job-1', 'TERMINATED')


class TestWorkflowFinalization(unittest.IsolatedAsyncioTestCase):
    def workflow(self, results):
        sdk = SimpleNamespace(
            defn=lambda value: value, run=lambda value: value,
            execute_activity=AsyncMock(side_effect=results),
        )
        module = load('src/presentation/temporal/workflows.py', {
            'workflow': sdk, 'ActivityError': ActivityFailed,
            'timedelta': timedelta, 'RetryPolicy': lambda **values: values,
            'legacy_execution_policy_v1': legacy_execution_policy_v1,
        })
        return sdk, module['GenerateImageWorkflow']()

    async def test_success_needs_no_failure_activity(self):
        sdk, workflow = self.workflow([{'key': 'result.jpg'}])
        self.assertEqual(await workflow.run({'job_id': 'job-1'}, policy()), {'key': 'result.jpg'})
        self.assertEqual(sdk.execute_activity.await_count, 1)

    async def test_timeout_records_final_failure_and_keeps_workflow_failed(self):
        sdk, workflow = self.workflow([ActivityFailed('timeout'), {'status': 'failed'}])
        with self.assertRaises(ActivityFailed):
            await workflow.run({'job_id': 'job-1'}, policy())
        self.assertEqual(sdk.execute_activity.call_args.args[0], 'record_job_failure')
        self.assertEqual(sdk.execute_activity.call_args.args[1]['job_id'], 'job-1')
        self.assertEqual(
            sdk.execute_activity.call_args.kwargs['schedule_to_close_timeout'], timedelta(minutes=5),
        )

    async def test_lost_completion_reply_preserves_published_success(self):
        sdk, workflow = self.workflow([
            ActivityFailed('reply lost'), {'status': 'completed', 'result': {'key': 'winner.jpg'}},
        ])
        self.assertEqual(await workflow.run({'job_id': 'job-1'}, policy()), {'key': 'winner.jpg'})

    async def test_saved_policy_controls_timeouts_and_retry_cutoff(self):
        sdk, workflow = self.workflow([{'key': 'result.jpg'}])
        saved = policy()
        saved['render']['start_to_close_seconds'] = 123.0
        saved['render']['maximum_attempts'] = 7
        await workflow.run({'job_id': 'job-1'}, saved)
        call = sdk.execute_activity.call_args
        self.assertEqual(call.kwargs['start_to_close_timeout'], timedelta(seconds=123))
        self.assertEqual(call.kwargs['retry_policy']['maximum_attempts'], 7)
        self.assertEqual(call.kwargs['args'], [{'job_id': 'job-1'}, saved['render']])

    async def test_legacy_one_argument_history_keeps_original_command_payload_and_timers(self):
        sdk, workflow = self.workflow([{'key': 'old-result.jpg'}])
        request = {'job_id': 'old-job'}
        await workflow.run(request)
        call = sdk.execute_activity.call_args
        self.assertEqual(call.args, ('render_image',))
        self.assertEqual(call.kwargs['arg'], request)
        self.assertNotIn('args', call.kwargs)
        self.assertEqual(call.kwargs['start_to_close_timeout'], timedelta(seconds=900))
        self.assertEqual(call.kwargs['schedule_to_close_timeout'], timedelta(seconds=86400))
        self.assertEqual(call.kwargs['heartbeat_timeout'], timedelta(seconds=30))
        retry = call.kwargs['retry_policy']
        self.assertEqual(retry['initial_interval'], timedelta(seconds=2))
        self.assertEqual(retry['maximum_interval'], timedelta(seconds=30))
        self.assertEqual(retry['maximum_attempts'], 3)

    async def test_legacy_failure_compensation_keeps_original_policy(self):
        sdk, workflow = self.workflow([ActivityFailed('old timeout'), {'status': 'failed'}])
        with self.assertRaises(ActivityFailed):
            await workflow.run({'job_id': 'old-job'})
        call = sdk.execute_activity.call_args
        self.assertEqual(call.args[0], 'record_job_failure')
        self.assertEqual(call.kwargs['start_to_close_timeout'], timedelta(seconds=30))
        self.assertEqual(call.kwargs['schedule_to_close_timeout'], timedelta(seconds=300))
        self.assertEqual(call.kwargs['retry_policy']['maximum_attempts'], 5)

    def test_legacy_policy_is_immutable_and_returned_copies_are_detached(self):
        with self.assertRaises(TypeError):
            LEGACY_RENDER_POLICY_V1['maximum_attempts'] = 100
        copy = legacy_execution_policy_v1()
        copy['render']['maximum_attempts'] = 100
        self.assertEqual(legacy_execution_policy_v1()['render']['maximum_attempts'], 3)
