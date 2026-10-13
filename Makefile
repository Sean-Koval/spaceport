.PHONY: test test-unit test-contract

UV ?= uv

test:
	$(UV) run --locked pytest

test-unit:
	$(UV) run --locked pytest -m unit -q

test-contract:
	$(UV) run --locked pytest -m contract -q
