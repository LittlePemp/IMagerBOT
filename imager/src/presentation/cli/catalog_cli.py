"""One-off migration: local source directories into immutable MinIO catalogs."""
import json

import click


@click.command()
@click.argument('directory', type=click.Path(exists=True, file_okay=False))
@click.option('--group-name', default=None, help='Treat directory as one named group')
def cli(directory, group_name):
    from pymongo import MongoClient
    from settings import settings
    from src.infrastructure.object_storage.catalog import import_local_catalog
    from src.infrastructure.object_storage.minio_store import object_store_from_settings
    from src.shared_kernel.loggers import presentation_logger

    try:
        store = object_store_from_settings(settings)
        with MongoClient(settings.mongodb_uri) as mongo:
            report = import_local_catalog(
                directory, store, mongo[settings.database_name].catalog_groups, group_name,
            )
        click.echo(json.dumps(report, default=str, ensure_ascii=False, indent=2))
    except Exception as error:
        presentation_logger.exception('Catalog import failed')
        raise click.ClickException(str(error)) from error


if __name__ == '__main__':
    cli()
