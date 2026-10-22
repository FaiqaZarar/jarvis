"""LLM client for the organize engine's cluster labelling (J-08).

Deliberately a thin, swappable interface - same shape as
`jarvis_server.internal.embed.embed.Embedder` - so the concrete provider
(OpenRouter today) can be replaced without touching the roll-up logic,
and tests can inject a stub with no network.

Only used by the "Clio way" roll-up in
`jarvis_server.organize.clustering`: LLM-title each base cluster, then
propose-then-dedupe parent names up the tree. Low temperature, structured
JSON output. See mvp-spec.md §6 / §9.
"""

import json
import os
import re
import time
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from typing import Optional

import requests

from jarvis_server.internal import db
from jarvis_server.internal.logging.logging_config import get_logger

logger = get_logger(__name__)

# Mirrors OpenRouterVoyageEmbedder._embed_batch's retry constants
# (jarvis/internal/embed/provider.py) - a real ConnectionResetError was
# observed live against this exact call (LLM cluster labelling, during a
# real ingest) with no retry at all, the same class of transient-network
# gap already fixed for embeddings. Same shape: a small, fixed number of
# retries with a short, growing pause, only for connection-level
# failures - never for a real HTTP error (401/403/400/etc.), which means
# the server was reached and gave a deterministic answer retrying can't
# change.
_LLM_MAX_RETRIES = 2
_LLM_RETRY_DELAY_S = 2.0

# Reuses the embedder's key/base env vars on purpose (per the J-08
# check-in): the organize engine already talks to OpenRouter for
# embeddings, so a second key would just be another thing to configure.
# The model is its own var since a chat model is a different choice from
# the embedding model.
DEFAULT_API_BASE = "https://openrouter.ai/api/v1"
# A small, cheap, JSON-reliable chat model. Confirmed available on the
# project's OpenRouter key at build time; override with ORGANIZE_LLM_MODEL.
DEFAULT_MODEL = "anthropic/claude-haiku-4.5"

# Labelling is a "keep it stable, keep it terse" task, not a creative one
# (spec §6's re-label discipline). Low, not zero, so a retry after a
# malformed response isn't deterministically identical.
DEFAULT_TEMPERATURE = 0.2

# A title + one-line description is tiny; cap the response so a runaway
# generation can't inflate cost or latency.
DEFAULT_MAX_TOKENS = 400


class LLMClient(ABC):
    """The interface the roll-up depends on."""

    @abstractmethod
    def complete(
        self,
        system: str,
        user: str,
        *,
        temperature: float = DEFAULT_TEMPERATURE,
        max_tokens: int = DEFAULT_MAX_TOKENS,
    ) -> str:
        """Return the assistant message text for a single-turn
        system+user prompt."""
        raise NotImplementedError


class OpenRouterLLM(LLMClient):
    """Chat completion via OpenRouter.

    Request/response shape follows OpenAI's `/chat/completions` contract,
    which OpenRouter mirrors. Provider fallback is disabled so a request
    can't silently land on a different model than the one being logged
    and priced for - same stance as OpenRouterVoyageEmbedder.
    """

    def __init__(self, model: Optional[str] = None, timeout: int = 60):
        self._api_key = os.environ.get("EMBEDDING_API_KEY")
        if not self._api_key:
            logger.warning(
                "EMBEDDING_API_KEY is not set - OpenRouterLLM will fail on first real call."
            )
        self.model = model or os.environ.get("ORGANIZE_LLM_MODEL") or DEFAULT_MODEL
        self.api_base = os.environ.get("EMBEDDING_API_BASE", DEFAULT_API_BASE)
        self.timeout = timeout

    def complete(
        self,
        system: str,
        user: str,
        *,
        temperature: float = DEFAULT_TEMPERATURE,
        max_tokens: int = DEFAULT_MAX_TOKENS,
    ) -> str:
        logger.info("LLM complete: model=%s temp=%.2f user_chars=%d", self.model, temperature, len(user))
        attempt = 0
        while True:
            try:
                response = requests.post(
                    f"{self.api_base}/chat/completions",
                    headers={
                        "Authorization": f"Bearer {self._api_key}",
                        "Content-Type": "application/json",
                    },
                    json={
                        "model": self.model,
                        "messages": [
                            {"role": "system", "content": system},
                            {"role": "user", "content": user},
                        ],
                        "temperature": temperature,
                        "max_tokens": max_tokens,
                        "provider": {"allow_fallbacks": False},
                    },
                    timeout=self.timeout,
                )
                response.raise_for_status()
            except (requests.ConnectionError, requests.Timeout) as exc:
                # Transport-level failure - the request never got a real
                # response at all (e.g. a ConnectionResetError mid-
                # handshake, confirmed as a real occurrence against
                # openrouter.ai). This is the one category worth
                # retrying: identical inputs, a plain network hiccup, no
                # reason to expect the same failure again.
                attempt += 1
                if attempt > _LLM_MAX_RETRIES:
                    logger.exception(
                        "LLM chat-completion call failed after %d attempt(s) (transient "
                        "network error, giving up): model=%s", attempt, self.model,
                    )
                    raise
                logger.warning(
                    "LLM chat-completion call hit a transient network error (%s) - retrying "
                    "(attempt %d/%d): model=%s",
                    type(exc).__name__, attempt, _LLM_MAX_RETRIES, self.model,
                )
                time.sleep(_LLM_RETRY_DELAY_S * attempt)
                continue
            except requests.RequestException:
                # Any other real HTTP status (401/403/400/500/...): the
                # server was reached and gave a deterministic answer -
                # not retried.
                logger.exception("LLM chat-completion call failed: model=%s", self.model)
                raise
            else:
                break

        try:
            return response.json()["choices"][0]["message"]["content"]
        except (KeyError, IndexError, ValueError):
            logger.exception("LLM response had an unexpected shape: %s", response.text[:500])
            raise


