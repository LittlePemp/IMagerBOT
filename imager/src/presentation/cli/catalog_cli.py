"""One-off migration: local source directories into immutable MinIO catalogs."""
import json
import os

import click


@click.command()
@click.argument('directory', type=click.Path(exists=True, file_okay=False))
@click.option('--group-name', default=None, help='Treat directory as one named group')
@click.option('--dry-run', is_flag=True, help='Validate/hash local photos and report capacity without contacting MinIO or Mongo')
def cli(directory, group_name, dry_run):
    from imager_config.loader import config
    from src.infrastructure.object_storage.catalog import import_local_catalog, preview_local_catalog
    from src.shared_kernel.loggers import presentation_logger

    try:
        if dry_run:
            report = preview_local_catalog(
                directory, group_name, bucket=os.environ.get('MINIO_BUCKET', config.storage.bucket),
            )
        else:
            from pymongo import MongoClient
            from settings import settings
            from src.infrastructure.object_storage.minio_store import object_store_from_settings

            store = object_store_from_settings(settings)
            with MongoClient(
                settings.mongodb_uri, tz_aware=True,
                connectTimeoutMS=config.mongo.connect_timeout_ms,
                socketTimeoutMS=config.mongo.socket_timeout_ms,
                serverSelectionTimeoutMS=config.mongo.server_selection_timeout_ms,
                waitQueueTimeoutMS=config.mongo.wait_queue_timeout_ms,
            ) as mongo:
                report = import_local_catalog(
                    directory, store, mongo[settings.database_name][config.mongo.catalog_collection], group_name,
                )
        totals = {name: sum(group[name] for group in report) for name in (
            'source_files', 'count', 'duplicate_files', 'source_bytes', 'unique_image_bytes',
            'manifest_bytes', 'additional_payload_bytes_upper_bound', 'normalized_cache_bytes',
        )}
        click.echo(json.dumps(
            {'dry_run': dry_run, 'groups': report, 'totals': totals,
             'payload_estimate_excludes_minio_metadata': True},
            default=str, ensure_ascii=False, indent=2,
        ))
    except Exception as error:
        presentation_logger.exception('Catalog import failed')
        raise click.ClickException(str(error)) from error


if __name__ == '__main__':
    cli()
