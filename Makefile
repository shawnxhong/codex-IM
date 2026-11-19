# Run on the Ubuntu device (never on the Windows dev machine, see AGENTS.md).
CODEX_VERSION ?= $(shell sed -n 's/^CODEX_VERSION=//p' deploy/.env 2>/dev/null)
COMPOSE = docker compose -f deploy/compose.yaml
COMPOSE_FAKE = $(COMPOSE) -f deploy/compose.fake.yaml

.PHONY: test lint build up down logs login smoke up-fake

test: ## unit tests inside the test image
	docker build -f deploy/docker/Dockerfile --target test --build-arg CODEX_VERSION=$(CODEX_VERSION) -t codex-im-test .
	docker run --rm codex-im-test

lint:
	docker run --rm codex-im-test ruff check src tests

build:
	$(COMPOSE) build

up:
	$(COMPOSE) up -d

down:
	$(COMPOSE) down

logs:
	$(COMPOSE) logs -f --tail=200 bridge agent egress-proxy

login: ## ChatGPT device-code login inside the agent container (once)
	$(COMPOSE) run --rm agent codex login --device-auth

smoke: ## post-upgrade smoke test through the real relay
	$(COMPOSE) run --rm --entrypoint python bridge -m codex_im.smoke

up-fake: ## bridge with the fake platform for docs/verification.md scenarios
	$(COMPOSE_FAKE) up -d
