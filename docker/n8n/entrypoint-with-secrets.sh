#!/bin/sh
# n8n reads most secrets from *_FILE variables. The owner password hash has no _FILE form,
# so load it here and hand over to the stock entrypoint.
set -eu
N8N_INSTANCE_OWNER_PASSWORD_HASH="$(cat /run/kit-secrets/n8n_owner_password.bcrypt)"
export N8N_INSTANCE_OWNER_PASSWORD_HASH
exec tini -- /docker-entrypoint.sh "$@"
