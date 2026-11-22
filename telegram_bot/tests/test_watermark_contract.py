"""Optional output branding must not invalidate accepted legacy commands."""
import copy
import hashlib
import json
import unittest
from dataclasses import replace

from bson import BSON

from imager_contracts import JobRequest, make_submission, payload_fingerprint, validate_watermark_text
from imager_contracts.protocol import WATERMARK_TEXT_MAX_LENGTH


def legacy_request():
    return {
        'job_id': 'legacy-watermark-job',
        'source': {'bucket': 'imager', 'key': 'uploads/legacy/source.png',
                   'sha256': 'a' * 64, 'size_bytes': 6, 'content_type': 'image/png'},
        'parameters': {'group_name': 'emoji', 'insertion_format': 'crop', 'alpha_channel': 30,
                       'noise_level': 30, 'cell_size': 40, 'result_size': 64},
        'catalog_manifest': {'bucket': 'imager', 'key': 'catalog/legacy/manifest.json',
                             'sha256': 'b' * 64, 'size_bytes': 9, 'content_type': 'application/json'},
        'seed': 2 ** 40 + 123, 'client_id': 'telegram',
        'client_context': {'user_id': 6000000000, 'chat_id': 6000000000}, 'schema_version': 1,
    }


class WatermarkContractTests(unittest.TestCase):
    def test_missing_or_explicit_null_watermark_keeps_exact_legacy_envelope_and_hash(self):
        legacy = legacy_request()
        original = copy.deepcopy(legacy)
        # Hash the actual old wire representation, independently of JobRequest.
        encoded = json.dumps(legacy, sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False)
        old_digest = hashlib.sha256(encoded.encode()).hexdigest()
        for wire in (legacy, dict(legacy, watermark_text=None)):
            with self.subTest(explicit_null='watermark_text' in wire):
                request = JobRequest.from_dict(wire)
                self.assertIsNone(request.watermark_text)
                self.assertEqual(request.to_dict(), original)
                self.assertNotIn('watermark_text', make_submission(request)['request'])
                self.assertEqual(payload_fingerprint(request), old_digest)
                self.assertEqual(payload_fingerprint(wire), old_digest)
        self.assertEqual(legacy, original)

    def test_watermark_is_frozen_outside_generation_parameters_and_affects_identity(self):
        plain = JobRequest.from_dict(legacy_request())
        branded = replace(plain, watermark_text='t.me/FirstBot')
        other = replace(branded, watermark_text='t.me/OtherBot')
        self.assertEqual(branded.to_dict()['watermark_text'], 't.me/FirstBot')
        self.assertEqual(branded.to_dict()['parameters'], plain.to_dict()['parameters'])
        self.assertNotEqual(payload_fingerprint(plain), payload_fingerprint(branded))
        self.assertNotEqual(payload_fingerprint(branded), payload_fingerprint(other))
        snapshot = make_submission(branded)
        self.assertEqual(snapshot['payload_hash'], payload_fingerprint(branded))
        self.assertEqual(JobRequest.from_dict(snapshot['request']), branded)

    def test_legacy_and_branded_commands_survive_real_bson_int64_roundtrip(self):
        for text in (None, 't.me/BrandBot'):
            with self.subTest(text=text):
                original = make_submission(dict(legacy_request(), watermark_text=text))
                restored = BSON.encode(original).decode()
                request = JobRequest.from_dict(restored['request'])
                self.assertEqual(payload_fingerprint(request), original['payload_hash'])
                self.assertEqual(request.watermark_text, text)
                self.assertIs(type(request.seed), int)
                self.assertEqual(request.to_dict(), original['request'])

    def test_only_bounded_visible_single_line_ascii_is_accepted(self):
        for invalid in ('', '   ', True, 123, b't.me/Bot', 't.me/Bot\n', 'line\rbreak',
                        'tab\ttext', 'bad\x00text', 'bad\x7ftext', 'бот', 'a' * (WATERMARK_TEXT_MAX_LENGTH + 1)):
            with self.subTest(value=repr(invalid)):
                with self.assertRaises(ValueError):
                    validate_watermark_text(invalid)
                with self.assertRaises(ValueError):
                    JobRequest.from_dict(dict(legacy_request(), watermark_text=invalid))
        for accepted in (None, 't.me/Bot', 'Company Brand', 'x' * WATERMARK_TEXT_MAX_LENGTH):
            with self.subTest(value=accepted):
                self.assertEqual(validate_watermark_text(accepted), accepted)
