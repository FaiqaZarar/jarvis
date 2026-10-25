#!/usr/bin/env bash
# Starts a packaged build's background service against an empty library
# and checks it answers /health. Catches a build that can't start on its
# platform, e.g. SQL its bundled SQLite doesn't support.
# Usage: smoke_test_daemon.sh <path to the packaged jarvis_desktop binary>
set -u

exe="${1:?usage: smoke_test_daemon.sh <jarvis_desktop binary>}"
home="$(mktemp -d)"
log="$home/smoke.log"

JARVIS_DAEMON_HOME="$home" QT_QPA_PLATFORM=offscreen "$exe" --daemon > "$log" 2>&1 &
pid=$!

# The first start compiles numba code, so allow a couple of minutes.
for _ in $(seq 1 120); do
  if [ -f "$home/.jarvis_daemon.json" ]; then
    port=$(sed -E 's/.*"port": *([0-9]+).*/\1/' "$home/.jarvis_daemon.json")
    if curl -fsS "http://127.0.0.1:$port/health"; then
      echo
      echo "Background service started and answered /health."
      kill "$pid"
      exit 0
    fi
  fi
  kill -0 "$pid" 2>/dev/null || break
  sleep 1
done

echo "::error::The packaged background service did not start."
cat "$log"
kill "$pid" 2>/dev/null
exit 1
