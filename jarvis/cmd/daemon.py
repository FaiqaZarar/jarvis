"""Entrypoint shim: `python -m jarvis.cmd.daemon` -> jarvis_server.daemon.server.

Kept alongside `mcp_server.py` / `migrate.py` / `debug_ui.py` in
`jarvis/cmd/` - every runnable entrypoint in one predictable place; the
implementation itself follows the rest of the product surface into
`jarvis/modules/` (see jarvis/modules/daemon/server.py).

Normally started automatically - `jarvis.modules.daemon.client.
ensure_daemon_running()` spawns this for you the first time a client
needs it (design doc §6.1/§6.4, docs/local-server-architecture-DESIGN.md).
Running it directly is for foreground debugging (Ctrl+C to stop; a
`--yes`-style manual step is not needed for normal use).

**Fixed PYTHONHASHSEED, checked before anything else imports.** The
organize engine's checkpoint-and-resume design
(docs/organize-checkpointing-DESIGN.md §1d/§5.2) depends on UMAP+HDBSCAN
clustering reproducing identical results across a crash-and-restart -
i.e. in a brand-new Python process, not just repeated calls within one
already-running process. A real 30-run cross-process investigation found
no divergence at all with Python's default (randomized) hash seed, so
this is not a fix for an observed bug - it's removing PYTHONHASHSEED as
a class of variability pre-emptively, since nothing about the
determinism guarantee this design leans on should depend on it, and the
cost of pinning it is zero.

PYTHONHASHSEED must be set in the environment *before* the interpreter
boots - mutating `os.environ` from already-running Python code has no
effect on that process's own hash randomization, only on children it
subsequently spawns (confirmed directly: a process with PYTHONHASHSEED
unset does not retroactively adopt a fixed hash() value just because
code later sets the env var).

The obvious fix - re-exec in place via `os.execve` - was tried and
**segfaults on this Windows/Python 3.13 combination** (confirmed
directly, not assumed: it crashes the interpreter before the new image
loads). The portable alternative used here - relaunch as a child
`subprocess` with the corrected environment and propagate its exit code
- was verified to actually work: the relaunched child's `hash()` output
was confirmed to match the same value a process started with
`PYTHONHASHSEED=0` from birth produces, not just that the env var is
present in `os.environ`.

`jarvis.modules.daemon.client._spawn_daemon` also sets this directly for
its subprocess.Popen `env=` - the common auto-start path never hits the
relaunch below at all (it's born correctly seeded). This module's own
check exists so a manual `python -m jarvis.cmd.daemon` run, or any
future launcher that doesn't know to set this, still gets a
deterministic daemon rather than depending on every caller remembering
to.

`_ensure_fixed_hash_seed()` is also reused directly (not duplicated) by
`jarvis/cmd/desktop_app.py`'s own `--daemon` dispatch branch (design doc
§4, resolved: single frozen executable, argv-dispatched) - which is
exactly why the relaunch argv below is frozen-aware: under a real,
unfrozen Python interpreter it's `-m jarvis.cmd.daemon` as before, but
inside a PyInstaller-frozen build `sys.executable` is the packaged
binary itself, not a general interpreter capable of `-m`, so the
relaunch there has to be `[sys.executable, "--daemon"]` instead - the
same argv `_spawn_daemon()`'s own frozen branch spawns.

**Implication for anyone spawning this directly without pre-setting
PYTHONHASHSEED:** the process you get a `Popen` handle back for may not
be the one that ends up serving - it can hand off to a child with a
*different* pid (confirmed: `tests/test_daemon.py::
test_daemon_process_starts_responds_and_stops` hit exactly this and now
uses the discovery file's pid to stop the daemon, not the Popen handle).
Use the discovery file (`.jarvis_daemon.json`'s `pid`) to find and stop
the real daemon, the same way `jarvis.modules.daemon.client` already
does everywhere else - don't rely on a launcher's own pid or a
`Popen.terminate()` call.
"""

import os
import subprocess
import sys


def _ensure_fixed_hash_seed() -> None:
    if os.environ.get("PYTHONHASHSEED") == "0":
        return
    env = dict(os.environ)
    env["PYTHONHASHSEED"] = "0"
    # sys.frozen is PyInstaller's own standard flag (set on the frozen
    # executable's sys module, never present under a real interpreter) -
    # see the module docstring for why the argv has to differ here.
    if getattr(sys, "frozen", False):
        argv = [sys.executable, "--daemon"]
    else:
        argv = [sys.executable, "-m", "jarvis.cmd.daemon"]
    # stdin/stdout/stderr passed explicitly, not left as subprocess.run's
    # default "inherit" - confirmed necessary specifically for a frozen,
    # windowed-subsystem (console=False) build: a manual `--daemon` run
    # from an already-open terminal, with output redirected, silently
    # lost all of it under the default when this relaunch fired (real,
    # reproduced regression - not theoretical). Passing the real
    # sys.std*/objects explicitly restores it; harmless when unframed/
    # unfrozen (they're just the normal streams already) and harmless
    # if any of them are ever None (subprocess.run treats that
    # identically to not passing the argument at all).
    result = subprocess.run(argv, env=env, stdin=sys.stdin, stdout=sys.stdout, stderr=sys.stderr)
    sys.exit(result.returncode)


if __name__ == "__main__":
    _ensure_fixed_hash_seed()

    from jarvis_server.daemon.server import main

    main()
