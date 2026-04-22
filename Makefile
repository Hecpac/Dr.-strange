.PHONY: evals

evals:
	.venv/bin/pytest tests/test_behavior_evals.py -q
