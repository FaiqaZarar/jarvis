# Builds and runs jarvis_cloud_server on its own (Railway's "Root
# Directory" for this service should be set to jarvis_cloud/, so this
# Dockerfile's build context is this directory - see the deploy notes
# in .env.example for the env vars the running container needs).
FROM python:3.11-slim

WORKDIR /app

# psycopg[binary] ships its own libpq, so no extra system packages are
# needed here - kept deliberately minimal (see pyproject.toml's own
# comment on choosing psycopg[binary] for exactly this reason).
COPY pyproject.toml ./
COPY jarvis_cloud_server ./jarvis_cloud_server
RUN pip install --no-cache-dir .

# Railway (and most PaaS platforms) inject PORT at runtime; server.py's
# own main() already reads it the same way, defaulting to 8000 for
# local/dev use.
ENV PORT=8000
EXPOSE 8000

# Migrations run once, at container start, before uvicorn - not as a
# separate deploy step, since idempotent tracking (schema_migrations)
# already makes re-running safe on every restart.
CMD ["sh", "-c", "python -m jarvis_cloud_server.internal.migrate && python -m jarvis_cloud_server.server"]
