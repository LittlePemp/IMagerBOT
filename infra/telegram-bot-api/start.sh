#!/bin/sh
set -eu

# Application credentials come from my.telegram.org, not the BotFather token.
# Validate only on service startup so a deployment without the bot profile can
# still be configured before those operator-owned credentials are supplied.
: "${TELEGRAM_API_ID:?Configure TELEGRAM_API_ID in the project Compose environment}"
: "${TELEGRAM_API_HASH:?Configure TELEGRAM_API_HASH in the project Compose environment}"
: "${TELEGRAM_WORK_DIR:?Telegram work directory is required}"
: "${TELEGRAM_TEMP_DIR:?Telegram temporary directory is required}"

mkdir -p "$TELEGRAM_WORK_DIR" "$TELEGRAM_TEMP_DIR"
# The upstream entrypoint fixes only WORK_DIR ownership. Both dedicated bind
# mounts must be writable after the server drops to its unprivileged UID 101.
chown 101:101 "$TELEGRAM_WORK_DIR" "$TELEGRAM_TEMP_DIR"
exec /docker-entrypoint.sh
