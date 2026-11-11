"""Create project-local development credentials; no packages/services required.

Secrets and data are never committed. This script does not pull images, start
Docker, inspect other projects, or remove existing data.
"""
import argparse
import base64
import getpass
import hashlib
import json
import os
import re
import secrets
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def parse_env(path):
    values = {}
    if path.is_file():
        for line in path.read_text(encoding='utf-8-sig').splitlines():
            line = line.strip()
            if not line or line.startswith('#') or '=' not in line:
                continue
            key, value = line.split('=', 1)
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
                if value[0] == '"':
                    try:
                        value = json.loads(value)
                    except ValueError:
                        value = value[1:-1]
                else:
                    value = value[1:-1].replace("\\'", "'")
            values[key.strip()] = value
    return values


def ssha(password, salt=None):
    """nginx RFC2307 SSHA; generated passwords have high random entropy."""
    salt = salt if salt is not None else secrets.token_bytes(16)
    encoded = password.encode('utf-8')
    return '{SSHA}' + base64.b64encode(hashlib.sha1(encoded + salt).digest() + salt).decode('ascii')


def default_data_root(project_root):
    if os.name == 'nt' and Path('E:/').is_dir():
        return Path('E:/IMagerBOT-1-runtime/data')
    return project_root / '.local' / 'data'


def dotenv_literal(value):
    text = str(value)
    if '\n' in text or '\r' in text:
        raise ValueError('Compose environment values must not contain newlines')
    # Compose treats single-quoted values literally, including $ in passwords.
    return "'" + text.replace("'", "\\'") + "'"


def prepare(project_root=ROOT, data_root=None, ui_user='imager', ui_password=None, rotate_ui=False):
    project_root = Path(project_root).resolve()
    env_path = project_root / 'infra' / '.env'
    values = parse_env(env_path)
    existing_bot = parse_env(project_root / 'telegram_bot' / '.env')
    secret_root = project_root / '.local' / 'secrets'
    secret_root.mkdir(parents=True, exist_ok=True)
    credential_path = secret_root / 'ui-credentials.json'
    auth_path = secret_root / 'temporal-ui.htpasswd'

    if data_root is None:
        configured_data_root = values.get('IMAGER_DATA_ROOT')
        if configured_data_root:
            data_root = Path(configured_data_root)
            if not data_root.is_absolute():
                # Compose resolves this environment path from infra/, regardless
                # of the directory from which setup was invoked.
                data_root = project_root / 'infra' / data_root
        else:
            data_root = default_data_root(project_root)
    data_root = Path(data_root).resolve()
    if data_root == Path(data_root.anchor):
        raise ValueError('Use a dedicated project data directory, not a drive/filesystem root')
    for directory in ('mongo', 'minio', 'temporal', 'scratch', 'bot-scratch', 'client-tmp'):
        (data_root / directory).mkdir(parents=True, exist_ok=True)

    if credential_path.is_file() and not rotate_ui:
        credential = json.loads(credential_path.read_text(encoding='utf-8'))
    else:
        if auth_path.exists() and not rotate_ui:
            raise ValueError('UI password metadata is missing; use --rotate-ui to replace the password explicitly')
        if not re.fullmatch(r'[A-Za-z0-9_.-]{1,64}', ui_user):
            raise ValueError('UI username must contain 1-64 letters, digits, dot, dash or underscore')
        password = ui_password if ui_password is not None else secrets.token_urlsafe(24)
        if len(password) < 16 or '\n' in password or '\r' in password:
            raise ValueError('UI password must contain at least 16 characters and no newlines')
        credential = {'username': ui_user, 'password': password}
        credential_path.write_text(json.dumps(credential, indent=2) + '\n', encoding='utf-8')
    if rotate_ui or not auth_path.is_file():
        auth_path.write_text(
            credential['username'] + ':' + ssha(credential['password']) + '\n', encoding='utf-8',
        )

    def configured_secret(name, length):
        current = values.get(name, '')
        return current if current and not current.startswith('replace_') else secrets.token_urlsafe(length)

    values.update({
        'IMAGER_DATA_ROOT': data_root.as_posix(),
        'IMAGER_SECRETS_ROOT': secret_root.as_posix(),
        'MONGO_PASSWORD': configured_secret('MONGO_PASSWORD', 30),
        'MINIO_ACCESS_KEY': configured_secret('MINIO_ACCESS_KEY', 15),
        'MINIO_SECRET_KEY': configured_secret('MINIO_SECRET_KEY', 30),
        'TEMPORAL_UI_PORT': values.get('TEMPORAL_UI_PORT', '8088'),
        'MINIO_CONSOLE_PORT': values.get('MINIO_CONSOLE_PORT', '9001'),
        'API_TOKEN': values.get('API_TOKEN') or existing_bot.get('API_TOKEN', existing_bot.get('api_token', '')),
        'TELEGRAM_ADMIN_IDS': values.get('TELEGRAM_ADMIN_IDS') or existing_bot.get('TELEGRAM_ADMIN_IDS', existing_bot.get('telegram_admin_ids', '')),
        'IMAGER_IMPORT_ROOT': values.get('IMAGER_IMPORT_ROOT') or (project_root / 'imager' / 'files' / 'groups').as_posix(),
    })
    env_path.parent.mkdir(parents=True, exist_ok=True)
    # Single quotes preserve spaces and prevent Compose expansion of secret $.
    env_path.write_text(
        '# Generated local configuration. Do not commit.\n' + ''.join(
            f'{key}={dotenv_literal(value)}\n' for key, value in values.items()
        ), encoding='utf-8',
    )
    readable = secret_root / 'local-credentials.txt'
    readable.write_text(
        f'Temporal UI: http://127.0.0.1:{values["TEMPORAL_UI_PORT"]}\n'
        f'Username: {credential["username"]}\nPassword: {credential["password"]}\n\n'
        f'MinIO Console: http://127.0.0.1:{values["MINIO_CONSOLE_PORT"]}\n'
        f'Username: {values["MINIO_ACCESS_KEY"]}\nPassword: {values["MINIO_SECRET_KEY"]}\n',
        encoding='utf-8',
    )
    for path in (env_path, auth_path, credential_path, readable):
        try:
            # nginx workers must read the hash file after dropping privileges.
            # Plaintext secrets remain owner-readable only.
            path.chmod(0o644 if path == auth_path else 0o600)
        except OSError:
            pass
    return {'env': env_path, 'credentials': readable, 'data': data_root}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data-root', type=Path)
    parser.add_argument('--ui-user', default='imager')
    parser.add_argument('--prompt-ui-password', action='store_true')
    parser.add_argument('--rotate-ui', action='store_true')
    args = parser.parse_args()
    password = getpass.getpass('Temporal UI password (at least 16 characters): ') if args.prompt_ui_password else None
    result = prepare(
        data_root=args.data_root, ui_user=args.ui_user,
        ui_password=password, rotate_ui=args.rotate_ui,
    )
    print(f'Compose environment: {result["env"]}')
    print(f'Credentials saved locally: {result["credentials"]}')
    print(f'Dedicated project data: {result["data"]}')
    print('No images pulled and no services started.')


if __name__ == '__main__':
    main()
