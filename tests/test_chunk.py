"""J-05 acceptance check: structure-aware chunking on heading boundaries,
deterministic chunk hashing. See mvp-spec.md §5.1."""

from pathlib import Path

import pytest

from jarvis_server.internal import db
from jarvis_server.internal.chunk.chunk import (
    MAX_CHUNK_TOKENS,
    _token_count,
    chunk_document,
    split_sections,
)
from jarvis_server.internal.migrate import run_migrations
from jarvis_server.internal.normalizer.hashing import hash_content, normalize_to_markdown


@pytest.fixture
def conn(tmp_path: Path):
    db_path = tmp_path / "test.db"
    run_migrations(db_path)
    connection = db.get_connection(db_path)
    yield connection
    connection.close()


def _paragraph(word: str, count: int) -> str:
    """A distinguishable paragraph of `count` repetitions of `word`, so it
    shows up unambiguously in overlap/boundary assertions."""
    return " ".join(f"{word}{i}" for i in range(count))


def _long_section_body(word: str, n_paragraphs: int = 40, words_per_paragraph: int = 30) -> str:
    return "\n\n".join(_paragraph(f"{word}p{p}_", words_per_paragraph) for p in range(n_paragraphs))


# ---------------------------------------------------------------------
# The literal J-05 acceptance criterion
# ---------------------------------------------------------------------

def test_editing_one_section_leaves_other_chunk_hashes_unchanged():
    """Editing one section of a test doc leaves other chunks' hashes
    unchanged (J-05 acceptance criterion, GitHub issue #5)."""
    doc = (
        "# Intro\n\nThis is the introduction section.\n\n"
        "# Middle\n\nThis is the original middle section text.\n\n"
        "# Conclusion\n\nThis is the conclusion section.\n"
    )
    normalized = normalize_to_markdown(doc)
    original_records = chunk_document("doc-hash", normalized)
    original_hashes = {r.heading_path: [c.chunk_hash for c in original_records if c.heading_path == r.heading_path] for r in original_records}

    edited = doc.replace(
        "This is the original middle section text.",
        "This is a completely rewritten middle section with different content entirely.",
    )
    edited_normalized = normalize_to_markdown(edited)
    edited_records = chunk_document("doc-hash", edited_normalized)
    edited_hashes = {r.heading_path: [c.chunk_hash for c in edited_records if c.heading_path == r.heading_path] for r in edited_records}

    assert edited_hashes["Intro"] == original_hashes["Intro"]
    assert edited_hashes["Conclusion"] == original_hashes["Conclusion"]
    assert edited_hashes["Middle"] != original_hashes["Middle"]


# ---------------------------------------------------------------------
# Structure / boundaries
# ---------------------------------------------------------------------

def test_never_crosses_a_heading_boundary():
    """No chunk's text may contain content from two different sections."""
    doc = "# One\n\nAlpha content here.\n\n# Two\n\nBeta content here.\n"
    records = chunk_document("h", normalize_to_markdown(doc))

    for record in records:
        assert not ("Alpha" in record.text and "Beta" in record.text)

    one_texts = " ".join(r.text for r in records if r.heading_path == "One")
    two_texts = " ".join(r.text for r in records if r.heading_path == "Two")
    assert "Beta" not in one_texts
    assert "Alpha" not in two_texts


def test_heading_inside_fenced_code_block_is_not_a_boundary():
    """A `#` inside a fenced code block must not be treated as a heading."""
    doc = "# Real Heading\n\nSome text.\n\n```python\n# not a heading\nx = 1\n```\n\nMore text.\n"
    records = chunk_document("h", normalize_to_markdown(doc))

    assert all(r.heading_path == "Real Heading" for r in records)
    combined = "\n".join(r.text for r in records)
    assert "# not a heading" in combined


def test_heading_path_breadcrumb_nests_correctly():
    doc = "# Top\n\nTop text.\n\n## Child\n\nChild text.\n\n### Grandchild\n\nGrandchild text.\n"
    records = chunk_document("h", normalize_to_markdown(doc))
    paths = {r.heading_path for r in records}

    assert "Top" in paths
    assert "Top > Child" in paths
    assert "Top > Child > Grandchild" in paths


