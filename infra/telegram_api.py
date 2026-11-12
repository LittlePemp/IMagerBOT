"""Explicit operator-only cloud logout before switching to Local Bot API.

This command never starts polling or sends messages. Tokens are loaded from
the ignored project environment file and are not included in diagnostics.
"""
import argparse
import json
import re
import sys
from pathlib import Path
from urllib.request import Request, urlopen

sys.path.insert(0, str(Path(__file__).resolve().parent))
from setup import ROOT, parse_env, config

CLOUD_ENDPOINT = 'https://api.telegram.org'


def logout_cloud(token, *, opener=urlopen):
    if not isinstance(token, str) or not re.fullmatch(r'[0-9]+:[A-Za-z0-9_-]+', token):
        raise ValueError('Configure a valid API_TOKEN in the selected environment file.')
    request = Request(f'{CLOUD_ENDPOINT}/bot{token}/logOut', data=b'', method='POST')
    try:
        with opener(request, timeout=config.bot.api_timeout_seconds) as response:
            result = json.load(response)
    except Exception:
        raise RuntimeError('Cloud logout request failed. Check connectivity and the configured bot token.') from None
    if result.get('ok') is not True or result.get('result') is not True:
        raise RuntimeError('Telegram did not confirm cloud logout. The local server was not started.')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--env-file', type=Path, required=True)
    parser.add_argument('--logout-cloud', action='store_true', required=True,
                        help='Explicitly log the configured bot out of the cloud API')
    args = parser.parse_args()
    selected = args.env_file if args.env_file.is_absolute() else ROOT / args.env_file
    # The explicitly selected project file is authoritative. A token inherited
    # from another shell/project must never log a different bot out.
    token = parse_env(selected).get('API_TOKEN')
    try:
        logout_cloud(token)
    except (ValueError, RuntimeError) as error:
        parser.error(str(error))
    print('Cloud logout confirmed. You can start the project Local Bot API and polling bot.')


if __name__ == '__main__':
    main()
