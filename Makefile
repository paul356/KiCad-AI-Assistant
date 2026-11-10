.PHONY: help install test lint format clean build run dist-plugin dist-pcm

PYTHON ?= python3

help:
	@echo "Available commands:"
	@echo " 	install       		Install dependencies"
	@echo " 	test          		Run tests"
	@echo " 	test <file>   		Run specific test file"
	@echo " 	lint          		Run linting"
	@echo " 	format        		Format code"
	@echo " 	clean         		Clean build artifacts"
	@echo " 	build         		Build package"
	@echo " 	run           		Start the KiCad MCP server"
	@echo " 	dist-plugin   		Build installable KiCad plugin zip"
	@echo " 	dist-pcm      		Build KiCad Plugin and Content Manager zip"

install:
	uv sync --group dev

test:
	# Collect extra args; if none, use tests/
	@files="$(filter-out $@,$(MAKECMDGOALS))"; \
	if [ -z "$$files" ]; then files="tests/"; fi; \
	uv run pytest $$files -v

# Prevent “No rule to make target …” errors for the extra args
%::
	@:

lint:
	uv run ruff check kcaa/ tests/
	uv run mypy kcaa/

format:
	uv run ruff format kcaa/ tests/

clean:
	rm -rf dist/
	rm -rf build/
	rm -rf *.egg-info/
	rm -rf .pytest_cache/
	rm -rf htmlcov/
	rm -f coverage.xml
	find . -type d -name __pycache__ -delete
	find . -type f -name "*.pyc" -delete

build:
	uv build

run:
	uv run python main.py

# Build an installable KiCad plugin zip.
# KiCad discovers manual plugins by looking for a directory named after the
# plugin inside its scripting/plugins/ folder. The zip contains:
#   kicad_ai_assistant/          <- KiCad plugin directory
#     __init__.py                <- entry point (KiCadAIPlugin)
#     context_bridge.py
#     llm_client.py
#     server_manager.py
#     settings.py
#     ui/
#     README.md
#     VERSION                    <- generated from pyproject.toml
# Install: unzip into <documents>/kicad/<ver>/scripting/plugins/
#          then run from the plugin dir:
#            ./setup_plugin.sh [<path>]
#            (no argument installs kcaa from PyPI)
PLUGIN_ZIP := dist/kicad_ai_assistant.zip
PCM_ZIP := dist/kicad_ai_assistant_pcm.zip
PLUGIN_SRC := kicad_plugin

dist-plugin:
	@$(PYTHON) scripts/build_plugin_zip.py legacy --output $(PLUGIN_ZIP) --source $(PLUGIN_SRC)

# Build a KiCad Plugin and Content Manager archive:
#   metadata.json
#   plugins/kicad_ai_assistant/  <- same plugin tree and VERSION file
# Install with Preferences -> Plugin and Content Manager -> Install from File...
dist-pcm:
	@$(PYTHON) scripts/build_plugin_zip.py pcm --output $(PCM_ZIP) --source $(PLUGIN_SRC)

