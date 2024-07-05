import os

os.environ['PY_ASSIMILATOR_MESSAGE'] = 'False'

from src.presentation.cli.cli import cli  # noqa

if __name__ == '__main__':
    cli()
