"""Admission policy for the Telegram client; image calculation stays in imager."""
from datetime import datetime, timezone
from copy import deepcopy

from imager_config.loader import config
from imager_contracts import GenerationParameters, validate_watermark_text
from src.infrastructure.data.errors import AccessDenied, DailyQuotaExceeded
from src.infrastructure.services.imager_service.schemas import GenerateImageRequest
from src.models.generation_history import generation_history_record_id
from src.models.image_build_params import ParamType
from src.models.admission import AdmissionReceipt


class GenerationSettingsUnavailable(ValueError):
    """The conversation needs a fresh settings choice before another image."""


def validate_output(result_size, cell_size):
    if (type(result_size) is not int or type(cell_size) is not int
            or not 0 < result_size <= config.limits.result_size
            or not 0 < cell_size <= config.limits.cell_size):
        raise ValueError('Качество или размер ячейки выходит за допустимые пределы.')
    pixels = (result_size * cell_size) ** 2
    if (result_size ** 2 > config.limits.cells or pixels > config.limits.output_pixels
            or pixels * config.render.estimated_jpeg_bytes_per_pixel > config.render.max_output_bytes):
        raise ValueError('Слишком большое изображение. Уменьшите качество или размер ячейки.')


class SubmitGeneration:
    def __init__(self, repositories, gateway, gate, *, fixed_admin_ids=(), watermark_text=None):
        self.repositories, self.gateway, self.gate = repositories, gateway, gate
        self.admissions = repositories.admission_repository
        self.fixed_admin_ids = frozenset(fixed_admin_ids)
        self.watermark_text = validate_watermark_text(watermark_text)

    async def previous_settings(self, actor_id):
        """Reuse values, never an earlier image, seed, command or mutable preset."""
        try:
            request = await self.admissions.latest_request(actor_id)
            if request is None:
                return None
            parameters = GenerationParameters(**request['parameters'])
            validate_output(parameters.result_size, parameters.cell_size)
        except (KeyError, TypeError, ValueError) as error:
            raise GenerationSettingsUnavailable('Сохранённые настройки больше недоступны.') from error
        return {
            'image_group': parameters.group_name,
            'image_size': parameters.result_size,
            'inset_size': parameters.cell_size,
            'noise_level': parameters.noise_level,
            'insertion_format': parameters.insertion_format,
            'alpha_channel': parameters.alpha_channel,
        }

    async def _policy(self, actor_id, draft):
        try:
            GenerationParameters(
                group_name=draft['image_group'], result_size=draft['image_size'],
                cell_size=draft['inset_size'], noise_level=draft['noise_level'],
                insertion_format=draft.get('insertion_format', config.generation.insertion_format),
                alpha_channel=draft.get('alpha_channel', config.generation.alpha_channel),
            )
            validate_output(draft['image_size'], draft['inset_size'])
        except (KeyError, TypeError, ValueError) as error:
            raise GenerationSettingsUnavailable('Параметры сборки недопустимы. Выберите их заново.') from error
        actor = await self.repositories.user_repository.get_user_by_id(actor_id)
        if actor is None or (actor.isbanned and actor_id not in self.fixed_admin_ids):
            raise AccessDenied('Приём изображений недоступен для этого аккаунта.')
        group = await self.repositories.image_group_repository.get_group_by_name(draft['image_group'])
        if group is None or not group.active:
            raise GenerationSettingsUnavailable('Библиотека больше недоступна. Выберите другую.')
        for kind, selected in draft.get('selected_presets', {}).items():
            available = await self.repositories.image_build_params_repository.get_common_params(ParamType(kind))
            if not available.is_success or not any(
                item.name == selected['name'] and item.value == selected['value'] for item in available.value
            ):
                raise GenerationSettingsUnavailable('Выбранный пресет изменён или удалён. Выберите параметры заново.')
        return actor_id in self.fixed_admin_ids or bool(actor.is_admin)

    async def _seed_day(self, actor_id, now):
        if not await self.admissions.day_exists(actor_id, now=now):
            start, end = self.admissions.day_window(now)
            used = await self.gateway.count_user_jobs_between(actor_id, start, end)
            await self.admissions.ensure_day(actor_id, initial_used=used, now=now)

    async def _capacity(self):
        pending = await self.admissions.pending_count()
        active = await self.gateway.count_active_jobs()
        if pending + active >= config.bot.max_pending_jobs:
            raise ValueError('Очередь заполнена. Попробуйте отправить изображение позже.')

    async def execute(self, *, actor_id, draft, job_id, client_context, source_loader):
        receipt = await self.admissions.find(job_id)
        if receipt is not None:
            if receipt.actor_id != actor_id:
                raise AccessDenied('Это изображение недоступно.')
            return receipt
        legacy = await self.gateway.get_job(job_id)
        if legacy is not None:
            if (legacy['request']['client_id'] != 'telegram'
                    or legacy['request']['client_context']['user_id'] != actor_id):
                raise AccessDenied('Это изображение недоступно.')
            return AdmissionReceipt(
                job_id=job_id, state='sent', date=legacy['created_at'].date().isoformat(),
                payload_hash=legacy['payload_hash'], duplicate=True,
                actor_id=actor_id,
            )
        if draft is None:
            draft = await self.previous_settings(actor_id)
            if draft is None:
                raise GenerationSettingsUnavailable('Сначала выберите настройки для изображения.')
        draft = deepcopy(draft)
        quota_exempt = await self._policy(actor_id, draft)
        now = datetime.now(timezone.utc)
        await self._seed_day(actor_id, now)
        if not quota_exempt and not await self.admissions.remaining(actor_id, now=now):
            raise DailyQuotaExceeded(config.bot.regular_user_upload_limit, self.admissions.day_window(now)[1])
        await self._capacity()
        async with self.gate.acquire(actor_id):
            source = await source_loader(job_id)
            await self._policy(actor_id, draft)
            request = GenerateImageRequest(
                source=source, group_name=draft['image_group'],
                watermark_text=self.watermark_text,
                result_size=draft['image_size'], noise_level=draft['noise_level'],
                cell_size=draft['inset_size'],
                insertion_format=draft.get('insertion_format', config.generation.insertion_format),
                alpha_channel=draft.get('alpha_channel', config.generation.alpha_channel),
            )
            context = dict(client_context, user_id=actor_id,
                           history_record_id=generation_history_record_id(job_id))
            document = await self.gateway.prepare_submission(request, job_id=job_id, client_context=context)
            async with self.gate.admission_lock:
                receipt = await self.admissions.find(job_id)
                if receipt is not None:
                    if receipt.actor_id != actor_id:
                        raise AccessDenied('Это изображение недоступно.')
                    return receipt
                quota_exempt = await self._policy(actor_id, draft)
                await self._capacity()
                now = datetime.now(timezone.utc)
                await self._seed_day(actor_id, now)
                document['created_at'] = document['updated_at'] = now
                return await self.admissions.accept_submission(
                    document, actor_id=actor_id, now=now, quota_exempt=quota_exempt,
                )
