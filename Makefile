.PHONY: test test-unit test-contract

UV ?= uv

test:
	$(UV) run --locked pytest

test-unit:
	$(UV) run --locked pytest -m unit -q

test-contract:
	$(UV) run --locked pytest -m contract -q

# Choose a new absolute state directory outside this repository. Secrets never
# appear in Make variables or terminal output; only the external path is used.
STATE_DIR ?=
BACKUP_FILE ?=
DEMO_PORT ?= 8080
PREREQUISITES_DIR ?=

.PHONY: demo-prepare demo-up demo-down verify-demo backup restore evidence

demo-prepare:
	@test -n "$(STATE_DIR)" || (echo 'Set STATE_DIR to a new absolute private directory outside the repository'; exit 1)
	$(UV) run --locked --offline python -m scripts.demo_runtime prepare --state-dir "$(STATE_DIR)" --port "$(DEMO_PORT)"

demo-up:
	@test -n "$(STATE_DIR)" || (echo 'Set STATE_DIR to the selected private demo directory'; exit 1)
	$(UV) run --locked --offline python -m scripts.demo_runtime up --state-dir "$(STATE_DIR)" --port "$(DEMO_PORT)"

demo-down:
	@test -n "$(STATE_DIR)"
	$(UV) run --locked --offline python -m scripts.demo_runtime down --state-dir "$(STATE_DIR)"

verify-demo:
	@test -n "$(STATE_DIR)"
	$(UV) run --locked --offline python -m scripts.verify_demo --state-dir "$(STATE_DIR)" --prerequisites-dir "$(PREREQUISITES_DIR)"

backup:
	@test -n "$(STATE_DIR)" && test -n "$(BACKUP_FILE)"
	$(UV) run --locked --offline python -m scripts.demo_backup backup --state-dir "$(STATE_DIR)" --archive "$(BACKUP_FILE)"

restore:
	@test -n "$(STATE_DIR)" && test -n "$(BACKUP_FILE)"
	$(UV) run --locked --offline python -m scripts.demo_backup restore --state-dir "$(STATE_DIR)" --archive "$(BACKUP_FILE)" --port "$(DEMO_PORT)"

evidence:
	@test -n "$(STATE_DIR)"
	$(UV) run --locked --offline python -m scripts.demo_evidence --state-dir "$(STATE_DIR)"
