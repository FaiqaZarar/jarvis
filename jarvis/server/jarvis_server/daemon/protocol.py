# The daemon's HTTP contract version, reported on /health. A client
# replaces any daemon reporting an older (or no) version: the daemon
# outlives the app that started it, so after an update - or with Claude
# Desktop's MCP connection keeping an old one alive - a new client can
# otherwise end up talking to an old daemon.
#
# Bump on any change a client relies on. 2: ingest routes reply 202 and
# serve the run's outcome on /ingest/status. 3: POST /sync_roots/remove.
API_VERSION = 3
