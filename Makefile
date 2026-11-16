# Machine monitor — developer commands. Targets for later stages (gen-client,
# recalc, edge-build-win) are added together with the code they run.

COMPOSE := docker compose --env-file .env -f deploy/docker-compose.yml $(COMPOSE_EXTRA)
PROFILES := --profile dev

.PHONY: up down clean logs ps migrate psql test test-backend test-edge lint format

.env:
	cp .env.example .env
	@echo ">> .env created from .env.example — change the passwords before production use"

up: .env  ## start the server stack + simulator (profile dev)
	$(COMPOSE) $(PROFILES) up -d --build

down: .env  ## stop the stack (volumes are kept)
	$(COMPOSE) $(PROFILES) down

clean: .env  ## stop the stack and DELETE its volumes
	$(COMPOSE) $(PROFILES) down -v

logs: .env  ## make logs s=ingest
	$(COMPOSE) $(PROFILES) logs -f --tail=100 $(s)

ps: .env
	$(COMPOSE) $(PROFILES) ps

migrate: .env  ## alembic upgrade head
	$(COMPOSE) run --rm migrate

psql: .env
	$(COMPOSE) exec db sh -c 'psql -U "$$POSTGRES_USER" -d "$$POSTGRES_DB"'

test: test-backend test-edge

test-backend:
	cd backend && uv run pytest -q

test-edge:
	cd edge && uv run pytest -q

lint:
	cd backend && uv run ruff check . && uv run ruff format --check . && uv run mypy app
	cd edge && uv run ruff check . && uv run ruff format --check . && uv run mypy collector

format:
	cd backend && uv run ruff format . && uv run ruff check --fix .
	cd edge && uv run ruff format . && uv run ruff check --fix .
