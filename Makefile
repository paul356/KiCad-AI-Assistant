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
	uv run ruff check kicad_mcp/ tests/
	uv run mypy kicad_mcp/

format:
	uv run ruff format kicad_mcp/ tests/

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
# Install: unzip into ~/.local/share/kicad/<ver>/scripting/plugins/
PLUGIN_ZIP := dist/kicad_ai_assistant.zip
PLUGIN_SRC := kicad_plugin

dist-plugin:
	@mkdir -p dist
	@rm -f $(PLUGIN_ZIP)
	@echo "Building $(PLUGIN_ZIP)..."
	@cd . && zip -r $(PLUGIN_ZIP) $(PLUGIN_SRC) \
		-x "$(PLUGIN_SRC)/__pycache__/*" \
		-x "$(PLUGIN_SRC)/ui/__pycache__/*" \
		-x "$(PLUGIN_SRC)/*.pyc" \
		-x "$(PLUGIN_SRC)/ui/*.pyc"
	@# Rename the top-level dir inside the zip from kicad_plugin → kicad_ai_assistant
	@python3 - <<'EOF'
import zipfile, os, shutil

src_zip = "$(PLUGIN_ZIP)"
tmp_zip = src_zip + ".tmp"

with zipfile.ZipFile(src_zip, "r") as zin, zipfile.ZipFile(tmp_zip, "w", zipfile.ZIP_DEFLATED) as zout:
    for item in zin.infolist():
        data = zin.read(item.filename)
        new_name = item.filename.replace("kicad_plugin/", "kicad_ai_assistant/", 1)
        item.filename = new_name
        zout.writestr(item, data)

os.replace(tmp_zip, src_zip)
print("Created", src_zip)
EOF

