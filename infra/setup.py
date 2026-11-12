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
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from imager_config.loader import CONFIG_OVERRIDE_VARIABLE, DEFAULT_CONFIG_PATH, config


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
    return project_root / config.paths.data_root


def dotenv_literal(value):
    text = str(value)
    if '\n' in text or '\r' in text:
        raise ValueError('Compose environment values must not contain newlines')
    # Compose treats single-quoted values literally, including $ in passwords.
    return "'" + text.replace("'", "\\'") + "'"


def prepare(project_root=ROOT, data_root=None, ui_user=None, ui_password=None, rotate_ui=False,
            production=False, openssl=None):
    project_root = Path(project_root).resolve()
    production_config = config.deployment.production if production else None
    env_path = project_root / (production_config.env_file if production else config.paths.compose_env)
    values = parse_env(env_path)
    development_env = project_root / config.paths.compose_env
    development_values = parse_env(development_env) if production else {}
    existing_bot = parse_env(project_root / config.paths.legacy_bot_env)
    secret_root = project_root / config.paths.secrets_root
    if production:
        secret_root /= production_config.secrets_directory
    secret_root.mkdir(parents=True, exist_ok=True)
    credential_path = secret_root / config.paths.ui_credentials
    auth_path = secret_root / config.paths.ui_password_hash
    ui_user = config.auth.ui_username if ui_user is None else ui_user

    if data_root is None:
        configured_data_root = values.get('IMAGER_DATA_ROOT')
        if configured_data_root:
            data_root = Path(configured_data_root)
            if not data_root.is_absolute():
                # Compose resolves this environment path from infra/, regardless
                # of the directory from which setup was invoked.
                data_root = env_path.parent / data_root
        else:
            if production and development_values.get('IMAGER_DATA_ROOT'):
                base = Path(development_values['IMAGER_DATA_ROOT'])
                if not base.is_absolute():
                    base = development_env.parent / base
            else:
                base = default_data_root(project_root)
            data_root = base / production_config.data_directory if production else base
    data_root = Path(data_root).resolve()
    if data_root == Path(data_root.anchor):
        raise ValueError('Use a dedicated project data directory, not a drive/filesystem root')
    if production:
        development_root = Path(development_values.get('IMAGER_DATA_ROOT') or default_data_root(project_root))
        if not development_root.is_absolute():
            development_root = development_env.parent / development_root
        development_root = development_root.resolve()
        role_directories = (
            config.paths.mongo_directory, config.paths.minio_directory,
            config.paths.temporal_directory, config.paths.worker_scratch_directory,
            config.paths.bot_scratch_directory, config.paths.client_temp_directory,
            config.paths.catalog_import_scratch_directory,
            config.paths.bot_api_directory, config.paths.bot_api_temp_directory,
        )
        for old in (development_root / name for name in role_directories):
            for new in (data_root / name for name in (*role_directories, production_config.postgres_directory)):
                if old.is_relative_to(new) or new.is_relative_to(old):
                    raise ValueError('Production data must not overlap development service directories')
    for directory in (
        config.paths.mongo_directory, config.paths.minio_directory, config.paths.temporal_directory,
        config.paths.worker_scratch_directory, config.paths.bot_scratch_directory,
        config.paths.client_temp_directory, config.paths.catalog_import_scratch_directory,
        config.paths.bot_api_directory, config.paths.bot_api_temp_directory,
    ):
        (data_root / directory).mkdir(parents=True, exist_ok=True)

    if credential_path.is_file() and not rotate_ui:
        credential = json.loads(credential_path.read_text(encoding='utf-8'))
    else:
        if auth_path.exists() and not rotate_ui:
            raise ValueError('UI password metadata is missing; use --rotate-ui to replace the password explicitly')
        if not re.fullmatch(r'[A-Za-z0-9_.-]{1,64}', ui_user):
            raise ValueError('UI username must contain 1-64 letters, digits, dot, dash or underscore')
        password = ui_password if ui_password is not None else secrets.token_urlsafe(config.auth.ui_password_random_bytes)
        if len(password) < config.auth.ui_password_min_length or '\n' in password or '\r' in password:
            raise ValueError(f'UI password must contain at least {config.auth.ui_password_min_length} characters and no newlines')
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
        'MONGO_PASSWORD': configured_secret('MONGO_PASSWORD', config.auth.mongo_password_random_bytes),
        'MINIO_ACCESS_KEY': configured_secret('MINIO_ACCESS_KEY', config.auth.minio_access_random_bytes),
        'MINIO_SECRET_KEY': configured_secret('MINIO_SECRET_KEY', config.auth.minio_secret_random_bytes),
        'TEMPORAL_UI_PORT': values.get('TEMPORAL_UI_PORT') or str(
            production_config.temporal_ui_port if production else config.deployment.temporal_ui_port),
        'MINIO_CONSOLE_PORT': values.get('MINIO_CONSOLE_PORT') or str(
            production_config.minio_console_port if production else config.deployment.minio_console_port),
        'API_TOKEN': values.get('API_TOKEN') or existing_bot.get('API_TOKEN', existing_bot.get('api_token', '')),
        'TELEGRAM_ADMIN_IDS': values.get('TELEGRAM_ADMIN_IDS') or existing_bot.get('TELEGRAM_ADMIN_IDS', existing_bot.get('telegram_admin_ids', '')),
        # Telegram application credentials are supplied by the operator. They
        # cannot be randomly generated like Mongo/MinIO passwords.
        'TELEGRAM_API_ID': values.get('TELEGRAM_API_ID') or existing_bot.get('TELEGRAM_API_ID', ''),
        'TELEGRAM_API_HASH': values.get('TELEGRAM_API_HASH') or existing_bot.get('TELEGRAM_API_HASH', ''),
        'IMAGER_IMPORT_ROOT': values.get('IMAGER_IMPORT_ROOT') or (project_root / config.paths.image_groups).as_posix(),
        'IMAGER_MONGO_DATA_PATH': (data_root / config.paths.mongo_directory).as_posix(),
        'IMAGER_MINIO_DATA_PATH': (data_root / config.paths.minio_directory).as_posix(),
        'IMAGER_TEMPORAL_DATA_PATH': (data_root / config.paths.temporal_directory).as_posix(),
        'IMAGER_WORKER_SCRATCH_PATH': (data_root / config.paths.worker_scratch_directory).as_posix(),
        'IMAGER_BOT_SCRATCH_PATH': (data_root / config.paths.bot_scratch_directory).as_posix(),
        'IMAGER_IMPORT_SCRATCH_PATH': (data_root / config.paths.catalog_import_scratch_directory).as_posix(),
        'IMAGER_BOT_API_DATA_PATH': (data_root / config.paths.bot_api_directory).as_posix(),
        'IMAGER_BOT_API_TEMP_PATH': (data_root / config.paths.bot_api_temp_directory).as_posix(),
        'IMAGER_TEMPORAL_UI_AUTH_FILE': auth_path.as_posix(),
        'IMAGER_TEMPORAL_NAMESPACE': config.temporal.namespace,
    })
    selected_config = os.environ.get(CONFIG_OVERRIDE_VARIABLE)
    if selected_config:
        selected_config = Path(selected_config).resolve()
    elif values.get('IMAGER_CONFIG_SOURCE'):
        selected_config = Path(values['IMAGER_CONFIG_SOURCE'])
        if not selected_config.is_absolute():
            selected_config = env_path.parent / selected_config
        selected_config = selected_config.resolve()
    else:
        selected_config = DEFAULT_CONFIG_PATH
    values['IMAGER_CONFIG_SOURCE'] = selected_config.as_posix()
    if production:
        from infra.certificates import ensure_certificates
        tls_root = ensure_certificates(secret_root / production_config.tls_directory, production_config, openssl)
        postgres_root = data_root / production_config.postgres_directory
        postgres_root.mkdir(parents=True, exist_ok=True)
        values.update({
            'POSTGRES_PASSWORD': configured_secret('POSTGRES_PASSWORD', production_config.postgres_password_random_bytes),
            'TEMPORAL_POSTGRES_PASSWORD': configured_secret('TEMPORAL_POSTGRES_PASSWORD', production_config.postgres_password_random_bytes),
            'TEMPORAL_POSTGRES_USER': production_config.postgres_user,
            'TEMPORAL_DATABASE': production_config.postgres_database,
            'TEMPORAL_VISIBILITY_DATABASE': production_config.visibility_database,
            'IMAGER_POSTGRES_DATA_PATH': postgres_root.as_posix(),
            'IMAGER_TLS_CA_FILE': (tls_root / production_config.ca_certificate).as_posix(),
            'IMAGER_TLS_SERVER_CERT_FILE': (tls_root / production_config.server_certificate).as_posix(),
            'IMAGER_TLS_SERVER_KEY_FILE': (tls_root / production_config.server_private_key).as_posix(),
            'IMAGER_TLS_CLIENT_CERT_FILE': (tls_root / production_config.client_certificate).as_posix(),
            'IMAGER_TLS_CLIENT_KEY_FILE': (tls_root / production_config.client_private_key).as_posix(),
            'IMAGER_TLS_POSTGRES_CERT_FILE': (tls_root / production_config.postgres_certificate).as_posix(),
            'IMAGER_TLS_POSTGRES_KEY_FILE': (tls_root / production_config.postgres_private_key).as_posix(),
            'TEMPORAL_HISTORY_SHARDS': str(production_config.history_shards),
            'IMAGER_RUNTIME_UID': str(os.getuid() if hasattr(os, 'getuid') else 1000),
            'IMAGER_RUNTIME_GID': str(os.getgid() if hasattr(os, 'getgid') else 1000),
        })
    env_path.parent.mkdir(parents=True, exist_ok=True)
    # Single quotes preserve spaces and prevent Compose expansion of secret $.
    env_path.write_text(
        '# Generated local configuration. Do not commit.\n' + ''.join(
            f'{key}={dotenv_literal(value)}\n' for key, value in values.items()
        ), encoding='utf-8',
    )
    readable = secret_root / config.paths.readable_credentials
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
    parser.add_argument('--ui-user')
    parser.add_argument('--prompt-ui-password', action='store_true')
    parser.add_argument('--rotate-ui', action='store_true')
    parser.add_argument('--production', action='store_true')
    parser.add_argument('--openssl', help='Existing OpenSSL executable for production TLS certificates')
    args = parser.parse_args()
    password = getpass.getpass(f'Temporal UI password (at least {config.auth.ui_password_min_length} characters): ') if args.prompt_ui_password else None
    result = prepare(
        data_root=args.data_root, ui_user=args.ui_user,
        ui_password=password, rotate_ui=args.rotate_ui,
        production=args.production, openssl=args.openssl,
    )
    print(f'Compose environment: {result["env"]}')
    print(f'Credentials saved locally: {result["credentials"]}')
    print(f'Dedicated project data: {result["data"]}')
    print('No images pulled and no services started.')


if __name__ == '__main__':
    main()
