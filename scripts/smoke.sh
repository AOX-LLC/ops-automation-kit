#!/usr/bin/env bash
# End-to-end check of the compose stack:
#   1. clean boot, 2. seeded data visible in n8n, Mailpit and Postgres,
#   3. approval round-trip through n8n's Wait node and the approver page,
#   4. the receipts workflow end to end in replay: extract, reconcile, spreadsheet, email,
#   5. the inbox workflow end to end in replay: triage, quarantine, drafts, approve one, reject one,
#   6. a second boot that changes nothing.
# Secrets stay in shell variables and are never echoed.
set -euo pipefail

# The schedules fire on clock boundaries, which would run a workflow before this script asks it to.
export KIT_SCHEDULED_RUNS=false

N8N="http://127.0.0.1:${KIT_N8N_PORT:-4300}"
API="http://127.0.0.1:${KIT_API_PORT:-4301}"
MAILPIT="http://127.0.0.1:${KIT_MAILPIT_WEB_PORT:-4303}"
WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT

step() { printf '\n== %s\n' "$*"; }
sql() { docker compose exec -T postgres psql -U postgres -d opskit -tAc "$1"; }
json() { python3 -c "import json,sys; d=json.load(sys.stdin); print($1)"; }

# What to look at when a step times out, printed before the full log is saved. A wait on an exact
# message count can fail because the count went past it (a workflow ran twice), which the count
# alone does not show: the runs, the n8n executions (how each was started) and the messages do.
diagnose() {
    set +e  # it runs just before the script exits 1: nothing in here may end it early
    local limits="-c statement_timeout=5000"  # a lock held on a table must not hang the report
    {
        echo "--- diagnostics at $(date -u +%FT%TZ) ---"
        echo "Mailpit messages: $(curl -sf -m 5 "$MAILPIT/api/v1/messages?limit=1" | json 'd["total"]' 2>/dev/null || echo '(Mailpit is not answering)')"
        echo "newest Mailpit messages (created | to | subject):"
        curl -sf -m 5 "$MAILPIT/api/v1/messages?limit=8" \
            | json '"\n".join(m["Created"][:19] + " | " + ",".join(t["Address"] for t in m["To"]) + " | " + m["Subject"][:60] for m in d["messages"])' 2>/dev/null || true
        echo "runs (workflow, status, started, finished):"
        docker compose exec -T -e PGOPTIONS="$limits" postgres psql -U postgres -d opskit -tAc "select workflow, status, to_char(started_at, 'HH24:MI:SS'), to_char(finished_at, 'HH24:MI:SS') from core.runs order by started_at" 2>&1 || true
        echo "n8n executions (workflow, mode, status, started, stopped); mode is webhook or trigger (a schedule):"
        docker compose exec -T -e PGOPTIONS="$limits" postgres psql -U postgres -d n8n -tAc "select \"workflowId\", mode, status, to_char(\"startedAt\", 'HH24:MI:SS'), to_char(\"stoppedAt\", 'HH24:MI:SS') from execution_entity order by id" 2>&1 || true
        echo "approvals by status:"
        docker compose exec -T -e PGOPTIONS="$limits" postgres psql -U postgres -d opskit -tAc "select status, count(*) from core.approvals group by 1 order by 1" 2>&1 || true
        echo "last service log lines (api, n8n, mailpit):"
        docker compose logs --no-color --tail 25 api n8n mailpit 2>&1 || true
        echo "--- end diagnostics ---"
    } >&2
}
fail() { printf 'FAIL: %s\n' "$*" >&2; diagnose; docker compose logs --no-color > smoke-logs.txt 2>&1 || true; exit 1; }
wait_for() {  # wait_for <seconds> <description> <command...>
    local deadline=$((SECONDS + $1)) what=$2; shift 2
    until "$@" >/dev/null 2>&1; do
        [ $SECONDS -lt $deadline ] || fail "timed out waiting for $what"
        sleep 2
    done
}
mail_count() { curl -sf "$MAILPIT/api/v1/messages?limit=1" | json 'd["total"]'; }
# How many messages match a Mailpit search ('from:...', 'subject:"..."'). The waits below look for
# the specific email a step should produce, so a stray extra message cannot hang them.
mail_matching() { curl -sf -G "$MAILPIT/api/v1/search" --data-urlencode "query=$1" | json 'd["messages_count"]'; }
mail_matching_is_at_least() { [ "$(mail_matching "$1")" -ge "$2" ]; }
wait_for_mail() {  # wait_for_mail <seconds> <description> <search query> [at least N, default 1]
    local deadline=$((SECONDS + $1)) what=$2 query=$3 want=${4:-1}
    until mail_matching_is_at_least "$query" "$want" >/dev/null 2>&1; do
        [ $SECONDS -lt $deadline ] || fail "timed out waiting for $what: $(mail_matching "$query" 2>/dev/null || echo '?') message(s) match [$query], wanted $want"
        sleep 2
    done
}
# The duplicate check, kept apart from the waits: exactly this many messages, never more.
assert_mail_exactly() {  # assert_mail_exactly <description> <search query> <count>
    local got
    got=$(mail_matching "$2") || fail "Mailpit search failed for $1"
    [ "$got" = "$3" ] || fail "$1: expected exactly $3 message(s) matching [$2], found $got (a duplicate was sent?)"
}
expected_sample_files() {
    find samples -type f \( -path 'samples/receipts/*' -o -path 'samples/leads/*' -o -path 'samples/crm/*' -o -path 'samples/inbox/*' \) | wc -l
}