class LoggingLLMClient(LLMClient):
    """Wraps another `LLMClient`, persisting a `tool_calls` row (Stats &
    Activity page) for every physical `.complete()` call - including both
    attempts of `complete_json`'s reject-and-retry, since each is a real,
    separately-billed call.

    Constructed once per real ingest run, around the concrete client, in
    `pipeline.py::_run_organize` - not inside `clustering.organize()`
    itself, so a caller that builds `clustering.organize()` directly with
    its own client (tests, the debug playground) never goes through this
    wrapper at all.
    """

    def __init__(self, inner: LLMClient, conn, *, run_id: Optional[str] = None):
        self._inner = inner
        self._conn = conn
        self._run_id = run_id

    def complete(
        self,
        system: str,
        user: str,
        *,
        temperature: float = DEFAULT_TEMPERATURE,
        max_tokens: int = DEFAULT_MAX_TOKENS,
    ) -> str:
        request = {
            "system": system,
            "user": user,
            "model": getattr(self._inner, "model", None),
            "temperature": temperature,
            "max_tokens": max_tokens,
            "run_id": self._run_id,
        }
        started = datetime.now(timezone.utc)
        start_monotonic = time.monotonic()
        try:
            result = self._inner.complete(system, user, temperature=temperature, max_tokens=max_tokens)
        except Exception as exc:
            db.insert_tool_call(
                self._conn, "llm", "cluster_label", request, None, False, started.isoformat(),
                error_message=str(exc), duration_ms=round((time.monotonic() - start_monotonic) * 1000),
            )
            raise
        db.insert_tool_call(
            self._conn, "llm", "cluster_label", request, {"content": result}, True, started.isoformat(),
            duration_ms=round((time.monotonic() - start_monotonic) * 1000),
        )
        return result


_JSON_OBJECT_RE = re.compile(r"\{.*\}", re.DOTALL)


def complete_json(
    client: LLMClient,
    system: str,
    user: str,
    *,
    required_keys: tuple[str, ...],
    temperature: float = DEFAULT_TEMPERATURE,
) -> dict:
    """Ask the model for a JSON object and parse it, retrying **once** on
    malformed / incomplete output (spec §6: "reject and retry once on
    malformed output").

    A model often wraps JSON in prose or a ```json fence; the first
    `{...}` span is extracted before parsing rather than trusting the
    whole message to be bare JSON.

    Retries exactly once, then raises ValueError (log + raise). The
    caller must NOT swallow this into a placeholder - per the J-08
    design, malformed LLM output fails loudly rather than being
    silently accepted as a topic label.
    """
    attempts = [
        user,
        user + "\n\nYour previous reply was not valid JSON with the required keys. "
               f"Reply with ONLY a JSON object containing exactly these keys: {list(required_keys)}.",
    ]
    last_error = "no attempt made"
    for i, prompt in enumerate(attempts):
        raw = client.complete(system, prompt, temperature=temperature)
        match = _JSON_OBJECT_RE.search(raw or "")
        if not match:
            last_error = f"no JSON object found in response (attempt {i + 1})"
            logger.warning("complete_json: %s", last_error)
            continue
        try:
            parsed = json.loads(match.group(0))
        except json.JSONDecodeError as exc:
            last_error = f"JSON parse failed on attempt {i + 1}: {exc}"
            logger.warning("complete_json: %s", last_error)
            continue
        missing = [k for k in required_keys if k not in parsed]
        if missing:
            last_error = f"response missing required keys {missing} (attempt {i + 1})"
            logger.warning("complete_json: %s", last_error)
            continue
        return parsed

    logger.error("complete_json: giving up after one retry - %s", last_error)
    raise ValueError(f"complete_json: unusable LLM output after retry - {last_error}")
