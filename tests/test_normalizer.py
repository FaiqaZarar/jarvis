"""jarvis_server.internal.normalizer - per-extension converters (Strategy pattern)
+ the Normalize dispatch class."""

from pathlib import Path

from jarvis_server.internal.file_handler.reader import OpenedFile
from jarvis_server.internal.normalizer.converters.csv import CsvNormalizer
from jarvis_server.internal.normalizer.converters.docx import DocxNormalizer
from jarvis_server.internal.normalizer.converters.html import HtmlNormalizer
from jarvis_server.internal.normalizer.converters.pdf import PDFNormalizer
from jarvis_server.internal.normalizer.converters.txt import TxtNormalizer
from jarvis_server.internal.normalizer.normalize import Normalize


def _opened(raw_bytes: bytes, extension: str) -> OpenedFile:
    return OpenedFile(path=Path(f"fake{extension}"), extension=extension, raw_bytes=raw_bytes)


# ---------------------------------------------------------------------
# TxtNormalizer
# ---------------------------------------------------------------------

def test_txt_normalizer_decodes_utf8():
    text, error = TxtNormalizer().convert(_opened("hello world".encode("utf-8"), ".txt"))
    assert error is None
    assert text == "hello world"


def test_txt_normalizer_strips_bom():
    text, error = TxtNormalizer().convert(_opened("﻿hello".encode("utf-8"), ".txt"))
    assert error is None
    assert not text.startswith("﻿")


def test_txt_normalizer_falls_back_for_non_utf8():
    # A short sample is genuinely ambiguous for encoding detection - use
    # enough Latin-1 text that charset-normalizer can identify it confidently.
    raw = "Le café est très chaud aujourd'hui, n'est-ce pas mon ami ?".encode("latin-1")
    text, error = TxtNormalizer().convert(_opened(raw, ".txt"))
    assert error is None
    assert "caf" in text


# ---------------------------------------------------------------------
# CsvNormalizer / HtmlNormalizer (markitdown, no extra dependency)
# ---------------------------------------------------------------------

def test_csv_normalizer_produces_a_markdown_table():
    raw = b"name,role,city\nAda,engineer,London\nGrace,admiral,New York\n"
    text, error = CsvNormalizer().convert(_opened(raw, ".csv"))
    assert error is None
    assert "| name | role | city |" in text
    assert "Ada" in text and "New York" in text


def test_html_normalizer_produces_markdown():
    raw = b"<html><body><h1>Quarterly Review</h1><p>Revenue up <b>12 percent</b>.</p></body></html>"
    text, error = HtmlNormalizer().convert(_opened(raw, ".html"))
    assert error is None
    assert "# Quarterly Review" in text
    assert "**12 percent**" in text


# ---------------------------------------------------------------------
# Empty files bypass MarkItDown entirely: magika's native ONNX inference
# can abort the whole process on malformed input, and an empty file isn't
# a real per-file failure worth risking that for.
# ---------------------------------------------------------------------

def test_csv_normalizer_skips_markitdown_for_empty_file():
    text, error = CsvNormalizer().convert(_opened(b"", ".csv"))
    assert error is None
    assert text == ""


def test_html_normalizer_skips_markitdown_for_empty_file():
    text, error = HtmlNormalizer().convert(_opened(b"", ".html"))
    assert error is None
    assert text == ""


def test_docx_normalizer_skips_markitdown_for_empty_file():
    text, error = DocxNormalizer().convert(_opened(b"", ".docx"))
    assert error is None
    assert text == ""


def test_pdf_normalizer_skips_markitdown_for_empty_file():
    text, error = PDFNormalizer().convert(_opened(b"", ".pdf"))
    assert error is None
    assert text == ""


# ---------------------------------------------------------------------
# Normalize dispatch
# ---------------------------------------------------------------------

def test_normalize_dispatches_every_supported_extension_to_its_converter():
    normalizer = Normalize()
    assert isinstance(normalizer.converters[".md"], TxtNormalizer)
    assert isinstance(normalizer.converters[".txt"], TxtNormalizer)
    assert isinstance(normalizer.converters[".docx"], DocxNormalizer)
    assert isinstance(normalizer.converters[".pdf"], PDFNormalizer)
    assert isinstance(normalizer.converters[".csv"], CsvNormalizer)
    assert isinstance(normalizer.converters[".html"], HtmlNormalizer)


def test_normalize_returns_markdown_for_supported_extension():
    md_content, err_msg, reason = Normalize().normalize(_opened(b"# Heading\n", ".md"))
    assert err_msg is None
    assert reason is None
    assert md_content == "# Heading\n"


def test_normalize_returns_error_not_exception_for_unsupported_extension():
    md_content, err_msg, reason = Normalize().normalize(_opened(b"binary junk", ".png"))
    assert md_content is None
    assert "Unsupported" in err_msg
    assert reason == "unsupported"


def test_normalize_flags_a_dispatched_converter_failure_distinctly(monkeypatch):
    """A converter that was actually dispatched and failed (issue #115)
    must not read the same as an unsupported extension - callers rely on
    `reason` to tell a real ingest failure from a legitimate skip."""

    def failing_convert(self, file):
        return None, "boom"

    monkeypatch.setattr(DocxNormalizer, "convert", failing_convert)

    md_content, err_msg, reason = Normalize().normalize(_opened(b"docx bytes", ".docx"))

    assert md_content is None
    assert err_msg == "boom"
    assert reason == "failed"


def test_normalize_routes_to_the_correct_converter(monkeypatch):
    """Adding/registering a converter is the only thing that changes which
    class handles an extension - verify normalize() actually calls through
    the dispatched converter rather than hardcoding behavior."""
    called_with = {}

    def fake_convert(self, file):
        called_with["file"] = file
        return "fake markdown", None

    monkeypatch.setattr(DocxNormalizer, "convert", fake_convert)

    file = _opened(b"docx bytes", ".docx")
    md_content, err_msg, reason = Normalize().normalize(file)

    assert err_msg is None
    assert reason is None
    assert md_content == "fake markdown"
    assert called_with["file"] is file