check_seeded_data() {
    local mails=$1 accounts=${2:-5}
    [ "$(sql 'select count(*) from core.sample_files')" = "$(expected_sample_files | tr -d ' ')" ] \
        || fail "core.sample_files does not match the files under samples/"
    [ "$(sql 'select count(*) from crm.accounts')" = "$accounts" ] || fail "crm.accounts should hold $accounts rows"
    [ "$(mail_count)" = "$mails" ] || fail "Mailpit should hold $mails messages, has $(mail_count)"
    echo "postgres: $(sql 'select count(*) from core.sample_files') sample files, $accounts CRM accounts; mailpit: $mails messages"
}

if [ "${1:-}" = "--clean" ]; then
    step "clean boot (docker compose down -v)"
    docker compose down -v --remove-orphans
fi

step "boot"
eval "$(scripts/build_identity.sh)"
docker compose up -d --wait $([ "${KIT_SKIP_BUILD:-0}" = "1" ] || echo --build) || fail "stack did not become healthy"
curl -sf "$API/healthz" >/dev/null || fail "helper API is not answering"
curl -sf "$N8N/healthz/readiness" >/dev/null || fail "n8n is not ready"

eval "$(docker compose run --rm -T kit-login python -m opskit.bootstrap.show_login --env)"

step "seeded data"
check_seeded_data 28

step "n8n owner login and workflows"
curl -sf -c "$WORK/n8n.jar" -H 'content-type: application/json' \
    -d "{\"emailOrLdapLoginId\":\"owner@kit.example\",\"password\":\"$KIT_OWNER_PASSWORD\"}" \
    "$N8N/rest/login" >/dev/null || fail "n8n owner login failed"
curl -sf -b "$WORK/n8n.jar" "$N8N/rest/workflows" > "$WORK/workflows.json"
ids=$(json '",".join(sorted(w["id"] for w in d["data"]))' < "$WORK/workflows.json")
[ "$ids" = "inbox00000000001,inboxReply000001,kitSmoke00000001,leads00000000001,receipts00000001,runError00000001" ] || fail "unexpected workflows: $ids"
active=$(json '",".join(sorted(w["id"] for w in d["data"] if w.get("active")))' < "$WORK/workflows.json")
[ "$active" = "inbox00000000001,inboxReply000001,kitSmoke00000001,leads00000000001,receipts00000001" ] || fail "unexpected published workflows: $active"
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
    --data-urlencode "decision=approve" --data-urlencode "csrf_token=$(csrf_from "$WORK/detail.html")" \
    "$API$approval_path/decision")
