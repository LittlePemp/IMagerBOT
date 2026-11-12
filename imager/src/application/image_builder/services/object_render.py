import re
import tempfile
from pathlib import Path

from imager_config.loader import config
from imager_contracts.protocol import (
    JPEG_CONTENT_TYPE, JOB_ID_PATTERN, PERCENT_MAX, SEED_UPPER_BOUND,
)
from src.shared_kernel.resource_limits import configure_decoder_limits

configure_decoder_limits()

import cv2
from src.application.image_builder.services.imager_builder import ImagerBuilder
from src.domain.image_builder.value_objects.image_build_params import (
    AlphaChannel, CellSize, ImageGroup, ImageInsertionFormat, NoiseLevel, ResultSize,
)
from src.shared_kernel.loggers import application_logger
from src.shared_kernel.resource_limits import generation_slot, require_memory


class RenderRejected(ValueError):
    retryable = False


class RenderFailed(RuntimeError):
    def __init__(self, error):
        super().__init__(str(error))
        self.retryable = bool(getattr(error, 'retryable', False))


class ObjectRenderExecutor:
    """Synchronous rendering use case; MinIO and catalog are injected ports."""

    def __init__(self, store, catalog_provider, jpeg_quality=None,
                 max_output_bytes=None):
        jpeg_quality = config.render.jpeg_quality if jpeg_quality is None else jpeg_quality
        max_output_bytes = config.render.max_output_bytes if max_output_bytes is None else max_output_bytes
        if type(jpeg_quality) is not int or not 1 <= jpeg_quality <= PERCENT_MAX:
            raise ValueError('JPEG quality must be an integer between 1 and 100')
        if type(max_output_bytes) is not int or max_output_bytes <= 0:
            raise ValueError('Output byte limit must be positive')
        self.store = store
        self.catalog_provider = catalog_provider
        self.jpeg_quality = jpeg_quality
        self.max_output_bytes = max_output_bytes

    @staticmethod
    def _parameters(request):
        parameters = request.get('parameters')
        if not isinstance(parameters, dict):
            raise RenderRejected('Generation parameters are required')
        for name, factory in (
            ('group_name', ImageGroup), ('insertion_format', ImageInsertionFormat),
            ('alpha_channel', AlphaChannel), ('noise_level', NoiseLevel),
            ('cell_size', CellSize), ('result_size', ResultSize),
        ):
            result = factory.create(parameters.get(name))
            if not result:
                raise RenderRejected(str(result.error))
        if (parameters['result_size'] * parameters['cell_size']) ** 2 > config.limits.output_pixels:
            raise RenderRejected('Requested output exceeds the core pixel budget')
        return parameters

    def execute(self, request, execution_token, heartbeat_callback=None):
        def heartbeat(details):
            if heartbeat_callback is not None:
                heartbeat_callback({'job_id': request.get('job_id'), **details})

        try:
            parameters = self._parameters(request)
            job_id = request.get('job_id')
            if (not isinstance(job_id, str) or not re.fullmatch(JOB_ID_PATTERN, job_id)
                    or not isinstance(execution_token, str)
                    or not re.fullmatch(JOB_ID_PATTERN, execution_token)):
                raise RenderRejected('Job and execution token must be normalized identifiers')
            seed = request.get('seed')
            if type(seed) is not int or not 0 <= seed < SEED_UPPER_BOUND:
                raise RenderRejected(f'Seed must be an integer between 0 and {SEED_UPPER_BOUND - 1}')
            self.store.validate_reference(request.get('source'), config.limits.source_file_bytes)
            manifest_ref = request.get('catalog_manifest')
            if not manifest_ref:
                raise RenderRejected('Pinned catalog manifest is required')
            with generation_slot(), tempfile.TemporaryDirectory(
                prefix=config.storage.render_temp_prefix,
            ) as directory:
                heartbeat({'stage': 'downloading_source'})
                require_memory(config.memory.decode_reserve_bytes)
                source_path = self.store.download_file(
                    request['source'], Path(directory) / config.storage.source_filename,
                )
                source = cv2.imread(source_path, cv2.IMREAD_UNCHANGED)
                heartbeat({'stage': 'loading_catalog'})
                catalog = self.catalog_provider.for_manifest(manifest_ref, parameters['group_name'])
                builder = ImagerBuilder(
                    file_repository=None, cell_repository=catalog,
                    insertion_format=parameters['insertion_format'],
                    result_size=parameters['result_size'], cell_size=parameters['cell_size'],
                    alpha=parameters['alpha_channel'] / PERCENT_MAX,
                    noise_degree=parameters['noise_level'], seed=seed,
                )
                rendered = builder.render_image(source, parameters['group_name'], heartbeat)
                if not rendered:
                    raise RenderFailed(rendered.error)
                heartbeat({'stage': 'encoding_result'})
                require_memory(config.memory.encode_reserve_bytes)
                result_path = Path(directory) / config.storage.result_filename
                written = cv2.imwrite(str(result_path), rendered.value, [
                    int(cv2.IMWRITE_JPEG_QUALITY), self.jpeg_quality,
                ])
                if not written or not result_path.is_file() or result_path.stat().st_size == 0:
                    raise OSError('JPEG encoder did not write a result')
                if result_path.stat().st_size > self.max_output_bytes:
                    raise RenderRejected('Encoded output exceeds the delivery byte budget')
                heartbeat({'stage': 'uploading_result'})
                key = config.storage.result_key_template.format(
                    job_id=job_id, execution_token=execution_token,
                    result_filename=config.storage.result_filename,
                )
                return self.store.upload_file(result_path, key, JPEG_CONTENT_TYPE)
        except Exception:
            application_logger.exception(f'Object render failed: job_id={request.get("job_id")}')
            raise
