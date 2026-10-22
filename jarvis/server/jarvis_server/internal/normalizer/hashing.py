"""The universal, format-agnostic normalize-to-Markdown + hashing helper.

Implemented in J-03: NFC normalize -> \\n endings + strip BOM -> strip
trailing whitespace -> collapse 3+ blank lines -> single trailing \\n, then
SHA-256 over the normalized body. See mvp-spec.md §5.1.

Fenced code blocks are protected from the blank-line collapse and trailing-
whitespace strip, since intentional blank lines / trailing spaces inside
code are meaningful and must survive normalization byte-for-byte.

Deliberately separate from jarvis_server.internal.normalizer.normalize.Normalize
(review feedback): this module runs the *same* cleanup + hash on Markdown
text regardless of which file type it came from, after a per-extension
converter has already turned raw bytes into Markdown. There's exactly one
call site that needs both steps together (the ingestion pipeline), so it
calls normalize_to_markdown() then hash_content() directly rather than
through a combined wrapper - a two-line inline call beats a one-off
abstraction for a single caller.
"""

import hashlib
import re
import unicodedata

from jarvis_server.internal.logging.logging_config import get_logger

logger = get_logger(__name__)

# Matches fenced code blocks (```...```), including the language tag line.
# DOTALL so '.' matches newlines inside the fence.
_FENCE_RE = re.compile(r"```.*?```", re.DOTALL)

# Placeholder token used to hold a fenced block's place during normalization.
# Uses NUL bytes as delimiters since they can't legitimately appear in
# normalized Markdown text, so there's no risk of collision with real content.
_PLACEHOLDER_TEMPLATE = "\x00FENCE{index}\x00"
_PLACEHOLDER_RE = re.compile(r"\x00FENCE(\d+)\x00")


def normalize_to_markdown(text: str) -> str:
    """Apply the canonical pre-hash normalization sequence (spec §5.1).

    Steps, in order:
      1. Unicode NFC normalization
      2. Line endings -> \\n, strip BOM
      3. Strip trailing whitespace per line (fenced code protected)
      4. Collapse runs of 3+ blank lines down to a single blank line
         (fenced code protected)
      5. Exactly one trailing newline

    Idempotent: normalizing already-normalized text returns it unchanged.
    """
    try:
        # 1. Unicode NFC
        text = unicodedata.normalize("NFC", text)

        # 2. Line endings -> \n, strip BOM
        text = text.replace("\r\n", "\n").replace("\r", "\n")
        text = text.lstrip("\ufeff")

        # Protect fenced code blocks before whitespace/blank-line passes.
        fenced_blocks: list[str] = []

        def _protect(match: re.Match) -> str:
            fenced_blocks.append(match.group(0))
            return _PLACEHOLDER_TEMPLATE.format(index=len(fenced_blocks) - 1)

        protected_text = _FENCE_RE.sub(_protect, text)

        # 3. Strip trailing whitespace per line
        protected_text = "\n".join(
            line.rstrip(" \t") for line in protected_text.split("\n")
        )

        # 4. Collapse runs of 3+ blank lines (i.e. 3+ consecutive newlines)
        #    down to a single blank line between content.
        protected_text = re.sub(r"\n{3,}", "\n\n", protected_text)

        # Restore fenced blocks verbatim.
        def _restore(match: re.Match) -> str:
            return fenced_blocks[int(match.group(1))]

        text = _PLACEHOLDER_RE.sub(_restore, protected_text)

        # 5. Exactly one trailing newline
        text = text.rstrip("\n") + "\n"

        return text
    except Exception:
        logger.exception("Failed to normalize text (length=%d)", len(text) if text else 0)
        raise


def hash_content(normalized_text: str) -> str:
    """SHA-256 hex digest over the normalized body only (spec §5.1, §4)."""
    try:
        return hashlib.sha256(normalized_text.encode("utf-8")).hexdigest()
    except Exception:
        logger.exception("Failed to hash normalized text")
        raise