[ "$status" = "303" ] || fail "decision POST returned $status"

run_done() { [ "$(sql "select status from core.runs where workflow = 'kit_smoke' order by started_at desc limit 1")" = "succeeded" ]; }
wait_for 90 "the smoke run to resume and finish" run_done
for action in approval.requested approval.decided approval.resumed model.call; do
    [ "$(sql "select count(*) from core.audit_log where action = '$action'")" -ge 1 ] || fail "no $action audit row"
done
wait_for_mail 30 "the Send Email node's message in Mailpit" 'subject:"Kit smoke: approved"'
echo "approved on the page, n8n resumed, audit rows written, confirmation email captured"

step "receipts workflow end to end (replay)"
receipts() { [ "$(curl -s -o /dev/null -w '%{http_code}' -X POST -H "X-Kit-Token: $KIT_WEBHOOK_TOKEN" "$N8N/webhook/receipts-run")" = "200" ]; }
wait_for 90 "the receipts webhook to register" receipts
receipts_done() { [ "$(sql "select status from core.runs where workflow = 'receipts' order by started_at desc limit 1")" = "succeeded" ]; }
wait_for 240 "the receipts run to finish" receipts_done
[ "$(sql "select count(*) from receipts.extractions where status = 'extracted'")" = "30" ] \
    || fail "expected 30 extracted receipts, got $(sql "select count(*) from receipts.extractions")"
flagged=$(sql "select summary->>'flagged' from receipts.reconciliations order by created_at desc limit 1")
[ -n "$flagged" ] || fail "no reconciliation was stored"
ls exports/reconciliation-*.xlsx >/dev/null 2>&1 || fail "no spreadsheet in exports/"
wait_for_mail 30 "the receipts summary email" 'subject:"Receipts reconciled"'
echo "30 receipts extracted from recordings, reconciled ($flagged flagged), spreadsheet written, summary emailed"

step "inbox workflow end to end (replay)"
inbox() { [ "$(curl -s -o /dev/null -w '%{http_code}' -X POST -H "X-Kit-Token: $KIT_WEBHOOK_TOKEN" "$N8N/webhook/inbox-run")" = "200" ]; }
wait_for 90 "the inbox webhook to register" inbox
inbox_triaged() { [ "$(sql 'select count(*) from inbox.triage')" = "28" ]; }
wait_for 240 "all 28 messages to be triaged" inbox_triaged
inbox_done() {
    [ "$(sql "select count(*) from core.runs where workflow = 'inbox' and status = 'running'")" = "0" ] \
        && [ "$(sql "select count(*) from core.runs where workflow = 'inbox' and status = 'succeeded'")" -ge 1 ]
}
wait_for 120 "the inbox run to finish" inbox_done
message_id_of() { grep -h -m1 -i '^Message-ID:' "$1" | sed 's/^[^:]*: *//' | tr -d '<>\r '; }
expected_held=$(for m in m15 m26 m27; do message_id_of "samples/inbox/messages/$m.eml"; done | sort | paste -sd, -)
[ "$(sql 'select count(*) from inbox.triage where quarantined')" = "3" ] || fail "expected 3 quarantined messages"
held=$(sql "select trim(both '<>' from message_id) from inbox.triage where quarantined" | sort | paste -sd, -)
[ "$held" = "$expected_held" ] || fail "quarantined messages are not the injection samples: $held"
[ "$(sql 'select count(*) from inbox.drafts d join inbox.triage t using (message_id) where t.quarantined')" = "0" ] \
    || fail "a quarantined message got a draft"

