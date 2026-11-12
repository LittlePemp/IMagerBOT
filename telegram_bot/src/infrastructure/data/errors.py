"""Expected storage/application outcomes, distinct from database failures."""


class AccessDenied(ValueError):
    pass


class ProtectedAdministrator(ValueError):
    pass


class UserConflict(ValueError):
    pass


class UserNotFound(ValueError):
    pass


class DailyQuotaExceeded(ValueError):
    def __init__(self, limit, resets_at):
        self.limit = limit
        self.resets_at = resets_at
        super().__init__(f'Лимит {limit} изображений на сегодня исчерпан.')


class AdmissionConflict(ValueError):
    pass


class AdmissionUncertain(RuntimeError):
    def __init__(self, job_id):
        self.job_id = job_id
        super().__init__(f'Не удалось уточнить приём задания {job_id}.')
