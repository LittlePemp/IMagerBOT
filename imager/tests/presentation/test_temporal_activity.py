import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from temporalio.exceptions import ApplicationError
from src.infrastructure.jobs.mongo_jobs import JobBusy, LostExecution
from src.presentation.temporal.activities import LeaseHeartbeat, RenderActivities


def request():
    return {
        'job_id': 'job_1',
        'source': {'bucket': 'imager', 'key': 'uploads/job_1/source.jpg',
                   'sha256': 'a' * 64, 'size_bytes': 100, 'content_type': 'image/jpeg'},
        'catalog_manifest': {'bucket': 'imager', 'key': 'catalog/manifests/cats/rev.json',
                             'sha256': 'b' * 64, 'size_bytes': 200, 'content_type': 'application/json'},
        'parameters': {'group_name': 'cats', 'insertion_format': 'crop', 'alpha_channel': 30,
                       'noise_level': 15, 'cell_size': 40, 'result_size': 120},
        'seed': 42,
    }


class TransientStorageError(RuntimeError):
    retryable = True


class TestTemporalActivity(unittest.TestCase):
    def setUp(self):
        self.repository = Mock()
        self.reference = {'bucket': 'imager', 'key': 'jobs/job_1/attempts/token/result.jpg',
                          'sha256': 'c' * 64, 'size_bytes': 1000, 'content_type': 'image/jpeg'}
        self.repository.claim_execution.return_value = {
            'already_completed': False, 'token': 'token', 'fence': 1,
        }
        self.repository.complete.return_value = self.reference
        self.executor = Mock()
        self.executor.execute.return_value = self.reference
        self.activities = RenderActivities(self.repository, self.executor)
        self.sdk_heartbeat = patch('src.presentation.temporal.activities.activity.heartbeat').start()
        self.sdk_cancelled = patch(
            'src.presentation.temporal.activities.activity.is_cancelled', return_value=False,
        ).start()
        self.sdk_info = patch(
            'src.presentation.temporal.activities.activity.info', return_value=SimpleNamespace(attempt=1),
        ).start()
        self.addCleanup(patch.stopall)

    def assert_no_heartbeat_threads(self):
        self.assertFalse([
            thread for thread in threading.enumerate()
            if thread.name == 'imager-lease-heartbeat' and thread.is_alive()
        ])

    def test_completed_retry_returns_previous_reference_without_rendering(self):
        self.repository.claim_execution.return_value = {
            'already_completed': True, 'result': self.reference, 'token': None,
        }
        actual = self.activities.render_image(request())
        self.assertEqual(actual, self.reference)
        self.executor.execute.assert_not_called()
        self.repository.complete.assert_not_called()
        self.repository.heartbeat_execution.assert_not_called()

    def test_success_claims_heartbeats_completes_and_stops_thread(self):
        progress = {'stage': 'rendering', 'completed_cells': 10, 'total_cells': 20}

        def execute(snapshot, token, callback):
            self.assertEqual(snapshot['seed'], 42)
            self.assertEqual(token, 'token')
            callback(progress)
            return self.reference

        self.executor.execute.side_effect = execute
        actual = self.activities.render_image(request())
        self.assertEqual(actual, self.reference)
        self.repository.complete.assert_called_once_with('job_1', 'token', self.reference)
        self.repository.heartbeat_execution.assert_called()
        self.sdk_heartbeat.assert_called()
        self.repository.retry_execution.assert_not_called()
        self.repository.fail.assert_not_called()
        self.assert_no_heartbeat_threads()

    def test_permanent_render_error_marks_failed_without_retry(self):
        self.executor.execute.side_effect = ValueError('unsupported image')
        with self.assertRaises(ApplicationError) as failed:
            self.activities.render_image(request())
        self.assertTrue(failed.exception.non_retryable)
        self.assertEqual(failed.exception.type, 'RenderRejected')
        self.repository.fail.assert_called_once()
        self.repository.retry_execution.assert_not_called()
        self.repository.complete.assert_not_called()
        self.assert_no_heartbeat_threads()

    def test_transient_render_error_releases_for_retry_and_stops_thread(self):
        self.executor.execute.side_effect = TransientStorageError('MinIO unavailable')
        with self.assertRaises(ApplicationError) as failed:
            self.activities.render_image(request())
        self.assertFalse(failed.exception.non_retryable)
        self.assertEqual(failed.exception.type, 'TransientRenderError')
        self.repository.retry_execution.assert_called_once()
        self.repository.fail.assert_not_called()
        self.assert_no_heartbeat_threads()

    def test_final_transient_attempt_marks_failed_instead_of_leaving_running(self):
        self.sdk_info.return_value = SimpleNamespace(attempt=3)
        self.executor.execute.side_effect = TransientStorageError('still unavailable')
        with self.assertRaises(ApplicationError):
            self.activities.render_image(request())
        self.repository.fail.assert_called_once()
        self.repository.retry_execution.assert_not_called()
        self.assert_no_heartbeat_threads()

    def test_lost_completion_ownership_is_not_success(self):
        self.repository.complete.side_effect = LostExecution('another attempt won')
        with self.assertRaises(ApplicationError) as failed:
            self.activities.render_image(request())
        self.assertFalse(failed.exception.non_retryable)
        self.repository.complete.assert_called_once()
        self.repository.retry_execution.assert_called_once()
        self.assert_no_heartbeat_threads()

    def test_invalid_wire_request_does_not_claim_or_render(self):
        malformed = []
        for key, value in (('job_id', '../outside'), ('parameters', None), ('source', None)):
            invalid = request()
            invalid[key] = value
            malformed.append(invalid)
        missing_seed = request()
        del missing_seed['seed']
        malformed.extend((missing_seed, {}, {'unknown': 'field'}))
        for invalid in malformed:
            with self.subTest(invalid=invalid), self.assertRaises(ApplicationError) as failed:
                self.activities.render_image(invalid)
            self.assertTrue(failed.exception.non_retryable)
        self.repository.claim_execution.assert_not_called()
        self.executor.execute.assert_not_called()

    def test_cancellation_detected_by_progress_prevents_completion(self):
        self.sdk_cancelled.side_effect = [False, True]

        def execute(snapshot, token, callback):
            callback({'stage': 'rendering'})
            return self.reference

        self.executor.execute.side_effect = execute
        with self.assertRaises(ApplicationError):
            self.activities.render_image(request())
        self.repository.complete.assert_not_called()
        self.assert_no_heartbeat_threads()

    def test_busy_lease_waits_with_heartbeats_then_renders_once(self):
        claim = self.repository.claim_execution.return_value
        self.repository.claim_execution.side_effect = [JobBusy('owned by previous attempt'), claim]
        with patch('src.presentation.temporal.activities.time.sleep') as sleep:
            actual = self.activities.render_image(request())
        self.assertEqual(actual, self.reference)
        self.assertEqual(self.repository.claim_execution.call_count, 2)
        sleep.assert_called_once_with(2)
        self.sdk_heartbeat.assert_any_call({'job_id': 'job_1', 'stage': 'waiting_for_execution_lease'})
        self.executor.execute.assert_called_once()
        self.repository.retry_execution.assert_not_called()
        self.assert_no_heartbeat_threads()

    def test_initial_heartbeat_failure_does_not_start_a_thread(self):
        self.repository.heartbeat_execution.side_effect = LostExecution('claim lost before render')
        with self.assertRaises(ApplicationError):
            self.activities.render_image(request())
        self.executor.execute.assert_not_called()
        self.repository.retry_execution.assert_called_once()
        self.assert_no_heartbeat_threads()

    def test_background_heartbeat_failure_is_visible_and_thread_is_joined(self):
        self.repository.heartbeat_execution.side_effect = [None, LostExecution('lease replaced')]
        heartbeat = LeaseHeartbeat(self.repository, 'job_1', 'token', interval=0.001)
        with heartbeat:
            deadline = time.monotonic() + 1
            while heartbeat.failure is None and time.monotonic() < deadline:
                time.sleep(0.001)
            self.assertIsNotNone(heartbeat.failure)
            with self.assertRaises(LostExecution):
                heartbeat.check()
        self.assertFalse(heartbeat.thread.is_alive())

    def test_workflow_compensation_adapter_delegates_canonical_result_decision(self):
        self.repository.finalize_failure.return_value = {'status': 'completed', 'result': self.reference}
        actual = self.activities.record_job_failure({'job_id': 'job_1', 'error': 'reply lost'})
        self.assertEqual(actual, {'status': 'completed', 'result': self.reference})
        self.repository.finalize_failure.assert_called_once_with('job_1', 'reply lost')
