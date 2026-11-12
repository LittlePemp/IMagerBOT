class InvalidCatalogArchive(ValueError):
    """A permanently rejected archive, safe to describe to its administrator."""


class CatalogImportConflict(ValueError):
    """A name or immutable import identity is already occupied."""


class CatalogImportBusy(RuntimeError):
    retryable = True


class LostCatalogImport(RuntimeError):
    retryable = True
