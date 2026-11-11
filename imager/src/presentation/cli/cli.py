import asyncio

import click
import uvicorn
from src.application.image_builder.commands.check_images import \
    CheckImagesCommand
from src.application.image_builder.commands.download_topic import \
    DownloadTopicCommand
from src.application.image_builder.commands.generate_image import \
    GenerateImageCommand
from src.application.image_builder.commands.load_images import \
    LoadImagesCommand
from src.application.image_builder.commands.load_missing_groups import \
    LoadMissingGroupsCommand
from src.application.image_builder.commands.validate_images_data import \
    ValidateImagesDataCommand
from src.application.image_builder.commands.verify_images import \
    VerifyImagesCommand
from src.application.image_builder.mediator import mediator
from src.application.image_builder.queries.get_image_data import \
    GetImageDataQuery
from src.application.image_builder.queries.get_list_image_groups import \
    GetListImageGroupsQuery
from src.presentation.rabbitmq.consumer import run_consumer
from src.shared_kernel.loggers import presentation_logger
from src.shared_kernel.result import Result


def _execute(request):
    try:
        result = mediator.send(request)
    except Exception as exc:
        presentation_logger.exception(f'CLI operation failed: {exc}')
        raise click.ClickException(str(exc)) from exc
    if isinstance(result, Result):
        if not result.is_success:
            presentation_logger.error(f'CLI operation failed: {result.error}')
            raise click.ClickException(str(result.error))
        return result.value
    return result


@click.group()
def cli():
    pass


@cli.command()
def load_missing_groups():
    command = LoadMissingGroupsCommand()
    click.echo(_execute(command))


@cli.command()
@click.argument('directory')
def load(directory):
    command = LoadImagesCommand(directory)
    click.echo(_execute(command))


@cli.command()
@click.argument('group_name')
def verify(group_name):
    command = VerifyImagesCommand(group_name)
    click.echo(_execute(command))


@cli.command()
@click.argument('group_name')
def validate(group_name):
    command = ValidateImagesDataCommand(group_name)
    click.echo(_execute(command))


@cli.command()
@click.argument('group_images')
def check(group_images):
    command = CheckImagesCommand(group_images)
    click.echo(_execute(command))


@cli.command()
@click.option('--keyword', required=True, help='Search phrase on fonwall.ru')
@click.option('--group-name', default=None, help='Destination group name')
@click.option('--limit', default=None, type=int, help='Maximum number of images to download')
@click.option('--max-pages', default=25, type=int, help='Maximum number of pages to scan')
@click.option('--sync/--no-sync', 'sync_to_db', default=True, help='Synchronize downloaded images into MongoDB')
def download_topic(keyword, group_name, limit, max_pages, sync_to_db):
    command = DownloadTopicCommand(
        keyword=keyword,
        group_name=group_name,
        limit=limit,
        max_pages=max_pages,
        sync_to_db=sync_to_db,
    )
    click.echo(_execute(command))


@cli.command()
def list_groups():
    query = GetListImageGroupsQuery()
    groups = _execute(query)
    for group in groups:
        click.echo(group)


@cli.command()
@click.argument('image_path')
def get_image_data(image_path):
    query = GetImageDataQuery(image_path)
    image_data = _execute(query)
    click.echo({'shape': tuple(image_data.shape), 'dtype': str(image_data.dtype)})


@cli.command()
@click.option('--image_path', required=True, help='Path to the image to be processed')
@click.option('--group_name', required=True, help='Name of the image group')
@click.option('--insertion_format', default='crop', help='Insertion format: scale or crop')
@click.option('--alpha_channel', default=30, help='Alpha channel percentage (0-100)')
@click.option('--noise_level', default=15, help='Noise level for RGB shifts')
@click.option('--cell_size', default=40, help='Size of each cell in the final image')
@click.option('--result_size', default=120, help='Number of cells along the longest side')
def generate_image(image_path, group_name, insertion_format, alpha_channel, noise_level, cell_size, result_size):
    command = GenerateImageCommand(
        image_path=image_path,
        insertion_format=insertion_format,
        alpha_channel=alpha_channel,
        noise_level=noise_level,
        cell_size=cell_size,
        result_size=result_size,
        group_name=group_name,
    )
    click.echo(f'Image saved at {_execute(command)}')


@cli.command()
@click.option('--host', default='0.0.0.0', help='Host to run the server on')
@click.option('--port', default=8000, help='Port to run the server on')
def runserver(host, port):
    '''Run the FastAPI server.'''
    uvicorn.run('src.presentation.api.main:app', host=host, port=port,)


@cli.command('run-consumer')
def run_consumer_command():
    '''Run RabbitMQ consumer for imager commands.'''
    try:
        asyncio.run(run_consumer())
    except Exception as exc:
        presentation_logger.exception(f'Consumer failed: {exc}')
        raise click.ClickException(str(exc)) from exc
