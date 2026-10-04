#!/usr/bin/env bash
# Capture the README canvas stills for each theme (default: light dark). No video, no workflow runs.
# Takes the shared Docker lock around the whole boot, capture and stop sequence, like record.sh.
#   demo/canvas-stills.sh [light] [dark]
set -euo pipefail
cd "$(dirname "$0")/.."
LOCK=${KIT_DOCKER_LOCK:-${XDG_RUNTIME_DIR:-/tmp}/ops-kit-docker.lock}
mkdir -p "$(dirname "$LOCK")"
themes=("$@"); [ ${#themes[@]} -gt 0 ] || themes=(light dark)
for theme in "${themes[@]}"; do
    case "$theme" in light|dark) ;; *) echo "theme must be light or dark, got: $theme" >&2; exit 2 ;; esac
done

capture_all() {
    set -euo pipefail
    trap 'docker compose down' EXIT
    docker compose down -v --remove-orphans
    eval "$(scripts/build_identity.sh)"
    docker compose up -d --wait
    eval "$(docker compose run --rm -T kit-login python -m opskit.bootstrap.show_login --env | sed 's/^/export /')"
    for theme in "$@"; do
        echo "== $theme: canvas stills"
        THEME=$theme node demo/canvas-stills.ts
    done
    unset KIT_OWNER_PASSWORD KIT_APPROVER_PASSWORD KIT_WEBHOOK_TOKEN
}
exec flock "$LOCK" bash -c "$(declare -f capture_all); capture_all \"\$@\"" _ "${themes[@]}"
