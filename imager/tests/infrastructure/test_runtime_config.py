import dataclasses
import json
import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest.mock import patch

from imager_config.loader import CONFIG_OVERRIDE_VARIABLE, ConfigurationError, config, load_config


class TestRuntimeConfig(unittest.TestCase):
    def test_retention_is_global_bounded_and_validated(self):
        self.assertEqual((config.retention.source_count, config.retention.result_count), (10, 10))
        for content in (
            '[retention]\nsource_count=0', '[retention]\nresult_count=-1',
            '[retention]\nscan_size=9', '[retention]\nbatch_size=257',
            '[retention]\nlease_seconds=0',
        ):
            with self.subTest(content=content):
                self.reject(content)

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='imager-config-test-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        environment = patch.dict(os.environ)
        environment.start()
        self.addCleanup(environment.stop)
        os.environ.pop(CONFIG_OVERRIDE_VARIABLE, None)

    def override(self, content):
        path = self.root / 'override.toml'
        path.write_text(textwrap.dedent(content), encoding='utf-8')
        return path

    def load(self, content):
        return load_config(self.override(content))

    def reject(self, content):
        with self.assertRaises(ConfigurationError):
            self.load(content)

    def test_partial_override_preserves_defaults_and_global_snapshot(self):
        baseline = load_config()
        changed = self.load('[render]\njpeg_quality=84\n[storage]\nconnect_timeout_seconds=2\n')
        self.assertEqual(changed.render.jpeg_quality, 84)
        self.assertEqual(changed.storage.connect_timeout_seconds, 2.0)
        self.assertIs(type(changed.storage.connect_timeout_seconds), float)
        self.assertEqual(changed.storage.result_filename, baseline.storage.result_filename)
        self.assertEqual(changed.limits, baseline.limits)
        self.assertEqual(config.render.jpeg_quality, baseline.render.jpeg_quality)

    def test_environment_selects_explicit_partial_override(self):
        os.environ[CONFIG_OVERRIDE_VARIABLE] = str(self.override('[render]\njpeg_quality=83\n'))
        self.assertEqual(load_config().render.jpeg_quality, 83)

    def test_nested_models_and_sequence_fields_are_immutable(self):
        changed = load_config()
        with self.assertRaises(dataclasses.FrozenInstanceError):
            changed.storage.result_filename = 'other.jpg'
        with self.assertRaises(dataclasses.FrozenInstanceError):
            changed.render = changed.render
        self.assertIsInstance(changed.catalog.allowed_extensions, tuple)

    def test_unknown_fields_and_tables_are_not_silently_ignored(self):
        for content in ('[render]\njpeg_quailty=84', '[unexpected]\nvalue=1', '[render]\nquality=1'):
            with self.subTest(content=content):
                self.reject(content)

    def test_strict_scalar_and_sequence_types_reject_bool_as_integer(self):
        for content in (
            '[render]\njpeg_quality=true', '[storage]\nsecure="false"',
            '[catalog]\nallowed_extensions=".jpg"', '[image]\nallowed_color_modes=["RGB", false]',
            '[limits]\nsource_pixels=1.5', '[render]\nvalue=1', 'storage="invalid table"',
        ):
            with self.subTest(content=content):
                self.reject(content)

    def test_nonfinite_negative_and_unrepresentable_durations_fail(self):
        for value in ('nan', 'inf', '-inf', '-1'):
            with self.subTest(value=value):
                self.reject(f'[storage]\nretry_backoff_seconds={value}')
        for value in ('1e-12', '1e30'):
            with self.subTest(value=value):
                self.reject(f'[temporal.render]\nschedule_to_close_seconds={value}')

    def test_plain_jpeg_filenames_are_required_and_mime_cannot_drift(self):
        for filename in ('../result.jpg', '/tmp/result.jpg', 'C:\\result.jpg', 'result.png',
                         'result.jpg/next.jpg', 'result.jpg ', 'result:ads.jpg', 'NUL.jpg', '.jpg'):
            with self.subTest(filename=filename):
                self.reject(f'[storage]\nresult_filename={json.dumps(filename)}')
        self.assertEqual(self.load('[storage]\nresult_filename="complete.jpeg"').storage.result_filename,
                         'complete.jpeg')

    def test_credential_subdirectory_and_temp_prefix_paths_stay_within_their_roots(self):
        for content in (
            '[paths]\nui_credentials="../credentials.json"',
            '[paths]\nworker_scratch_directory="../../tmp"',
            '[paths]\nimage_groups="../groups"', '[paths]\ncompose_env="/tmp/compose.env"',
            '[paths]\ndata_root="../data"', '[storage]\ncell_temp_prefix="../cell-"',
            '[render]\noutput_timestamp_format="%Y/%m"', '[render]\noutput_stem="../image"',
        ):
            with self.subTest(content=content):
                self.reject(content)
        changed = self.load('[paths]\ndata_root="E:/imager-data"\nsecrets_root="/srv/imager/secrets"')
        self.assertEqual(changed.paths.data_root, 'E:/imager-data')

    def test_storage_keys_require_full_job_attempt_and_content_hash_identifiers(self):
        invalid = (
            ('result_key_template', 'jobs/{job_id}/{result_filename}'),
            ('result_key_template', 'jobs/{job_id}/{execution_token:.1}/{result_filename}'),
            ('result_key_template', 'jobs/{job_id}/{execution_token!r}/{result_filename}'),
            ('result_key_template', 'jobs/{job_id}/{execution_token[0]}/{result_filename}'),
            ('result_key_template', 'jobs/{job_id}/{execution_token}/{execution_token}/{result_filename}'),
            ('source_key_template', 'uploads/{job_id}/source.jpg'),
            ('source_key_template', 'uploads/{job_id}/{sha256:.1}.jpg'),
            ('catalog_image_key_template', 'images/{sha256!s}{extension}'),
            ('catalog_manifest_key_template', 'manifests/{group_id}/{sha256:.8}.json'),
            ('result_key_template', '/jobs/{job_id}/{execution_token}/{result_filename}'),
            ('result_key_template', 'jobs/../{job_id}/{execution_token}/{result_filename}'),
        )
        for name, template in invalid:
            with self.subTest(name=name, template=template):
                self.reject(f'[storage]\n{name}={json.dumps(template)}')

    def test_storage_key_length_accounts_for_identifiers_and_filename(self):
        template = 'x' * 900 + '/{job_id}/{execution_token}/{result_filename}'
        self.reject(f'[storage]\nresult_key_template={json.dumps(template)}')

    def test_local_output_template_preserves_identity_and_stays_a_plain_filename(self):
        for template in (
            '../{stem}_{timestamp}_{suffix}.{extension}',
            '{stem}_{timestamp}_{suffix:.1}.{extension}',
            '{stem}_{timestamp}_{suffix!s}.{extension}',
            '{stem}_{timestamp}.{extension}',
            '{stem}_{timestamp}_{suffix[0]}.{extension}',
        ):
            with self.subTest(template=template):
                self.reject(f'[render]\noutput_filename_template={json.dumps(template)}')
        self.reject('[render]\noutput_stem_max_length=240')
        changed = self.load('[render]\noutput_filename_template="{timestamp}-{stem}-{suffix}.{extension}"')
        self.assertEqual(changed.render.output_filename_template, '{timestamp}-{stem}-{suffix}.{extension}')
        self.reject('[paths]\nrpc_journal_directory="../rpc"')
        self.reject('[storage]\nrpc_temp_prefix="../rpc-"')
        self.reject('[storage]\nlocal_result_temp_prefix="../local-"')

    def test_memory_ceiling_and_allocation_reservations_are_consistent(self):
        for content in (
            '[limits]\nmemory_bytes=1000000001', '[limits]\noriginal_cache_bytes=900000000',
            '[limits]\nmemory_headroom_bytes=1000000000',
            '[memory]\ndecode_reserve_bytes=1000000000', '[memory]\nrender_bytes_per_pixel=3',
            '[storage]\ntransfer_chunk_bytes=65000000', '[limits]\nsource_pixels=100',
        ):
            with self.subTest(content=content):
                self.reject(content)

    def test_zero_cache_and_disabled_retries_are_supported_explicitly(self):
        changed = self.load('''
            [limits]
            original_cache_bytes=0
            prepared_cache_bytes=0
            [storage]
            retry_attempts=0
            retry_backoff_seconds=0
            [bot]
            noise_min=0
            noise_max=0
            [generation]
            alpha_channel=0
            noise_level=0
        ''')
        self.assertEqual(changed.limits.original_cache_bytes, 0)
        self.assertEqual(changed.storage.retry_attempts, 0)
        self.assertEqual(changed.generation.noise_level, 0)

    def test_generation_defaults_fit_admin_ranges_and_core_limits(self):
        for content in (
            '[generation]\ninsertion_format="stretch"', '[generation]\nalpha_channel=101',
            '[generation]\nnoise_level=101', '[generation]\ncell_size=201',
            '[generation]\ncell_size=0', '[bot]\nnoise_min=50\nnoise_max=20',
            '[bot]\ncell_size_max=2001', '[bot]\nresult_size_max=1001',
            '[bot]\nresult_size_min=200', '[limits]\ncells=100', '[limits]\noutput_pixels=100',
        ):
            with self.subTest(content=content):
                self.reject(content)
        changed = self.load('[bot]\ncell_size_max=300\n[generation]\ncell_size=300\nresult_size=20')
        self.assertEqual(changed.generation.cell_size, 300)

    def test_temporal_retry_and_timeout_ordering_is_validated(self):
        for content in (
            '[temporal.render]\nretry_initial_seconds=31\nretry_maximum_seconds=30',
            '[temporal.failure]\nretry_initial_seconds=11\nretry_maximum_seconds=10',
            '[temporal.render]\nschedule_to_close_seconds=899',
            '[temporal.failure]\nschedule_to_close_seconds=29',
            '[temporal.render]\nstart_to_close_seconds=30',
            '[jobs]\nclaim_wait_seconds=30', '[jobs]\nheartbeat_interval_seconds=30',
            '[jobs]\nheartbeat_renew_divisor=1', '[jobs]\nheartbeat_join_seconds=10',
            '[mongo]\nserver_selection_timeout_ms=30000',
            '[temporal.worker]\nactivity_slots=2',
        ):
            with self.subTest(content=content):
                self.reject(content)

    def test_notification_renewal_and_backoff_happen_within_their_bounds(self):
        for content in (
            '[bot]\nnotification_renew_divisor=1',
            '[bot]\nnotification_renew_min_seconds=120',
            '[bot]\nhistory_repair_retry_initial_seconds=301',
            '[bot]\nhistory_repair_backoff_coefficient=0.5',
            '[bot]\nprogress_batch_size=63',
            '[bot]\nerror_excerpt_length=2001',
        ):
            with self.subTest(content=content):
                self.reject(content)

    def test_storage_and_deployment_identifiers_are_validated(self):
        for content in (
            '[storage]\nbucket="Invalid/Bucket"', '[catalog]\nallowed_extensions=[]',
            '[catalog]\nallowed_extensions=[".jpg", ".jpg"]', '[catalog]\nallowed_extensions=["JPG"]',
            '[deployment]\ntemporal_ui_port=65536', '[deployment]\ntemporal_ui_port=9001',
            '[image]\nallowed_color_modes=[]', '[render]\noutput_random_suffix_length=33',
        ):
            with self.subTest(content=content):
                self.reject(content)

    def test_models_and_contract_definitions_import_without_configuration_io(self):
        project = Path(__file__).resolve().parents[3]
        code = '''
import importlib, pathlib, sys
from unittest.mock import patch
sys.path.insert(0, sys.argv[1])
with patch('builtins.open', side_effect=AssertionError('unexpected file I/O')), \\
     patch('pathlib.Path.open', side_effect=AssertionError('unexpected config I/O')):
    models = importlib.import_module('imager_config.models')
    importlib.reload(models)
    importlib.import_module('imager_contracts')
assert 'imager_config.loader' not in sys.modules
'''
        result = subprocess.run(
            [sys.executable, '-I', '-B', '-c', textwrap.dedent(code), str(project)],
            capture_output=True, text=True, timeout=15,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_production_configuration_preserves_isolation_and_tls_identity(self):
        for content in (
            '[deployment.production]\nenv_file="infra/.env"',
            '[deployment.production]\npostgres_directory="mongo"',
            '[deployment.production]\nclient_certificate="client.key"',
            '[deployment.production]\npostgres_user="invalid; DROP ROLE postgres"',
            '[deployment.production]\nvisibility_database="temporal"',
            '[deployment.production]\nrsa_key_bits=1024',
            '[deployment.production]\ncertificate_days=3650',
            '[deployment.production]\nhistory_shards=127',
            '[deployment.production]\ntemporal_ui_port=8088',
            '[temporal.tls]\nenabled=true',
            '[temporal.tls]\nclient_key="configured-without-tls.key"',
        ):
            with self.subTest(content=content):
                self.reject(content)


if __name__ == '__main__':
    unittest.main()
