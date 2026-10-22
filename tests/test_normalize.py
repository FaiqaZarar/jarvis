"""J-03 acceptance check: two differently-formatted versions of the same
content, fed through normalization, produce identical content hashes."""

from jarvis_server.internal.normalizer.hashing import hash_content, normalize_to_markdown


def test_identical_hash_across_differently_formatted_versions():
    """The core acceptance criterion: CRLF, trailing whitespace, extra
    blank lines, and a leading BOM must all wash out, leaving the same
    content hash as a cleanly-formatted equivalent."""
    version_a = (
        "# Title  \n"
        "\n"
        "Some text here.   \n"
        "\n"
        "\n"
        "\n"
        "More text.\n"
    )
    version_b = (
        "\ufeff# Title\r\n"
        "\r\n"
        "Some text here.\r\n"
        "\r\n"
        "More text.\r\n"
    )

    hash_a = hash_content(normalize_to_markdown(version_a))
    hash_b = hash_content(normalize_to_markdown(version_b))

    assert hash_a == hash_b


def test_nfc_unicode_normalization():
    """Combining-character vs. precomposed representations of the same
    glyph (e.g. e + combining acute vs. é) must hash identically."""
    precomposed = "café\n"
    decomposed = "cafe\u0301\n"  # 'e' + combining acute accent

    hash_precomposed = hash_content(normalize_to_markdown(precomposed))
    hash_decomposed = hash_content(normalize_to_markdown(decomposed))

    assert hash_precomposed == hash_decomposed


def test_crlf_converted_to_lf():
    normalized = normalize_to_markdown("line one\r\nline two\r\n")
    assert "\r" not in normalized
    assert normalized == "line one\nline two\n"


def test_bom_stripped():
    normalized = normalize_to_markdown("\ufeff# Heading\n")
    assert not normalized.startswith("\ufeff")
    assert normalized == "# Heading\n"


def test_trailing_whitespace_stripped():
    normalized = normalize_to_markdown("Some text.   \nMore text.\t\t\n")
    assert normalized == "Some text.\nMore text.\n"


def test_collapses_three_or_more_blank_lines():
    raw = "Para one.\n\n\n\n\nPara two.\n"
    normalized = normalize_to_markdown(raw)
    assert normalized == "Para one.\n\nPara two.\n"


def test_single_trailing_newline_enforced():
    normalized = normalize_to_markdown("Some content\n\n\n\n")
    assert normalized == "Some content\n"
    normalized_no_trailing = normalize_to_markdown("Some content")
    assert normalized_no_trailing == "Some content\n"


def test_fenced_code_block_blank_lines_protected():
    """Blank lines and trailing whitespace *inside* a fenced code block
    must survive normalization untouched — collapsing them would corrupt
    the code."""
    raw = (
        "Some intro text.\n"
        "\n"
        "```python\n"
        "def foo():\n"
        "\n"
        "\n"
        "\n"
        "    return 1   \n"
        "```\n"
        "\n"
        "Some outro text.\n"
    )
    normalized = normalize_to_markdown(raw)
    assert "def foo():\n\n\n\n    return 1   \n" in normalized


def test_normalization_is_idempotent():
    raw = "# Title\r\n\r\nSome text.   \n\n\n\nMore.\n"
    once = normalize_to_markdown(raw)
    twice = normalize_to_markdown(once)
    assert once == twice


def test_hash_content_matches_manual_sha256():
    import hashlib

    text = "hello world\n"
    assert hash_content(text) == hashlib.sha256(text.encode("utf-8")).hexdigest()
