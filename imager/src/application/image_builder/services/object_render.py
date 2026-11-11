import os
import re
import tempfile
from pathlib import Path

from src.shared_kernel.resource_limits import configure_decoder_limits

configure_decoder_limits()

import cv2
from src.application.image_builder.services.imager_builder import ImagerBuilder
from src.domain.image_builder.value_objects.image_build_params import (
    AlphaChannel, CellSize, ImageGroup, ImageInsertionFormat, NoiseLevel, ResultSize,
)
from src.shared_kernel.loggers import application_logger
from src.shared_kernel.resource_limits import (
    MAX_OUTPUT_PIXELS, generation_slot, require_memory,
)


class RenderRejected(ValueError):
    retryable = False


class RenderFailed(RuntimeError):
    def __init__(self, error):
        super().__init__(str(error))
        self.retryable = bool(getattr(error, 'retryable', False))


class ObjectRenderExecutor:
    """Synchronous rendering use case; MinIO and catalog are injected ports."""

    def __init__(self, store, catalog_provider, jpeg_quality=90,
                 max_output_bytes=47_185_920):
        if type(jpeg_quality) is not int or not 1 <= jpeg_quality <= 100:
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
        if (parameters['result_size'] * parameters['cell_size']) ** 2 > MAX_OUTPUT_PIXELS:
            raise RenderRejected('Requested output exceeds the core pixel budget')
        return parameters

    def execute(self, request, execution_token, heartbeat_callback=None):
        def heartbeat(details):
            if heartbeat_callback is not None:
                heartbeat_callback({'job_id': request.get('job_id'), **details})

        try:
            parameters = self._parameters(request)
            job_id = request.get('job_id')
            if (not isinstance(job_id, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', job_id)
                    or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', execution_token)):
                raise RenderRejected('Job and execution token must be normalized identifiers')
            seed = request.get('seed')
            if type(seed) is not int or not 0 <= seed < 2 ** 64:
                raise RenderRejected('Seed must be an unsigned 64-bit integer')
            self.store.validate_reference(request.get('source'), 64_000_000)
            manifest_ref = request.get('catalog_manifest')
            if not manifest_ref:
                raise RenderRejected('Pinned catalog manifest is required')
            with generation_slot(), tempfile.TemporaryDirectory(prefix='imager-render-') as directory:
                heartbeat({'stage': 'downloading_source'})
                require_memory(256_000_000)
                source_path = self.store.download_file(
                    request['source'], Path(directory) / 'source.bin',
                )
                source = cv2.imread(source_path, cv2.IMREAD_UNCHANGED)
                heartbeat({'stage': 'loading_catalog'})
                catalog = self.catalog_provider.for_manifest(manifest_ref, parameters['group_name'])
                builder = ImagerBuilder(
                    file_repository=None, cell_repository=catalog,
                    insertion_format=parameters['insertion_format'],
                    result_size=parameters['result_size'], cell_size=parameters['cell_size'],
                    alpha=parameters['alpha_channel'] / 100,
                    noise_degree=parameters['noise_level'], seed=seed,
                )
                rendered = builder.render_image(source, parameters['group_name'], heartbeat)
                if not rendered:
                    raise RenderFailed(rendered.error)
                heartbeat({'stage': 'encoding_result'})
                require_memory(64_000_000)
                result_path = Path(directory) / 'result.jpg'
                written = cv2.imwrite(str(result_path), rendered.value, [
                    int(cv2.IMWRITE_JPEG_QUALITY), self.jpeg_quality,
                ])
                if not written or not result_path.is_file() or result_path.stat().st_size == 0:
                    raise OSError('JPEG encoder did not write a result')
                if result_path.stat().st_size > self.max_output_bytes:
                    raise RenderRejected('Encoded output exceeds the delivery byte budget')
                heartbeat({'stage': 'uploading_result'})
                key = f'jobs/{job_id}/attempts/{execution_token}/result.jpg'
                return self.store.upload_file(result_path, key, 'image/jpeg')
        except Exception:
            application_logger.exception(f'Object render failed: job_id={request.get("job_id")}')
            raise
