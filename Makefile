.PHONY: help install test lint format clean build run dist-plugin

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
# KiCad discovers plugins by looking for a directory named after the plugin
# inside its scripting/plugins/ folder. The zip contains:
#   kicad_ai_assistant/          <- KiCad plugin directory
#     __init__.py                <- entry point (KiCadAIPlugin)
#     context_bridge.py
#     llm_client.py
#     server_manager.py
#     settings.py
#     ui/
#     README.md
#     setup_plugin.sh            <- Linux setup
#     setup_plugin_macos.command <- double-clickable macOS setup
#     install_macos.sh           <- one-step macOS installer
# Install: unzip into ~/.local/share/kicad/<ver>/scripting/plugins/ (Linux)
#          or ~/Library/Preferences/kicad/<ver>/scripting/plugins/ (macOS)
#          then run the appropriate setup script.
PLUGIN_ZIP := dist/kicad_ai_assistant.zip
PLUGIN_SRC := kicad_plugin

dist-plugin:
	@mkdir -p dist
	@rm -f $(PLUGIN_ZIP)
	@echo "Building $(PLUGIN_ZIP)..."
	@rm -rf dist/_stage
	@mkdir -p dist/_stage
	@cp -r $(PLUGIN_SRC) dist/_stage/kicad_ai_assistant
	@find dist/_stage -type d -name __pycache__ -exec rm -rf {} +
	@find dist/_stage -type f -name "*.pyc" -delete
	@echo "Extracting version from pyproject.toml..."
	@grep -m1 '^version = ' pyproject.toml | sed 's/version = "\(.*\)"/\1/' > dist/_stage/kicad_ai_assistant/VERSION
	@echo "Version: $$(cat dist/_stage/kicad_ai_assistant/VERSION)"
	@cd dist/_stage && zip -r ../$(notdir $(PLUGIN_ZIP)) kicad_ai_assistant
	@rm -rf dist/_stage
	@echo "Created $(PLUGIN_ZIP)"

