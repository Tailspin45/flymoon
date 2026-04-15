SHELL=/bin/bash


CMD_ACTIVATE_VENV = source .venv/bin/activate
CMD_CHECK_ENV = [ ! -f .env ] && cp .env.mock .env || :
PYTHON = python3.9


install:
	@[ ! -d .venv ] && $(PYTHON) -m venv .venv ||:;
	@( \
		$(CMD_ACTIVATE_VENV) || exit 1; \
		pip install -r requirements.txt; \
	)


dev-install:
	@( \
		$(CMD_ACTIVATE_VENV) || exit 1; \
		pip install -r requirements-dev.txt; \
	)


LINT_EXCLUDE = '.cache|.venv|electron|archive'

lint:
	@( \
		black --check . --exclude $(LINT_EXCLUDE); \
		isort --check-only --skip electron --skip archive --skip .venv .; \
		autoflake --check --recursive --remove-all-unused-imports --remove-unused-variables --exclude $(LINT_EXCLUDE) .; \
	)


lint-apply:
	@( \
		black . --exclude $(LINT_EXCLUDE); \
		isort --skip electron --skip archive --skip .venv .; \
		autoflake --in-place --recursive --remove-all-unused-imports --remove-unused-variables --exclude $(LINT_EXCLUDE) .; \
	)


test:
	@( \
		$(CMD_ACTIVATE_VENV) || exit 1; \
		pytest tests/unit/ -v --cov=src --cov-report=term-missing; \
	)


create-env:
	@$(CMD_CHECK_ENV)


setup: create-env install


build-mac-app:
	@bash build_mac_app.sh
