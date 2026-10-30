.PHONY: test agent-sync-check read-path-check agent-sync-status agent-sync-auth

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
