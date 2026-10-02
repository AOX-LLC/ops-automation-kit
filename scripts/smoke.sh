#!/usr/bin/env bash
# End-to-end check of the compose stack:
#   1. clean boot, 2. seeded data visible in n8n, Mailpit and Postgres,
#   3. approval round-trip through n8n's Wait node and the approver page,
#   4. a second boot that changes nothing.
# Secrets stay in shell variables and are never echoed.
set -euo pipefail

N8N="http://127.0.0.1:${KIT_N8N_PORT:-4300}"
API="http://127.0.0.1:${KIT_API_PORT:-4301}"
MAILPIT="http://127.0.0.1:${KIT_MAILPIT_WEB_PORT:-4303}"
WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT

step() { printf '\n== %s\n' "$*"; }
fail() { printf 'FAIL: %s\n' "$*" >&2; docker compose logs --no-color > smoke-logs.txt 2>&1 || true; exit 1; }
sql() { docker compose exec -T postgres psql -U postgres -d opskit -tAc "$1"; }
json() { python3 -c "import json,sys; d=json.load(sys.stdin); print($1)"; }
wait_for() {  # wait_for <seconds> <description> <command...>
    local deadline=$((SECONDS + $1)) what=$2; shift 2
    until "$@" >/dev/null 2>&1; do
        [ $SECONDS -lt $deadline ] || fail "timed out waiting for $what"
        sleep 2
    done
}
mail_count() { curl -sf "$MAILPIT/api/v1/messages?limit=1" | json 'd["total"]'; }
expected_sample_files() {
    find samples -type f \( -path 'samples/receipts/*' -o -path 'samples/leads/*' -o -path 'samples/crm/*' -o -path 'samples/inbox/*' \) | wc -l
}

check_seeded_data() {
    local mails=$1
    [ "$(sql 'select count(*) from core.sample_files')" = "$(expected_sample_files | tr -d ' ')" ] \
        || fail "core.sample_files does not match the files under samples/"
    [ "$(sql 'select count(*) from crm.accounts')" = "5" ] || fail "crm.accounts should hold 5 rows"
    [ "$(mail_count)" = "$mails" ] || fail "Mailpit should hold $mails messages, has $(mail_count)"
    echo "postgres: $(sql 'select count(*) from core.sample_files') sample files, 5 CRM accounts; mailpit: $mails messages"
}

if [ "${1:-}" = "--clean" ]; then
    step "clean boot (docker compose down -v)"
    docker compose down -v --remove-orphans
fi

step "boot"
KIT_GIT_COMMIT=$(git rev-parse HEAD 2>/dev/null || true)
KIT_GIT_BRANCH=$(git rev-parse --abbrev-ref HEAD 2>/dev/null || true)
export KIT_GIT_COMMIT KIT_GIT_BRANCH
docker compose up -d --wait $([ "${KIT_SKIP_BUILD:-0}" = "1" ] || echo --build) || fail "stack did not become healthy"
curl -sf "$API/healthz" >/dev/null || fail "helper API is not answering"
curl -sf "$N8N/healthz/readiness" >/dev/null || fail "n8n is not ready"

eval "$(docker compose run --rm -T kit-login python -m opskit.bootstrap.show_login --env)"

step "seeded data"
check_seeded_data 25

step "n8n owner login and workflows"
curl -sf -c "$WORK/n8n.jar" -H 'content-type: application/json' \
    -d "{\"emailOrLdapLoginId\":\"owner@kit.example\",\"password\":\"$KIT_OWNER_PASSWORD\"}" \
    "$N8N/rest/login" >/dev/null || fail "n8n owner login failed"
curl -sf -b "$WORK/n8n.jar" "$N8N/rest/workflows" > "$WORK/workflows.json"
ids=$(json '",".join(sorted(w["id"] for w in d["data"]))' < "$WORK/workflows.json")
[ "$ids" = "inbox00000000001,kitSmoke00000001,leads00000000001,receipts00000001" ] || fail "unexpected workflows: $ids"
active=$(json '",".join(sorted(w["id"] for w in d["data"] if w.get("active")))' < "$WORK/workflows.json")
[ "$active" = "kitSmoke00000001" ] || fail "only the smoke workflow should be published, got: $active"
echo "workflows: $ids (published: $active)"

step "approval round-trip"
trigger() { [ "$(curl -s -o /dev/null -w '%{http_code}' -X POST -H "X-Kit-Token: $KIT_WEBHOOK_TOKEN" "$N8N/webhook/kit-smoke")" = "200" ]; }
wait_for 90 "the smoke webhook to register" trigger

csrf_from() { sed -n 's/.*name="csrf_token" value="\([^"]*\)".*/\1/p' "$1" | head -1; }
curl -sf -c "$WORK/approver.jar" -b "$WORK/approver.jar" "$API/approver/login" > "$WORK/login.html"
curl -sf -o /dev/null -c "$WORK/approver.jar" -b "$WORK/approver.jar" \
    --data-urlencode "password=$KIT_APPROVER_PASSWORD" --data-urlencode "csrf_token=$(csrf_from "$WORK/login.html")" \
    "$API/approver/login" || fail "approver login failed"

pending() { curl -sf -b "$WORK/approver.jar" "$API/approver/" | grep -o '/approver/approvals/[0-9a-f-]*' | head -1 > "$WORK/approval"; [ -s "$WORK/approval" ]; }
wait_for 60 "the smoke approval to appear on the approver page" pending
approval_path=$(cat "$WORK/approval")
curl -sf -b "$WORK/approver.jar" "$API$approval_path" > "$WORK/detail.html"
status=$(curl -s -o /dev/null -w '%{http_code}' -b "$WORK/approver.jar" \
    --data-urlencode "decision=approved" --data-urlencode "csrf_token=$(csrf_from "$WORK/detail.html")" \
    "$API$approval_path/decision")
[ "$status" = "303" ] || fail "decision POST returned $status"

run_done() { [ "$(sql "select status from core.runs where workflow = 'kit_smoke' order by started_at desc limit 1")" = "succeeded" ]; }
wait_for 90 "the smoke run to resume and finish" run_done
for action in approval.requested approval.decided approval.resumed model.call; do
    [ "$(sql "select count(*) from core.audit_log where action = '$action'")" -ge 1 ] || fail "no $action audit row"
done
wait_for 30 "the Send Email node's message in Mailpit" test "$(mail_count)" = 26
echo "approved on the page, n8n resumed, audit rows written, confirmation email captured"

step "second boot without -v"
docker compose up -d --wait || fail "second boot did not become healthy"
check_seeded_data 26
if docker compose logs --no-color n8n-import | grep -q WARNING; then
    fail "second boot re-imported a workflow that did not change"
fi
echo "second boot: same data, no workflow re-imported"

printf '\nSMOKE PASSED\n'
