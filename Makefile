# Day-to-day commands. Everything runs through docker compose or uv.
.PHONY: up down clean login check test smoke samples evals export reimport

up:
	@eval "$$(scripts/build_identity.sh)"; \
	docker compose up -d --build --wait

down:
	docker compose down

clean:
	docker compose down -v --remove-orphans

# The only way to read the generated n8n owner and approver passwords.
login:
	@docker compose run --rm -T kit-login

check:
	uv run ruff check .
	uv run ruff format --check .
	uv run mypy src
	uv run lint-imports
	uv run python scripts/lint_workflows.py n8n/workflows
	uv run pytest tests/unit -q

# Integration tests need the stack running (make up).
test:
	uv run pytest tests/unit -q
	KIT_INTEGRATION=1 uv run pytest tests/integration -v -rs

evals:
	uv run python -m opskit.evals.receipts --label small-replay --min-field-accuracy 0.95
	uv run python -m opskit.evals.inbox --label replay --require-injection-recall --min-triage-accuracy 0.85

smoke:
	scripts/smoke.sh --clean

# Regenerate samples/ and evals/answer_keys/ inside the pinned image (fonts and Pillow fixed).
samples:
	docker build -f docker/api.Dockerfile --target samplegen -t ops-automation-kit-samplegen:local .
	docker run --rm --user "$$(id -u):$$(id -g)" -v "$$(pwd):/repo" ops-automation-kit-samplegen:local

# Write workflows edited in the n8n editor back to n8n/workflows/ and record their hashes,
# so the next boot does not re-import them.
export:
	docker compose exec -T n8n sh -c 'rm -rf /tmp/kit-export && n8n export:workflow --all --separate --output=/tmp/kit-export/ >/dev/null'
	rm -rf .kit-export && mkdir -p .kit-export
	docker compose cp n8n:/tmp/kit-export/. .kit-export/
	uv run python scripts/export_workflows.py .kit-export n8n/workflows > .kit-export/hashes
	docker compose exec -T n8n sh -c 'mkdir -p /home/node/.n8n/kit-import-state && while read id hash; do echo "$$hash" > "/home/node/.n8n/kit-import-state/$$id.sha256"; done' < .kit-export/hashes
	rm -rf .kit-export

# Force every workflow back to the committed JSON (replaces editor changes, with a warning).
reimport:
	docker compose stop n8n
	FORCE_REIMPORT=1 docker compose up --no-deps --force-recreate n8n-import
	docker compose up -d --wait n8n
