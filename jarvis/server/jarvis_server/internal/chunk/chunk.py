"""Structure-aware chunking.

Implemented in J-05: split on Markdown heading boundaries, pack to
~512-1024 tokens with ~10-15% overlap, deterministic chunk hashing.
See mvp-spec.md §5.1.

The non-negotiable property (spec §5.1) is determinism of boundaries, not
the exact size/overlap figures: each section (the text under one heading,
before the next heading of any level) is packed into chunks using *only*
that section's own text. A section is never split, joined, or overlapped
across a heading boundary, and packing never looks at a neighboring
section's content - so editing one section can only ever change that
section's own chunk hashes; every other section's chunks stay byte-stable.
"""

import re
from dataclasses import dataclass
from typing import Optional

import tiktoken

from jarvis_server.internal.logging.logging_config import get_logger
from jarvis_server.internal.normalizer.hashing import hash_content

logger = get_logger(__name__)

# Target packing window (spec §5.1: "dev-tunable recommendations").
MAX_CHUNK_TOKENS = 1024
OVERLAP_TOKENS = round(MAX_CHUNK_TOKENS * 0.125)  # ~12.5%, within the 10-15% band

_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*$")
_FENCE_LINE_RE = re.compile(r"^```")
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+")

_encoder = None


def _get_encoder():
    """Lazily load the tokenizer once per process.

    tiktoken's cl100k_base vocab is fetched once and then cached on disk;
    a failure here (e.g. no network on first-ever run, no cache yet) is an
    external-dependency failure worth a clear, actionable log rather than
    a bare traceback deep inside the packer.
    """
    global _encoder
    if _encoder is None:
        try:
            _encoder = tiktoken.get_encoding("cl100k_base")
        except Exception:
            logger.exception(
                "Failed to load the tiktoken cl100k_base encoding. This needs network "
                "access on its first-ever run to fetch and cache the vocab file."
            )
            raise
    return _encoder


def _token_count(text: str) -> int:
    return len(_get_encoder().encode(text))


@dataclass
class Section:
    """The text directly under one heading (or the pre-heading preamble),
    up to but not including the next heading of any level."""
    heading_path: Optional[str]  # e.g. "Intro > Setup"; None for a headingless preamble
    body: str


@dataclass
class ChunkRecord:
    chunk_id: str       # = chunk_hash (schema: chunks.chunk_id is the PK)
    content_hash: str
    ordinal: int         # position within the document; metadata only, not hashed
    chunk_hash: str        # SHA-256 over `text`, via jarvis_server.internal.normalizer.hashing.hash_content
    text: str
    heading_path: Optional[str]


def split_sections(normalized_markdown: str) -> list[Section]:
    """Split normalized Markdown into heading-bounded sections.

    Fenced code blocks are tracked so a `#` inside a ``` block is never
    mistaken for a heading (mirrors jarvis_server.internal.normalizer.hashing's fence protection).
    Heading lines themselves are boundaries, not content - they're captured
    in `heading_path`, not duplicated into any section's `body`.
    """
    lines = normalized_markdown.split("\n")
    sections: list[Section] = []
    stack: list[tuple[int, str]] = []  # (level, title), root-to-current
    current_path: Optional[str] = None
    current_body_lines: list[str] = []
    in_fence = False

    def flush() -> None:
        body = "\n".join(current_body_lines).strip("\n")
        if body.strip():
            sections.append(Section(heading_path=current_path, body=body))

    for line in lines:
        if _FENCE_LINE_RE.match(line):
            in_fence = not in_fence
            current_body_lines.append(line)
            continue

        heading_match = None if in_fence else _HEADING_RE.match(line)
        if heading_match:
            flush()
            level = len(heading_match.group(1))
            title = heading_match.group(2)
            while stack and stack[-1][0] >= level:
                stack.pop()
            stack.append((level, title))
            current_path = " > ".join(t for _, t in stack)
            current_body_lines = []
            continue

        current_body_lines.append(line)

    flush()
    return sections


def _flatten_to_units(body: str) -> list[tuple[str, int]]:
    """Split a section's body into atomic (text, token_count) units, anchored
    to paragraph boundaries (spec §5.1: structure, not absolute offsets).

    A paragraph that alone exceeds the chunk budget is further split into
    sentences purely as a packing fallback - it is never left as a single
    unhandled block that would force an oversized chunk for no reason.
    """
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", body) if p.strip()]
    units: list[tuple[str, int]] = []
    for paragraph in paragraphs:
        tokens = _token_count(paragraph)
        if tokens <= MAX_CHUNK_TOKENS:
            units.append((paragraph, tokens))
            continue
        for sentence in _SENTENCE_SPLIT_RE.split(paragraph):
            sentence = sentence.strip()
            if sentence:
                units.append((sentence, _token_count(sentence)))
    return units


def _pack_units(units: list[tuple[str, int]]) -> list[str]:
    """Greedily pack units into chunk texts, each up to MAX_CHUNK_TOKENS,
    carrying an ~OVERLAP_TOKENS look-back of whole units into the next chunk.

    Always accepts at least one new unit per chunk (even an oversized one)
    so a pathological single unit can never stall progress into a loop.
    """
    texts: list[str] = []
    unit_lists: list[list[tuple[str, int]]] = []
    i = 0
    n = len(units)

    while i < n:
        chunk_units: list[tuple[str, int]] = []
        chunk_tokens = 0

        if unit_lists:
            overlap_tokens = 0
            overlap: list[tuple[str, int]] = []
            for unit_text, unit_tokens in reversed(unit_lists[-1]):
                if overlap and overlap_tokens + unit_tokens > OVERLAP_TOKENS:
                    break
                overlap.insert(0, (unit_text, unit_tokens))
                overlap_tokens += unit_tokens
            chunk_units.extend(overlap)
            chunk_tokens += overlap_tokens

        start_i = i
        while i < n:
            unit_text, unit_tokens = units[i]
            if i > start_i and chunk_tokens + unit_tokens > MAX_CHUNK_TOKENS:
                break
            chunk_units.append((unit_text, unit_tokens))
            chunk_tokens += unit_tokens
            i += 1

        unit_lists.append(chunk_units)
        texts.append("\n\n".join(t for t, _ in chunk_units))

    return texts


def chunk_document(content_hash: str, normalized_markdown: str) -> list[ChunkRecord]:
    """Chunk a document's normalized Markdown into ChunkRecords, ready for
    jarvis.db.insert_chunk. See module docstring for the
    determinism invariant this relies on."""
    try:
        sections = split_sections(normalized_markdown)
        records: list[ChunkRecord] = []
        ordinal = 0
        for section in sections:
            units = _flatten_to_units(section.body)
            if not units:
                continue
            for chunk_text in _pack_units(units):
                chunk_hash = hash_content(chunk_text)
                records.append(
                    ChunkRecord(
                        chunk_id=chunk_hash,
                        content_hash=content_hash,
                        ordinal=ordinal,
                        chunk_hash=chunk_hash,
                        text=chunk_text,
                        heading_path=section.heading_path,
                    )
                )
                ordinal += 1
        return records
    except Exception:
        logger.exception("Failed to chunk document content_hash=%s", content_hash)
        raise
