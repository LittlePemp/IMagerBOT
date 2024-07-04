import click
from imager.application.image_builder.commands.check_images import \
    CheckImagesCommand
from imager.application.image_builder.commands.load_images import \
    LoadImagesCommand
from imager.application.image_builder.commands.load_missing_groups import \
    LoadMissingGroupsCommand
from imager.application.image_builder.commands.validate_images_data import \
    ValidateImagesDataCommand
from imager.application.image_builder.commands.verify_images import \
    VerifyImagesCommand
from imager.application.image_builder.mediator import mediator
from imager.application.image_builder.queries.get_image_data import \
    GetImageDataQuery
from imager.application.image_builder.queries.get_list_image_groups import \
    GetListImageGroupsQuery

from imager.application.image_builder.commands.generate_image import (
    GenerateImageCommand, GenerateImageCommandHandler)
from imager.shared_kernel.loggers import presentation_logger


@click.group()
def cli():
    pass


@cli.command()
def load_missing_groups():
    command = LoadMissingGroupsCommand()
    result = mediator.send(command)
    if result.is_success:
        click.echo(result.value)
    else:
        click.echo(f'Error: {result.error}')


@cli.command()
@click.argument('directory')
def load(directory):
    command = LoadImagesCommand(directory)
    result = mediator.send(command)
    if result.is_success:
        click.echo(result.value)
    else:
        click.echo(f'Error: {result.error}')


@cli.command()
@click.argument('group_name')
def verify(group_name):
    command = VerifyImagesCommand(group_name)
    result = mediator.send(command)
    if result.is_success:
        click.echo(result.value)
    else:
        click.echo(f'Error: {result.error}')


@cli.command()
@click.argument('directory')
def validate(directory):
    command = ValidateImagesDataCommand(directory)
    result = mediator.send(command)
    if result.is_success:
        click.echo(result.value)
    else:
        click.echo(f'Error: {result.error}')


@cli.command()
@click.argument('group_images')
def check(group_images):
    command = CheckImagesCommand(group_images)
    result = mediator.send(command)
    if result.is_success:
        click.echo(result.value)
    else:
        click.echo(f'Error: {result.error}')


@cli.command()
def list_groups():
    query = GetListImageGroupsQuery()
    groups = mediator.send(query)
    for group in groups:
        click.echo(group)


@cli.command()
@click.argument('image_path')
def get_image_data(image_path):
    query = GetImageDataQuery(image_path)
    handler = mediator.send(query)
    image_data = handler.handle(query)
    click.echo(image_data)


@cli.command()
@click.option('--image_path', required=True, help='Path to the image to be processed')
@click.option('--group_name', required=True, help='Name of the image group')
@click.option('--insertion_format', default='crop', help='Insertion format: scale or crop')
@click.option('--alpha_channel', default=30, help='Alpha channel percentage (0-100)')
@click.option('--noise_level', default=40, help='Noise level for RGB shifts')
@click.option('--cell_size', default=60, help='Size of each cell in the final image')
@click.option('--result_size', default=120, help='Number of cells along the longest side')
def generate_image(image_path, group_name, insertion_format, alpha_channel, noise_level, cell_size, result_size):
    try:
        command = GenerateImageCommand(
            image_path=image_path,
            insertion_format=insertion_format,
            alpha_channel=alpha_channel,
            noise_level=noise_level,
            cell_size=cell_size,
            result_size=result_size,
            group_name=group_name
        )
        handler = GenerateImageCommandHandler()
        result = handler.handle(command)
        if result.is_success:
            click.echo(f'Image saved at {result.value}')
        else:
            presentation_logger.error(f'Failed to generate image: {result.error}')
    except Exception as e:
        presentation_logger.error(f'An error occurred while generating image: {e}')
