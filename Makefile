.PHONY: local-up local-down local-status local-logs local-app-check test agent-sync-check read-path-check agent-sync-status agent-sync-auth

PYTHON ?= .venv/bin/python3

test:
	@$(PYTHON) -m unittest discover -s tests -v

agent-sync-check:
	@$(PYTHON) -m unittest discover -s scripts/agent_sync/tests -t . -p 'test_*.py'

read-path-check:
	@$(PYTHON) -m unittest discover -s scripts/read_path/tests -t . -p 'test_*.py'

agent-sync-status:
	@$(PYTHON) -m scripts.agent_sync.cli status

# Human interactive OAuth only. Never run this target automatically or as an agent.
agent-sync-auth:
	@$(PYTHON) -m scripts.agent_sync.cli auth

# Local developer launcher (Linux): backend 127.0.0.1:8010 + Vite 127.0.0.1:5173. See README "Local development launcher".
# Extra options: make local-up ARGS="--backend-port 8011"; make local-logs ARGS="backend -n 100 -f"
local-up:
	@$(PYTHON) scripts/local_app.py up $(ARGS)

local-down:
	@$(PYTHON) scripts/local_app.py down

local-status:
	@$(PYTHON) scripts/local_app.py status $(ARGS)

local-logs:
	@$(PYTHON) scripts/local_app.py logs $(ARGS)

local-app-check:
	@$(PYTHON) -m unittest discover -s scripts/local_app_tests -t . -p 'test_*.py'
