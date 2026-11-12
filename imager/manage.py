import sys
from pathlib import Path

# The two applications share configuration and wire contracts at repository root.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.shared_kernel.resource_limits import configure_decoder_limits

configure_decoder_limits()

from settings import init_app
from src.presentation.cli.cli import cli  # noqa

if __name__ == '__main__':
    # Initializations
    init_app()

    # Terminal menu
    cli()
