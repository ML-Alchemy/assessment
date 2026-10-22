# Relay starter package commands. Run `make help` for a summary.
COMPOSE = docker compose
TOOLS = $(COMPOSE) run --rm tools

.PHONY: help init up seed status burst check logs down reset

help:
	@echo "make init    Generate local credentials (.env)"
	@echo "make up      Build and start everything"
	@echo "make seed    Connect each customer's CRM through the OAuth flow"
	@echo "make status  Check that every service is responding"
	@echo "make burst   Send every transcript several times over, all at once"
	@echo "make check   Run the verification report"
	@echo "make logs    Follow the logs of all services"
	@echo "make down    Stop everything"
	@echo "make reset   Stop everything and delete all local data"

init:
	python3 scripts/init_env.py

up:
	@test -f .env || (echo "Run 'make init' first." && exit 1)
	$(COMPOSE) up -d --build --wait
	$(COMPOSE) --profile tools build meeting-sim tools

seed:
	$(TOOLS) python seed.py

status:
	@$(COMPOSE) ps
	@$(TOOLS) python status.py

burst:
	$(COMPOSE) run --rm meeting-sim burst

check:
	$(TOOLS) python check.py

logs:
	$(COMPOSE) logs -f

down:
	$(COMPOSE) down

reset:
	$(COMPOSE) down -v
	rm -rf state/*
