import json

import pytest

from bist.activity_reports.processor import (
    build_extraction_user_input,
    chunk_pages,
    parse_report_filename,
)


def test_report_filename_parser_extracts_metadata():
    assert parse_report_filename("THYAO_2026_2_1234567.pdf") == (
        "THYAO",
        2026,
        2,
        1234567,
    )
    assert parse_report_filename("thyao_2025_4.pdf") == ("THYAO", 2025, 4, None)
    with pytest.raises(ValueError):
        parse_report_filename("THYAO_2026_8.pdf")


def test_prompt_keeps_nonconsecutive_pdf_page_numbers():
    row = {
        "symbol": "THYAO",
        "year": 2026,
        "period": 2,
        "start_page": 3,
        "end_page": 8,
        "included_pages": [3, 8],
        "text": "[Page 3] Report text",
    }
    assert json.loads(build_extraction_user_input(row))["included_pages"] == [3, 8]


def test_chunks_keep_page_references_and_bound_long_text():
    pages = [{"page": 3, "text": "word " * 1000}, {"page": 8, "text": "other " * 1000}]
    chunks = chunk_pages(pages, target_chars=1000, max_chars=1200, overlap_chars=100)
    assert len(chunks) > 1
    assert all(set(c["included_pages"]) <= {3, 8} for c in chunks)
    assert all(len(c["text"]) < 1300 for c in chunks)