def test_sibling_headings_do_not_leak_into_each_others_path():
    doc = "# A\n\n## A1\n\nContent.\n\n# B\n\n## B1\n\nOther content.\n"
    records = chunk_document("h", normalize_to_markdown(doc))
    paths = {r.heading_path for r in records}

    assert "A > A1" in paths
    assert "B > B1" in paths
    assert "A > B1" not in paths


def test_preamble_before_any_heading_is_its_own_headingless_section():
    doc = "Some preamble text with no heading above it.\n\n# First Heading\n\nBody.\n"
    records = chunk_document("h", normalize_to_markdown(doc))

    preamble = [r for r in records if r.heading_path is None]
    assert len(preamble) == 1
    assert "preamble" in preamble[0].text


# ---------------------------------------------------------------------
# Hashing / determinism
# ---------------------------------------------------------------------

def test_chunk_hash_is_sha256_of_stored_text():
    doc = "# H\n\nSome body text for hashing.\n"
    records = chunk_document("h", normalize_to_markdown(doc))

    assert len(records) == 1
    assert records[0].chunk_hash == hash_content(records[0].text)
    assert records[0].chunk_id == records[0].chunk_hash


def test_chunking_is_deterministic_across_runs():
    doc = "# H\n\n" + _long_section_body("word", n_paragraphs=10)
    normalized = normalize_to_markdown(doc)

    first = [r.chunk_hash for r in chunk_document("h", normalized)]
    second = [r.chunk_hash for r in chunk_document("h", normalized)]

    assert first == second
    assert len(first) > 0


def test_ordinals_are_sequential_from_zero():
    doc = "# A\n\n" + _long_section_body("alpha", n_paragraphs=10) + "\n\n# B\n\n" + _long_section_body("beta", n_paragraphs=10)
    records = chunk_document("h", normalize_to_markdown(doc))

    assert [r.ordinal for r in records] == list(range(len(records)))


# ---------------------------------------------------------------------
# Packing: size target + overlap
# ---------------------------------------------------------------------

def test_long_section_produces_multiple_overlapping_chunks():
    doc = "# Long\n\n" + _long_section_body("marker", n_paragraphs=40, words_per_paragraph=30)
    records = chunk_document("h", normalize_to_markdown(doc))

    assert len(records) >= 2
    for record in records:
        assert _token_count(record.text) <= MAX_CHUNK_TOKENS * 1.05  # small slack for a whole-unit boundary

    # Consecutive chunks should share overlap content (last paragraph(s) of
    # chunk i reappear at the start of chunk i+1).
    first_chunk_last_paragraph = records[0].text.split("\n\n")[-1]
    assert first_chunk_last_paragraph in records[1].text


def test_short_section_produces_a_single_chunk():
    doc = "# Short\n\nJust a little bit of text, nothing more.\n"
    records = chunk_document("h", normalize_to_markdown(doc))

    assert len(records) == 1
    assert records[0].heading_path == "Short"


# ---------------------------------------------------------------------
# Persistence round-trip
# ---------------------------------------------------------------------

def test_chunks_persist_and_round_trip_text(conn):
    db.insert_document(
        conn, content_hash="h1", normalized_markdown="# H\n\nBody.\n",
        first_seen_at="now", last_ingested_at="now",
    )
    records = chunk_document("h1", "# H\n\nBody.\n")
    for r in records:
        db.insert_chunk(
            conn, chunk_id=r.chunk_id, content_hash=r.content_hash,
            ordinal=r.ordinal, chunk_hash=r.chunk_hash, text=r.text,
            heading_path=r.heading_path,
        )

    rows = db.get_chunks_for_document(conn, "h1")
    assert len(rows) == 1
    assert rows[0]["text"] == records[0].text
    assert rows[0]["chunk_hash"] == hash_content(records[0].text)


# ---------------------------------------------------------------------
# split_sections (unit-level)
# ---------------------------------------------------------------------

def test_split_sections_skips_empty_sections():
    doc = "# Empty\n\n# NotEmpty\n\nHas content.\n"
    sections = split_sections(normalize_to_markdown(doc))

    assert len(sections) == 1
    assert sections[0].heading_path == "NotEmpty"