drafts_settled() { [ "$(sql "select count(*) from inbox.drafts where status = 'draft'")" = "0" ] \
    && [ "$(sql "select count(*) from inbox.drafts where status = 'pending'")" -ge 2 ]; }
wait_for 180 "at least two drafts to wait for approval" drafts_settled

m28_id=$(message_id_of samples/inbox/messages/m28.eml)
m28_from=$(sed -n 's/^From:.*<\(.*\)>.*/\1/p' samples/inbox/messages/m28.eml | head -1)
m28=$(sql "select status || '|' || to_addr || '|' || reply_to_differs::text || '|' || coalesce(approval_id::text, '') from inbox.drafts where trim(both '<>' from message_id) = '$m28_id'")
if [ -n "$m28" ]; then
    IFS='|' read -r m28_status m28_to m28_differs m28_approval <<< "$m28"
    if [ "$m28_status" != "failed" ]; then
        [ "$m28_to" = "$m28_from" ] || fail "the m28 draft goes to $m28_to, not the From address $m28_from"
        [ "$m28_differs" = "true" ] || fail "the m28 draft should record that Reply-To differs"
    fi
fi

# Pending inbox approvals whose page offers "Approve and send", read from the approver list.
curl -sf -b "$WORK/approver.jar" "$API/approver/" | grep -o '/approver/approvals/[0-9a-f-]*' | sort -u > "$WORK/approval-paths"
: > "$WORK/sendable"
while read -r path; do
    curl -sf -b "$WORK/approver.jar" "$API$path" > "$WORK/candidate.html" || continue
    grep -q "Approve and send" "$WORK/candidate.html" && echo "$path" >> "$WORK/sendable"
