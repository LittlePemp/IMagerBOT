"""Create project-local certificates with an existing OpenSSL executable."""
import os
import secrets
import shutil
import subprocess
import tempfile
from pathlib import Path


def find_openssl(explicit=None):
    candidate = explicit or shutil.which('openssl')
    if not candidate and os.name == 'nt':
        installed = Path(os.environ.get('ProgramFiles', '')) / 'Git/usr/bin/openssl.exe'
        if installed.is_file():
            candidate = str(installed)
    if not candidate:
        raise ValueError('OpenSSL is required for production TLS; install it or pass --openssl PATH')
    return str(candidate)


def ensure_certificates(directory, policy, openssl=None):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    names = (
        policy.ca_certificate, policy.ca_private_key, policy.server_certificate,
        policy.server_private_key, policy.client_certificate, policy.client_private_key,
        policy.postgres_certificate, policy.postgres_private_key,
    )
    binary = find_openssl(openssl)

    def run(*arguments):
        result = subprocess.run([binary, *map(str, arguments)], capture_output=True, text=True)
        if result.returncode:
            # OpenSSL diagnostics contain paths and certificate metadata, not
            # key material. No generated password or private key is printed.
            raise ValueError(f'OpenSSL certificate operation failed: {result.stderr.strip()}')
        return result.stdout

    existing = [name for name in names if (directory / name).is_file()]
    if existing:
        if len(existing) != len(names):
            raise ValueError('Production TLS files are incomplete; restore the complete certificate set before starting')
        for name in (policy.ca_certificate, policy.server_certificate,
                     policy.client_certificate, policy.postgres_certificate):
            run('x509', '-in', directory / name, '-noout', '-checkend', '0')
        identities = (
            (policy.ca_certificate, policy.ca_private_key, None, None),
            (policy.server_certificate, policy.server_private_key, 'sslserver', 'temporal'),
            (policy.client_certificate, policy.client_private_key, 'sslclient', None),
            (policy.postgres_certificate, policy.postgres_private_key, 'sslserver', 'postgres'),
        )
        for certificate, private_key, purpose, hostname in identities:
            cert, key = directory / certificate, directory / private_key
            certificate_public_key = run('x509', '-in', cert, '-pubkey', '-noout').strip()
            private_public_key = run('pkey', '-in', key, '-pubout').strip()
            if certificate_public_key != private_public_key:
                raise ValueError(f'Production certificate does not match its private key: {certificate}')
            arguments = ['verify', '-CAfile', directory / policy.ca_certificate]
            if purpose:
                arguments.extend(['-purpose', purpose])
            if hostname:
                arguments.extend(['-verify_hostname', hostname])
            run(*arguments, cert)
        return directory

    with tempfile.TemporaryDirectory(prefix='.tls-new-', dir=directory.parent) as temporary:
        temporary = Path(temporary)
        ca_key = temporary / policy.ca_private_key
        ca_cert = temporary / policy.ca_certificate
        run('genpkey', '-algorithm', 'RSA', '-pkeyopt', f'rsa_keygen_bits:{policy.rsa_key_bits}', '-out', ca_key)
        run('req', '-new', '-x509', '-sha256', '-key', ca_key, '-out', ca_cert,
            '-days', policy.ca_certificate_days, '-subj', '/CN=IMager production CA',
            '-addext', 'basicConstraints=critical,CA:TRUE,pathlen:0',
            '-addext', 'keyUsage=critical,keyCertSign,cRLSign')
        identities = (
            ('temporal', policy.server_certificate, policy.server_private_key,
             'serverAuth,clientAuth', 'DNS:temporal,DNS:localhost,IP:127.0.0.1'),
            ('imager-client', policy.client_certificate, policy.client_private_key, 'clientAuth', ''),
            ('postgres', policy.postgres_certificate, policy.postgres_private_key, 'serverAuth', 'DNS:postgres'),
        )
        for common_name, certificate, private_key, usages, sans in identities:
            key, cert = temporary / private_key, temporary / certificate
            request, extensions = temporary / f'{common_name}.csr', temporary / f'{common_name}.ext'
            run('genpkey', '-algorithm', 'RSA', '-pkeyopt', f'rsa_keygen_bits:{policy.rsa_key_bits}', '-out', key)
            run('req', '-new', '-sha256', '-key', key, '-out', request, '-subj', f'/CN={common_name}')
            extensions.write_text(
                'basicConstraints=critical,CA:FALSE\nkeyUsage=critical,digitalSignature,keyEncipherment\n'
                f'extendedKeyUsage={usages}\n' + (f'subjectAltName={sans}\n' if sans else ''),
                encoding='ascii',
            )
            run('x509', '-req', '-sha256', '-in', request, '-CA', ca_cert, '-CAkey', ca_key,
                '-set_serial', f'0x{secrets.token_hex(16)}', '-days', policy.certificate_days,
                '-extfile', extensions, '-out', cert)
            run('verify', '-CAfile', ca_cert, cert)
        for name in names:
            destination = directory / name
            (temporary / name).replace(destination)
            destination.chmod(0o600 if name in (
                policy.ca_private_key, policy.server_private_key,
                policy.client_private_key, policy.postgres_private_key,
            ) else 0o644)
    return directory
