#!/bin/sh
set -eu
psql --set=ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname postgres \
    --set=app_user="$TEMPORAL_POSTGRES_USER" --set=app_password="$TEMPORAL_POSTGRES_PASSWORD" \
    --set=main_db="$TEMPORAL_DATABASE" --set=visibility_db="$TEMPORAL_VISIBILITY_DATABASE" <<'SQL'
SELECT format('CREATE ROLE %I LOGIN PASSWORD %L NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION', :'app_user', :'app_password') \gexec
SELECT format('CREATE DATABASE %I OWNER %I', :'main_db', :'app_user') \gexec
SELECT format('CREATE DATABASE %I OWNER %I', :'visibility_db', :'app_user') \gexec
SQL
