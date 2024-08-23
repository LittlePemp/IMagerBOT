from settings import init_app
from src.presentation.cli.cli import cli  # noqa

if __name__ == '__main__':
    # Initializations
    init_app()

    # Terminal menu
    cli()
