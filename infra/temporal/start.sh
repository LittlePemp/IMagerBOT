#!/bin/sh
set -eu
export TEMPORAL_BROADCAST_ADDRESS="$(hostname -i | awk '{print $1}')"
exec temporal-server --config-file /etc/temporal/production.yaml --allow-no-auth start
