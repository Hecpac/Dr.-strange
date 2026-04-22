.PHONY: evals

evals:
	.venv/bin/pytest tests/test_behavior_evals.py tests/test_capability_registry.py -q
