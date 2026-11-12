# c-lord — developer shortcuts.
#
# Everything here is a thin name for a command you could type yourself; the
# source of truth for what "passing" means is .github/workflows/ci.yml, and
# `make ci` runs the same four checks in the same order.
#
#   make help         list the targets
#   make setup        once per clone: point git at the committed hooks
#   make ci           what CI runs (lint, format check, types, tests)

SRC   := c_lord/
TESTS := tests/
HOOKS := .githooks

.DEFAULT_GOAL := help
.PHONY: help setup check-setup need-uv format lint format-check types check test ci tmux

help:  ## List the targets
	@awk -F':.*## ' '/^[a-z-]+:.*## /{printf "  make %-12s %s\n", $$1, $$2}' $(MAKEFILE_LIST)

# --- one-time setup ---------------------------------------------------------

need-uv:
	@command -v uv >/dev/null 2>&1 || { \
		echo "❌ 'uv' is not installed. Install: https://docs.astral.sh/uv/getting-started/installation/"; \
		exit 1; }

setup: need-uv  ## Once per clone: use the hooks in .githooks/
	git config core.hooksPath $(HOOKS)
	@echo "✅ Git hooks configured ($(HOOKS)/pre-commit active)"

check-setup: need-uv  ## Fail unless `make setup` has been run in this clone
	@[ "$$(git config core.hooksPath)" = "$(HOOKS)" ] || { \
		echo "❌ Git hooks not configured. Run: make setup"; \
		exit 1; }
	@echo "✅ Development setup OK (uv present, hooks configured)"

# --- the checks CI runs -----------------------------------------------------

lint:  ## ruff check (no fixes)
	uv run ruff check $(SRC)

format-check:  ## ruff format, report only
	uv run ruff format --check $(SRC)

types:  ## pyright
	uv run pyright $(SRC)

check: lint format-check types  ## Everything static: lint + format check + types

test:  ## The test suite (e2e and live-LLM tests stay deselected, as in CI)
	uv run pytest $(TESTS)

ci: check test  ## The whole CI job, locally

format:  ## Rewrite the package's files with ruff format
	uv run ruff format $(SRC)

# --- convenience ------------------------------------------------------------

# Session name: $CLORD_TMUX_SESSION_NAME, else this directory's name. A missing
# session is created with one window called "admin" before attaching.
tmux:  ## Attach to this clone's tmux session (creating it if absent)
	@name=$${CLORD_TMUX_SESSION_NAME:-$$(basename "$$PWD")}; \
	tmux has-session -t "$$name" 2>/dev/null || { \
		echo "Creating tmux session '$$name' ..."; \
		tmux new-session -d -s "$$name" -n admin; }; \
	exec tmux attach -t "$$name"
