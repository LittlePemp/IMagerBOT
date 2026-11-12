#!/bin/sh
set -eu

# Image and schema files have the same pinned Temporal Server version.
# setup-schema without --overwrite is idempotent and never drops user data.
for schema in temporal visibility; do
    if [ "$schema" = temporal ]; then
        export SQL_DATABASE="$TEMPORAL_DATABASE"
    else
        export SQL_DATABASE="$TEMPORAL_VISIBILITY_DATABASE"
    fi
    temporal-sql-tool setup-schema -v 0.0
    temporal-sql-tool update-schema -d "/etc/temporal/schema/postgresql/v12/$schema/versioned"
done
