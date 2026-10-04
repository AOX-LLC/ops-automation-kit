#!/usr/bin/env bash
# Record the walkthrough for each theme (default: light dark) against a clean, seeded stack.
# Anything that starts containers takes the shared lock around the whole boot-record-stop run.
#   demo/record.sh [light] [dark]
# A workflow only processes its pending items once, so every theme starts from `down -v` and a
# fresh seed. The stack is stopped afterwards.
set -euo pipefail
cd "$(dirname "$0")/.."
export KIT_SCHEDULED_RUNS=false  # runs happen when the script triggers them, not on a clock boundary
LOCK=${KIT_DOCKER_LOCK:-$HOME/portfolio-projects/.locks/docker}
themes=("$@"); [ ${#themes[@]} -gt 0 ] || themes=(light dark)

record_all() {
    for theme in "${themes[@]}"; do
        echo "== $theme: clean boot"
        docker compose down -v --remove-orphans
        eval "$(scripts/build_identity.sh)"
        docker compose up -d --wait
        set -a; eval "$(docker compose run --rm -T kit-login python -m opskit.bootstrap.show_login --env)"; set +a
        echo "== $theme: record"
        THEME=$theme node demo/walkthrough.ts
    done
    docker compose down
}
exec flock "$LOCK" bash -c "$(declare -f record_all); themes=(${themes[*]}); cd '$PWD'; record_all"
