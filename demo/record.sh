#!/usr/bin/env bash
# Record the walkthrough for each theme (default: light dark) against a clean, seeded stack.
# Anything that starts containers takes the shared lock around the whole boot-record-stop run.
#   demo/record.sh [light] [dark]
# A workflow only processes its pending items once, so every theme starts from `down -v` and a
# fresh seed. The stack is stopped afterwards, also when a take fails. KIT_DOCKER_LOCK names the
# lock file shared with whatever else starts containers on this machine.
set -euo pipefail
cd "$(dirname "$0")/.."
export KIT_SCHEDULED_RUNS=false  # runs happen when the script triggers them, not on a clock boundary
LOCK=${KIT_DOCKER_LOCK:-${XDG_RUNTIME_DIR:-/tmp}/ops-kit-docker.lock}
mkdir -p "$(dirname "$LOCK")"
themes=("$@"); [ ${#themes[@]} -gt 0 ] || themes=(light dark)
for theme in "${themes[@]}"; do
    case "$theme" in light|dark) ;; *) echo "theme must be light or dark, got: $theme" >&2; exit 2 ;; esac
done

record_all() {
    set -euo pipefail
    trap 'docker compose down' EXIT
    for theme in "$@"; do
        echo "== $theme: clean boot"
        docker compose down -v --remove-orphans
        eval "$(scripts/build_identity.sh)"
        docker compose up -d --wait
        echo "== $theme: record"
        # The secrets go into the recorder's environment only, never the whole script's.
        eval "$(docker compose run --rm -T kit-login python -m opskit.bootstrap.show_login --env | sed 's/^/export /')"
        THEME=$theme node demo/walkthrough.ts
        unset KIT_OWNER_PASSWORD KIT_APPROVER_PASSWORD KIT_WEBHOOK_TOKEN
    done
}
exec flock "$LOCK" bash -c "$(declare -f record_all); record_all \"\$@\"" _ "${themes[@]}"
