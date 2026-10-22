"""Entrypoint shim: `python -m jarvis.cmd.debug_ui` -> jarvis.modules.debug_ui.server.

Kept alongside `mcp_server.py` / `migrate.py` / `drive_ingest.py` in
`jarvis/cmd/` - every runnable entrypoint in one predictable place; the
implementation itself follows the rest of the product surface into
`jarvis/modules/` (see jarvis/modules/debug_ui/server.py).
"""

from jarvis.modules.debug_ui.server import main

if __name__ == "__main__":
    main()
