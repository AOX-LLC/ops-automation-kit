#!/bin/sh
# Print the build identity for /healthz as shell assignments: eval "$(scripts/build_identity.sh)".
# A dirty tree or a detached HEAD reports an empty value (null in /healthz), never a guess.
commit=""
branch=""
if git rev-parse --git-dir >/dev/null 2>&1; then
    if [ -z "$(git status --porcelain --untracked-files=no 2>/dev/null)" ]; then
        commit=$(git rev-parse HEAD)
    fi
    branch=$(git symbolic-ref -q --short HEAD || true)
fi
printf 'export KIT_GIT_COMMIT=%s KIT_GIT_BRANCH=%s\n' "$commit" "$branch"
