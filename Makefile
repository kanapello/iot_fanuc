# Machine monitor — developer commands.

COMPOSE := docker compose --env-file .env -f deploy/docker-compose.yml $(COMPOSE_EXTRA)
PROFILES := --profile dev
# Production on a cloud VM (ADR 0010, docs/runbooks/cloud-install.md)
PROD := docker compose --env-file .env -f deploy/docker-compose.yml -f deploy/docker-compose.prod.yml $(COMPOSE_EXTRA)

.PHONY: up down clean logs ps migrate psql recalc test test-backend test-edge test-frontend lint format \
	env-prod prod-up prod-down prod-ps prod-logs prod-psql backup-now backup-list restore-check restore prod-drop-demo

.env:
	cp .env.example .env
	@echo ">> .env created from .env.example — change the passwords before production use"

up: .env  ## start the server stack + simulator (profile dev)
	$(COMPOSE) $(PROFILES) up -d --build --remove-orphans

down: .env  ## stop the stack (volumes are kept)
	$(COMPOSE) $(PROFILES) down --remove-orphans

clean: .env  ## stop the stack and DELETE its volumes
	$(COMPOSE) $(PROFILES) down -v --remove-orphans

logs: .env  ## make logs s=ingest
	$(COMPOSE) $(PROFILES) logs -f --tail=100 $(s)

ps: .env
	$(COMPOSE) $(PROFILES) ps

migrate: .env  ## alembic upgrade head
	$(COMPOSE) run --rm migrate

psql: .env
	$(COMPOSE) exec db sh -c 'psql -U "$$POSTGRES_USER" -d "$$POSTGRES_DB"'

recalc: .env  ## make recalc from=2026-11-16T05:00:00Z [to=<iso>] [m=<machine_id>]
	$(COMPOSE) run --rm worker python -m app.worker recalc --from "$(from)" $(if $(to),--to "$(to)") $(if $(m),--machine "$(m)")

test: test-backend test-edge test-frontend

test-backend:
	cd backend && uv run pytest -q

test-edge:
	cd edge && uv run pytest -q

test-frontend:  ## no unit tests yet: type check only
	cd frontend && pnpm install --frozen-lockfile && pnpm typecheck

lint:
	cd backend && uv run ruff check . && uv run ruff format --check . && uv run mypy app
	cd edge && uv run ruff check . && uv run ruff format --check . && uv run mypy collector
	cd frontend && pnpm install --frozen-lockfile && pnpm typecheck

format:
	cd backend && uv run ruff format . && uv run ruff check --fix .
	cd edge && uv run ruff format . && uv run ruff check --fix .

# ---------------------------------------------------------------- production

env-prod:  ## make env-prod domain=mm.example.com email=ops@example.com: .env with random passwords
	sh deploy/scripts/env-prod.sh "$(domain)" "$(email)"

prod-up:  ## HTTPS (Caddy), MQTT over TLS, nightly backups; also the update after `git pull`
	$(PROD) up -d --build --remove-orphans

prod-down:  ## stop (volumes are kept)
	$(PROD) down --remove-orphans

prod-ps:
	$(PROD) ps

prod-logs:  ## make prod-logs s=caddy
	$(PROD) logs -f --tail=100 $(s)

prod-psql:
	$(PROD) exec db sh -c 'psql -U "$$POSTGRES_USER" -d "$$POSTGRES_DB"'

backup-now:  ## a backup to S3 right now, as the nightly one
	$(PROD) exec backup mm-backup backup

backup-list:
	$(PROD) exec backup mm-backup list

restore-check:  ## restore a backup (file=<name>, default latest) into a throwaway database, compare with the live one
	$(PROD) --profile tools up -d restore-check-db
	$(PROD) --profile tools run --rm backup restore-check $(or $(file),latest); status=$$?; \
	$(PROD) --profile tools rm -sfv restore-check-db; exit $$status

restore:  ## make restore file=<name>|latest confirm=yes: REPLACES the database with a backup
	@if [ "$(confirm)" != yes ]; then \
		echo "This replaces the database with the backup. Run: make restore file=$(or $(file),latest) confirm=yes"; exit 1; fi
	$(PROD) up -d db
	$(PROD) stop caddy api ingest worker
	$(PROD) run --rm --no-deps backup restore $(or $(file),latest) --yes
	$(PROD) up -d

prod-drop-demo:  ## remove the demo organization of the migrations (refuses if it has data)
	$(PROD) exec -T db sh -c 'psql -U "$$POSTGRES_USER" -d "$$POSTGRES_DB"' < deploy/scripts/drop-demo.sql
