#!/usr/bin/env bash
# Record the quickstart clip under the docker lock, each theme from a clean stack.
# Usage: demo/terminal/record-quickstart.sh [dark|light ...]   (default: both)
# Boots the stack once per theme (minutes each). Always leaves the stack down.
set -euo pipefail

here=$(cd "$(dirname "$0")" && pwd)
repo=$(git -C "$here" rev-parse --show-toplevel)
lock=$HOME/portfolio-projects/.locks/docker
mkdir -p "$(dirname "$lock")"
themes=("$@")
[ ${#themes[@]} -gt 0 ] || themes=(dark light)

exec 9>"$lock"
flock 9
cd "$repo"
trap 'docker compose down >/dev/null 2>&1 || true' EXIT
for theme in "${themes[@]}"; do
  docker compose down -v --remove-orphans   # same effect as make clean: a true first boot
  "$here/render.sh" "$theme" quickstart     # the tape runs `up -d --wait` and `ps`
  docker compose down
done
