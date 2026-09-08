# Disable Bake for compatibility with Docker Compose 2.40.x.
export COMPOSE_BAKE=false

.PHONY: setup up down migrate test check logs status backend-shell test-ui frontend-install reset-password collector-logs live-check engine-check clock-check paper-check capacity-check
setup:
	python3 scripts/setup.py
up:
	docker compose up --build -d --wait db backend collector frontend
	@if ! rg -q '^LOCAL_NOTIFICATION_WORKER_ENABLED=false$$' .env; then docker compose up --build -d --wait notifications; else echo "Local Telegram consumer disabled: production owns the bot."; fi
migrate:
	docker compose run --build --rm migrate
# An isolated PostgreSQL service is used; the application database is never a test fixture.
test:
	docker compose --profile test up -d --force-recreate --wait test-db
	docker compose --profile test run --build --rm test
check: test
	cd frontend && npm run typecheck && npm run build
test-ui:
	cd frontend && npm run test:e2e
frontend-install:
	cd frontend && npm ci
logs:
	docker compose logs --tail=100 -f backend collector frontend notifications
status:
	docker compose ps -a
down:
	docker compose down
backend-shell:
	docker compose exec backend sh

reset-password:
	python3 scripts/reset_password.py
	docker compose up -d --force-recreate backend
collector-logs:
	docker compose logs --tail=100 -f collector
live-check:
	docker compose exec -T collector python -m arbiq.collector.verify

engine-check:
	docker compose exec -T collector python -m arbiq.opportunities.verify
clock-check:
	docker compose exec -T collector python scripts/clock_diagnostics.py --rounds 4 --interval 15

paper-check:
	docker compose exec -T collector python - < scripts/paper_diagnostics.py
capacity-check:
	PYTHONPATH=backend backend/.venv/bin/python scripts/benchmark_stage4.py --seconds 60

production-config:
	python3 scripts/validate_production_config.py
