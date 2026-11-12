#!/bin/sh
set -eu
# PostgreSQL rejects loose key permissions on Windows bind mounts. Copy only
# this container's server identity to ephemeral storage and restrict ownership.
mkdir -p /run/postgres-tls
cp /run/secrets/postgres_certificate /run/postgres-tls/server.crt
cp /run/secrets/postgres_private_key /run/postgres-tls/server.key
chown -R postgres:postgres /run/postgres-tls
chmod 600 /run/postgres-tls/server.key
exec docker-entrypoint.sh postgres \
    -c ssl=on \
    -c ssl_cert_file=/run/postgres-tls/server.crt \
    -c ssl_key_file=/run/postgres-tls/server.key \
    -c hba_file=/etc/postgresql/pg_hba.conf
