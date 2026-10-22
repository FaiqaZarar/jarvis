"""OpenRouterVoyageEmbedder - the concrete Embedder used in production.

See embed.py for the Embedder interface and the embed_and_index_chunks
orchestrator that drives it.
"""

import os
import time

import requests

from jarvis_server.internal.embed.embed import DEFAULT_BATCH_SIZE, EMBEDDING_DIMENSIONS, Embedder
from jarvis_server.internal.errors import EmbeddingAuthError
from jarvis_server.internal.logging.logging_config import get_logger

logger = get_logger(__name__)

# Used only if EMBEDDING_API_BASE isn't set - keeps existing setups working
# without requiring an immediate .env update.
DEFAULT_API_BASE = "https://openrouter.ai/api/v1"

# A real, observed failure mode (a ConnectionResetError mid-TLS-handshake
# against openrouter.ai during a live ingest): a batch fails not because
# the request was bad, but because the connection itself was severed in
# transit - the same category of "transient, not a real rejection" failure
# daemon_client.call() already retries once for its own (local) HTTP
# calls. Mirrors that pattern here: a small, fixed number of retries with
# a short, growing pause, and ONLY for connection-level failures
# (ConnectionError/Timeout) - never for an HTTPError (401/403/etc.), which
# means the server was reached and gave a real, deterministic answer that
# retrying can't change.
# Each request is also capped by total characters, not just item count:
# 100 chunks of dense text (HTML, tables) can exceed the provider's
# per-request token limit and come back as a 400. A single chunk larger
# than this still goes out alone.
_MAX_BATCH_CHARS = 120_000

_EMBED_MAX_RETRIES = 2
_EMBED_RETRY_DELAY_S = 2.0


class OpenRouterVoyageEmbedder(Embedder):
    """Voyage embeddings via OpenRouter.

    Request shape and response shape are confirmed against OpenRouter's
    actual API reference (not assumed): request shape is {model, input,
    dimensions, input_type, provider}, response vectors live at
    data[].embedding. Provider fallback is explicitly disabled
    (provider.allow_fallbacks=False) so a request can never silently land
    on a different provider/model than the one being logged and priced
    for.

    Model and API base come from EMBEDDING_MODEL / EMBEDDING_API_BASE
    (.env), not hardcoded constants - see .env.example. Note the model is
    not a fully free config change: the embedding dimension is baked into
    the sqlite-vec chunk_vectors schema (FLOAT[1024], migration 0006), so
    switching to a model with a different output dimension requires a new
    migration and a full re-embed. embed_and_index_chunks enforces this
    with a hard check on every returned vector.
    """

    def __init__(self, batch_size: int = DEFAULT_BATCH_SIZE):
        self.batch_size = batch_size
        self._api_key = os.environ.get("EMBEDDING_API_KEY")
        if not self._api_key:
            logger.warning(
                "EMBEDDING_API_KEY is not set - OpenRouterVoyageEmbedder will "
                "fail on first real call."
            )
        self.model = os.environ.get("EMBEDDING_MODEL")
        if not self.model:
            logger.warning(
                "EMBEDDING_MODEL is not set - OpenRouterVoyageEmbedder will "
                "fail on first real call."
            )
        self.api_base = os.environ.get("EMBEDDING_API_BASE", DEFAULT_API_BASE)

    def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []

        vectors: list[list[float]] = []
        for batch in self._split_batches(texts):
            vectors.extend(self._embed_batch(batch))
        return vectors

    def _split_batches(self, texts: list[str]) -> list[list[str]]:
        batches: list[list[str]] = []
        current: list[str] = []
        current_chars = 0
        for text in texts:
            if current and (
                len(current) >= self.batch_size or current_chars + len(text) > _MAX_BATCH_CHARS
            ):
                batches.append(current)
                current, current_chars = [], 0
            current.append(text)
            current_chars += len(text)
        if current:
            batches.append(current)
        return batches

    def _embed_batch(self, batch: list[str]) -> list[list[float]]:
        logger.info("Embedding batch: size=%d model=%s", len(batch), self.model)
        attempt = 0
        while True:
            try:
                response = requests.post(
                    f"{self.api_base}/embeddings",
                    headers={
                        "Authorization": f"Bearer {self._api_key}",
                        "Content-Type": "application/json",
                    },
                    json={
                        "model": self.model,
                        "input": batch,
                        "dimensions": EMBEDDING_DIMENSIONS,
                        "input_type": "document",
                        "encoding_format": "float",
                        "provider": {
                            "order": ["voyageai"],
                            "allow_fallbacks": False,
                        },
                    },
                    timeout=60,
                )
                response.raise_for_status()
            except requests.HTTPError as exc:
                status = exc.response.status_code if exc.response is not None else None
                if status in (401, 403):
                    # Auth failure: almost always a missing / wrong / expired
                    # EMBEDDING_API_KEY. Re-raise with an actionable message,
                    # chained from the original so the raw 401 is still in the
                    # traceback (`raise ... from exc`), not swallowed. Never
                    # retried - a bad key won't fix itself between attempts.
                    logger.error(
                        "Embedding API auth failure (HTTP %s) - EMBEDDING_API_KEY in .env is "
                        "missing, wrong, or expired. batch_size=%d", status, len(batch),
                    )
                    raise EmbeddingAuthError(
                        f"Embedding API rejected the request (HTTP {status}). Check that "
                        "EMBEDDING_API_KEY in your .env is set to a valid OpenRouter key "
                        "(see the original error below for the provider's response)."
                    ) from exc
                # Any other real HTTP status (400, 500, ...): the server was
                # reached and gave a deterministic answer - not retried.
                logger.exception(
                    "Embedding API call failed: batch_size=%d chars=%d status=%s body=%s",
                    len(batch), sum(len(t) for t in batch), status,
                    exc.response.text[:1000] if exc.response is not None else None,
                )
                raise
            except (requests.ConnectionError, requests.Timeout) as exc:
                # Transport-level failure - the request never got a real
                # response at all (e.g. a ConnectionResetError mid-handshake,
                # confirmed as a real occurrence against openrouter.ai). This
                # is the one category worth retrying: identical inputs, a
                # plain network hiccup, no reason to expect the same failure
                # again.
                attempt += 1
                if attempt > _EMBED_MAX_RETRIES:
                    logger.exception(
                        "Embedding API call failed after %d attempt(s) (transient network "
                        "error, giving up): batch_size=%d", attempt, len(batch),
                    )
                    raise
                logger.warning(
                    "Embedding API call hit a transient network error (%s) - retrying "
                    "(attempt %d/%d): batch_size=%d",
                    type(exc).__name__, attempt, _EMBED_MAX_RETRIES, len(batch),
                )
                time.sleep(_EMBED_RETRY_DELAY_S * attempt)
                continue
            except requests.RequestException:
                logger.exception("Embedding API call failed: batch_size=%d", len(batch))
                raise
            else:
                break

        body = response.json()
        vectors = [item["embedding"] for item in body["data"]]

        # Dimension is not validated here - embed_and_index_chunks is the
        # single place that enforces it, right before a vector would reach
        # the (dimension-fixed) chunk_vectors table.
        logger.info(
            "Embedding batch succeeded: size=%d dimensions=%d",
            len(vectors), len(vectors[0]) if vectors else 0,
        )
        return vectors
