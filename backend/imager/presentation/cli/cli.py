import click
from imager.application.image_builder.commands.check_images import \
    CheckImagesCommand
from imager.application.image_builder.commands.load_images import \
    LoadImagesCommand
from imager.application.image_builder.commands.validate_images_data import \
    ValidateImagesDataCommand
from imager.application.image_builder.commands.verify_images import \
    VerifyImagesCommand
from imager.application.image_builder.mediator import mediator
from imager.application.image_builder.queries.get_image_data import \
    GetImageDataQuery
from imager.application.image_builder.queries.get_list_image_groups import \
    GetListImageGroupsQuery
from imager.application.image_builder.commands.load_missing_groups import LoadMissingGroupsCommand


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
@click.argument('directory')
def verify(directory):
    command = VerifyImagesCommand(directory)
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
@click.argument('directory')
def check(directory):
    command = CheckImagesCommand(directory)
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