done < "$WORK/approval-paths"
[ "$(wc -l < "$WORK/sendable")" -ge 2 ] || fail "fewer than two reply approvals offer 'Approve and send'"
approve_path=$(sed -n 1p "$WORK/sendable")
reject_path=$(sed -n 2p "$WORK/sendable")
approve_id=${approve_path##*/}
reject_id=${reject_path##*/}

decide() {  # decide <approval path> <approve|reject>
    curl -sf -b "$WORK/approver.jar" "$API$1" > "$WORK/detail.html"
    [ "$(curl -s -o /dev/null -w '%{http_code}' -b "$WORK/approver.jar" \
        --data-urlencode "decision=$2" --data-urlencode "csrf_token=$(csrf_from "$WORK/detail.html")" \
        "$API$1/decision")" = "303" ] || fail "$2 decision POST failed"
}
decide "$approve_path" approve
decide "$reject_path" reject

draft_status() { sql "select status from inbox.drafts where approval_id = '$1'"; }
sent() { [ "$(draft_status "$approve_id")" = "sent" ]; }
rejected() { [ "$(draft_status "$reject_id")" = "rejected" ]; }
wait_for 120 "the approved draft to be sent" sent
wait_for 120 "the rejected draft to be closed" rejected
[ "$(sql "select count(*) from inbox.drafts where approval_id = '$reject_id' and sent_at is not null")" = "0" ] \
    || fail "a rejected draft was sent"
reply_to=$(sql "select to_addr from inbox.drafts where approval_id = '$approve_id'")
replied() { [ "$(curl -sf -G "$MAILPIT/api/v1/search" --data-urlencode "query=from:inbox@kit.example to:$reply_to" | json 'd["messages_count"]')" -ge 1 ]; }
wait_for 30 "the approved reply in Mailpit" replied
wait_for_mail 30 "the inbox summary email" 'subject:"Inbox:"'
echo "28 messages triaged, 3 held and never drafted, 1 reply approved and sent, 1 rejected and left unsent"

step "leads workflow end to end (replay)"
leads() { [ "$(curl -s -o /dev/null -w '%{http_code}' -X POST -H "X-Kit-Token: $KIT_WEBHOOK_TOKEN" "$N8N/webhook/leads-run")" = "200" ]; }
wait_for 90 "the leads webhook to register" leads
leads_done() { [ "$(sql "select count(*) from core.runs where workflow = 'leads' and status = 'succeeded'")" -ge 1 ]; }
wait_for 240 "the leads run to finish" leads_done
[ "$(sql "select count(*) from leads.research where status = 'researched'")" = "17" ] \
    || fail "expected 17 researched companies, got $(sql "select count(*) from leads.research where status = 'researched'")"
[ "$(sql "select count(*) from leads.research where reason = 'no website given'")" = "3" ] \
    || fail "expected 3 companies reported as no website given"
[ "$(sql 'select count(*) from crm.accounts')" = "21" ] || fail "expected 21 CRM accounts (5 seeded + 16 new; Ironwood updated)"
[ "$(sql "select crm_action from leads.research where domain = 'ironwood.example'")" = "updated" ] \
    || fail "Ironwood should be updated, not duplicated"
[ "$(sql "select count(*) from crm.account_sources s join crm.accounts a on a.id = s.account_id where a.domain = 'northfield.example' and s.field = 'employee_band' and s.excerpt like '%11-50%'")" = "1" ] \
    || fail "Northfield's band should be sourced as 11-50"
[ "$(sql "select count(*) from crm.account_sources where source_ref = '' or excerpt = ''")" = "0" ] || fail "a CRM field has no source"
wait_for_mail 30 "the leads summary email" 'subject:"Leads:"'
echo "17 companies researched with sources, 3 reported as no website given, Ironwood updated not duplicated"

step "leads run again (no duplicates)"
leads_runs() { [ "$(sql "select count(*) from core.runs where workflow = 'leads' and status = 'succeeded'")" -ge 2 ]; }
leads
wait_for 240 "the second leads run to finish" leads_runs
[ "$(sql 'select count(*) from crm.accounts')" = "21" ] || fail "a re-run changed the number of CRM accounts"
[ "$(sql 'select count(*) from (select account_id, field from crm.account_sources group by 1, 2 having count(*) > 1) d')" = "0" ] \
    || fail "a re-run duplicated a source row"
wait_for_mail 30 "the second leads summary email" 'subject:"Leads:"' 2
echo "re-run updated the same records: still 21 accounts, one source row per field"

step "no workflow sent a duplicate email"
assert_mail_exactly "the smoke confirmation" 'subject:"Kit smoke: approved"' 1
assert_mail_exactly "the receipts summary" 'subject:"Receipts reconciled"' 1
assert_mail_exactly "the inbox summary" 'subject:"Inbox:"' 1
assert_mail_exactly "the approved reply" "from:inbox@kit.example to:$reply_to -subject:\"Inbox:\"" 1
assert_mail_exactly "the leads summaries (two runs)" 'subject:"Leads:"' 2
[ "$(mail_count)" = "34" ] || fail "Mailpit should hold 34 messages (28 seeded + 6 sent), has $(mail_count)"
echo "one email per trigger: smoke 1, receipts 1, inbox summary 1, approved reply 1, leads 2; 34 in all"

step "second boot without -v"
docker compose up -d --wait || fail "second boot did not become healthy"
check_seeded_data 34 21
if docker compose logs --no-color n8n-import | grep -q WARNING; then
    fail "second boot re-imported a workflow that did not change"
fi
echo "second boot: same data, no workflow re-imported"

# For the record: how each execution started. A schedule ("trigger") that fires beside a webhook
# run doubles that workflow's email, which an exact count then reports as a timeout.
echo "n8n executions (workflow, how started, count):"
docker compose exec -T postgres psql -U postgres -d n8n -tAc \
    'select "workflowId", mode, count(*) from execution_entity group by 1, 2 order by 1, 2' || true

printf '\nSMOKE PASSED\n'
