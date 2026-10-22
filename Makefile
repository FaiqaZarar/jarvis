# Jarvis MVP - short commands for the operations in the README Setup section.
#
# Windows only. Targets run under cmd.exe and call the venv's Python
# directly (.venv\Scripts\python.exe) - they do NOT assume an activated
# shell, since Make does not reliably inherit one.
#
# Needs GNU Make. Install: winget install --id GnuWin32.Make
# GnuWin32.Make does NOT add itself to PATH - add this to your PATH:
#   C:\Program Files (x86)\GnuWin32\bin
# (see the README "Make commands" section).

SHELL := cmd.exe
.SHELLFLAGS := /C

# Backslash path is a literal value here (a backslash only continues a
# line when it is the LAST character on the line), so this is safe.
PY := .venv\Scripts\python.exe

.DEFAULT_GOAL := help
.PHONY: help setup config migrate run debug-ui ingest test clean

help: ## List every command with a description
	@powershell -NoProfile -Command "Write-Host 'Jarvis MVP - make targets:'; Get-Content '$(firstword $(MAKEFILE_LIST))' | Select-String '^[a-zA-Z][a-zA-Z_-]*:.*##' | ForEach-Object { $$p = $$_.Line -split ':.*?## '; '  make {0,-14} {1}' -f $$p[0].Trim(), $$p[1].Trim() }"

setup: ## Create .venv, install requirements.txt + jarvis_server, offer to create .env
	python -m venv .venv
	$(PY) -m pip install --upgrade pip
	$(PY) -m pip install -r requirements.txt
	$(PY) -m pip install -e ./jarvis/server
	@powershell -NoProfile -Command "if (Test-Path .env) { Write-Host '.env already exists - left untouched.' } elseif ((Read-Host 'No .env found. Create it from .env.example now? [y/N]') -match '^(y|Y)') { Copy-Item .env.example .env; Write-Host 'Created .env - now open it and set EMBEDDING_API_KEY to a real OpenRouter key.' } else { Write-Host 'Skipped. Create .env (and set EMBEDDING_API_KEY) before make ingest / make run.' }; Write-Host ''; Write-Host 'setup done. Next: make sure .env has a real EMBEDDING_API_KEY, then run: make migrate'"

config: ## Print .env + Claude Desktop config file locations
	@powershell -NoProfile -Command "$$repo = (Get-Location).Path; Write-Host ''; Write-Host '.env  (repo root, beside the jarvis\ package):'; Write-Host ('  ' + $$repo + '\.env'); Write-Host '  required:  EMBEDDING_API_KEY   = <your OpenRouter key>'; Write-Host '  optional:  EMBEDDING_MODEL, EMBEDDING_API_BASE, ORGANIZE_LLM_MODEL, JARVIS_LOG_LEVEL'; Write-Host ''; Write-Host 'Claude Desktop config (claude_desktop_config.json):'; Write-Host ('  standard install:  ' + $$env:APPDATA + '\Claude\claude_desktop_config.json'); Write-Host ('  packaged / Microsoft Store install:'); Write-Host ('     ' + $$env:LOCALAPPDATA + '\Packages\Claude_<publisher-id>\LocalCache\Roaming\Claude\claude_desktop_config.json'); Write-Host '  (if the standard path is missing, search the filename with hidden items shown)'; Write-Host ''; Write-Host 'mcpServers entry for this checkout:'; Write-Host ('  command : ' + $$repo + '\.venv\Scripts\python.exe'); Write-Host '  args    : [\"-m\", \"jarvis.cmd.mcp_server\"]'; Write-Host ('  cwd     : ' + $$repo); Write-Host ('  env     : { \"PYTHONPATH\": \"' + $$repo + '\" }')"

migrate: ## Apply DB migrations (python -m jarvis.cmd.migrate)
	$(PY) -m jarvis.cmd.migrate

run: ## Start the MCP server on stdio (Ctrl+C to stop)
	$(PY) -m jarvis.cmd.mcp_server

debug-ui: ## Start the local MCP debug console at http://127.0.0.1:8765 (Ctrl+C to stop)
	$(PY) -m jarvis.cmd.debug_ui

ingest: ## Ingest a folder:  make ingest FOLDER=C:\path\to\docs
ifndef FOLDER
	$(error FOLDER is required.  Usage: make ingest FOLDER=C:\path\to\your\docs  (runs sync -> chunk -> embed -> index -> organize -> reconcile))
endif
	$(PY) scripts\mk_ingest.py "$(FOLDER)"

test: ## Run the test suite (pytest)
	$(PY) -m pytest tests

clean: ## Delete .venv, caches, and local DB/graph files (asks first)
	@powershell -NoProfile -Command "if ((Read-Host 'Remove .venv, every __pycache__, .pytest_cache, jarvis.db, jarvis.db.bak, jarvis_graph.lbdb - type YES to confirm') -cne 'YES') { Write-Host 'Aborted - nothing deleted.'; exit 0 }; $$dirs = @('.venv', '.pytest_cache') + (Get-ChildItem -Path . -Recurse -Directory -Filter '__pycache__' -ErrorAction SilentlyContinue | ForEach-Object { $$_.FullName }); foreach ($$d in $$dirs) { if (Test-Path -LiteralPath $$d) { cmd /c rmdir /s /q \"$$d\"; if (Test-Path -LiteralPath $$d) { Write-Host ('WARNING: could not fully remove ' + $$d) } else { Write-Host ('removed ' + $$d) } } }; foreach ($$f in @('jarvis.db', 'jarvis.db.bak', 'jarvis_graph.lbdb')) { if (Test-Path -LiteralPath $$f) { Remove-Item -LiteralPath $$f -Force; Write-Host ('removed ' + $$f) } }; Write-Host 'clean complete.'"
