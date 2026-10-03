#!/bin/sh
# Import credentials and workflows into n8n's database before the server starts.
#
# Credentials are generated from secrets, so they are re-imported on every boot.
# A workflow is re-imported only when its committed JSON changed since the last import
# (or FORCE_REIMPORT=1), so work done in the editor is never replaced silently.
set -eu

WORKFLOWS=/workflows
STATE=/home/node/.n8n/kit-import-state
PUBLISHED_IDS="kitSmoke00000001 receipts00000001 inbox00000000001"
WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT INT TERM

mkdir -p "$STATE"

node /kit/render-credentials.js > "$WORK/credentials.json"
n8n import:credentials --input="$WORK/credentials.json"
rm -f "$WORK/credentials.json"

imported=0
for file in "$WORKFLOWS"/*.json; do
    id=$(node -e 'process.stdout.write(require(process.argv[1]).id)' "$file")
    name=$(basename "$file")
    current=$(sha256sum "$file" | cut -d' ' -f1)
    recorded=$(cat "$STATE/$id.sha256" 2>/dev/null || true)

    if [ -z "$recorded" ]; then
        echo "INFO  importing $name (first import)"
    elif [ "${FORCE_REIMPORT:-0}" = "1" ]; then
        echo "WARNING $name: forced re-import; editor changes to this workflow were replaced"
    elif [ "$recorded" != "$current" ]; then
        echo "WARNING $name: committed JSON changed since last import; editor changes to this workflow were replaced"
    else
        echo "INFO  $name unchanged since last import; keeping the editor's version"
        continue
    fi

    cp "$file" "$WORK/$name"
    n8n import:workflow --input="$WORK/$name"
    case " $PUBLISHED_IDS " in
        *" $id "*) n8n publish:workflow --id="$id" ;;
    esac
    echo "$current" > "$STATE/$id.sha256"
    imported=$((imported + 1))
done
echo "INFO  $imported workflow(s) imported"
