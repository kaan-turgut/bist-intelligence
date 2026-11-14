import argparse
import hashlib
import json
import logging
import os
import re
import traceback
from concurrent.futures import (
    FIRST_COMPLETED,
    ProcessPoolExecutor,
    ThreadPoolExecutor,
    wait,
)
from pathlib import Path

import pdfplumber
import requests
import unidecode

from bist.activity_reports.store import (
    existing_chunk_signature,
    extraction_summary,
    fetch_pending_chunks,
    file_sha256,
    ingested_document_keys,
    init_activity_report_tables,
    insert_extractions,
    pipeline_run,
    record_extraction_failure,
    replace_chunks,
    replace_pages,
    resolve_company_ids,
    store_embedding,
    upsert_document,
    writer_connection,
)
from bist.companies.metadata_store import connect_metadata_db
from bist.http_utils import RateGate, send_with_retry
from bist.logging_config import configure_logging
from bist.paths import ACTIVITY_REPORTS_DIR

logger = logging.getLogger(__name__)

DEFAULT_EXTRACTION_MODEL = "gpt-6-luna"
DEFAULT_EMBEDDING_MODEL = "text-embedding-3-small"
DEFAULT_EXTRACTION_VERSION = "gpt6_luna_v2"
DEFAULT_CHUNK_TARGET_CHARS = 9000
DEFAULT_CHUNK_MAX_CHARS = 12000
DEFAULT_CHUNK_OVERLAP_CHARS = 700
OPENAI_CHAT_COMPLETIONS_URL = "https://api.openai.com/v1/chat/completions"
OPENAI_EMBEDDINGS_URL = "https://api.openai.com/v1/embeddings"
OPENAI_RETRIES = 6
OPENAI_RETRY_BACKOFF = 10
MAX_CONSECUTIVE_FAILURES = 5
# Measured on Turkish activity reports (pilot of 373 chunks).
CHARS_PER_TOKEN = 2.25
DEFAULT_API_WORKERS = 6
# Shared by every thread: a 429 pauses all requests, and starts are spaced apart.
OPENAI_GATE = RateGate()
FATAL_HTTP_STATUS = {401, 403, 404}

EXCLUDE_PAGE_KEYWORDS = [
    "finansal durum tablosu",
    "kar veya zarar tablosu",
    "kapsamli gelir tablosu",
    "kurumsal yonetim ilkeleri",
    "ozkaynaklar degisim",
    "veya zararda yeniden si",
    "icindekiler",
    "misyonumuz",
    "vizyonumuz",
    "degerlerimiz",
    "turev finansal",
    "finansal tablo dipnotlari",
    "bagimsiz denetci gorusu",
    "bagimsiz denetci raporu",
    "denetim konulari",
    "bagimsizlik beyan",
    "olarak gorev aldi",
    "yuksek lisans",
    "mezun ol",
    "egitim gordu",
    "raporu uygunlugu hakkinda",
]

CATEGORIES = [
    "operations",
    "strategy",
    "risk",
    "regulation",
    "market",
    "governance",
    "ESG",
    "investment",
    "capacity",
    "supply_chain",
    "financing",
    "legal",
    "human_capital",
    "technology",
    "other",
]

EVENT_TYPES = [
    "expansion",
    "contraction",
    "new_market",
    "product_launch",
    "capacity_increase",
    "cost_pressure",
    "regulatory_change",
    "litigation",
    "management_change",
    "supply_disruption",
    "demand_growth",
    "demand_weakness",
    "capex_plan",
    "efficiency_program",
    "financing_change",
    "governance_change",
    "operational_update",
    "market_outlook",
    "risk_disclosure",
    "other",
]

THEMES = [
    "demand",
    "pricing",
    "market_share",
    "competition",
    "products_services",
    "customers",
    "exports",
    "international_expansion",
    "capacity",
    "production",
    "capex",
    "acquisitions_divestments",
    "subsidiaries_affiliates",
    "partnerships_contracts",
    "costs",
    "energy",
    "raw_materials",
    "supply_chain_logistics",
    "debt_financing",
    "equity_capital",
    "dividends_buybacks",
    "credit_rating",
    "fx_exposure",
    "interest_rates",
    "inflation",
    "macro_economy",
    "geopolitics",
    "regulation_policy",
    "tax_incentives",
    "litigation",
    "workforce",
    "management",
    "board_governance",
    "risk_management_internal_control",
    "capital_liquidity",
    "lending_deposits",
    "asset_quality",
    "insurance_premiums_claims",
    "cybersecurity_data_privacy",
    "ownership_shareholders",
    "related_parties",
    "sustainability_emissions",
    "digitalization_technology",
    "research_development",
    "guidance_targets",
    "other",
]

EXTRACTION_SYSTEM_PROMPT = """
You extract analyst-relevant information from public company activity reports.
Return only facts that help understand operations, strategy, risks, governance,
market position, regulation, investment plans, ESG, or future outlook.

Do not extract ordinary financial-statement line items such as sales, net profit,
assets, equity, margins, or ratios when they can be obtained from statements.
Do not extract marketing language, awards, slogans, social-charity work, or
generic self-praise. Prefer specific, dated developments, decisions, targets and
figures over static descriptions (company history, mission, general structure) that
are repeated in every report. Use English for summaries. Keep company and place names
in their original language. Avoid duplicate extractions within the same input.

State only what the text says. Do not add purposes, motivations, effects, causes or
interpretations that the text does not state, and do not merge separate statements
into a claim the text does not make. source_quote must be copied verbatim from the
text; use " ... " only to join separate verbatim excerpts.

report_type tells you whether this is an annual report (full year, often long and
repetitive) or an interim report (Q1-Q3).

Fields:
- themes: 1-3 values from the fixed list that best describe the fact.
- entities: specific named things the fact is about (subsidiaries, products, brands,
  plants, projects, counterparties, places), as written in the report; [] if none.
- metrics: every concrete figure stated in the fact (counts, capacities, amounts,
  percentages, targets), one entry per figure. name is a short English snake_case
  label (e.g. fleet_size, installed_capacity_mw, capex_target). value is the number
  as stated; unit is its unit or currency (e.g. aircraft, MW, TRY million, %); period
  is the date or period the figure refers to as written (e.g. 2025, 2026 target,
  31.12.2025), or null if not stated. Use [] when the fact has no figure.
- magnitude: importance for an equity analyst, using the whole scale:
  1-2 routine or descriptive detail; 3-4 notable detail of one business line;
  5-6 significant development for a business line or a material risk factor;
  7-8 material for the whole company (major investment, large contract, new market,
  guidance or strategy change, significant financing); 9-10 transformative
  (merger, major restructuring, threat to the business).
""".strip()

NULLABLE_STRING = {"type": ["string", "null"]}

EXTRACTION_SCHEMA = {
    "name": "activity_report_extractions",
    "strict": True,
    "schema": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "Extractions": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "page": {"type": "integer"},
                        "category": {"type": "string", "enum": CATEGORIES},
                        "event_type": {"type": "string", "enum": EVENT_TYPES},
                        "themes": {
                            "type": "array",
                            "items": {"type": "string", "enum": THEMES},
                            "minItems": 1,
                            "maxItems": 3,
                        },
                        "entities": {
                            "type": "array",
                            "items": {"type": "string"},
                            "maxItems": 6,
                        },
                        "metrics": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "additionalProperties": False,
                                "properties": {
                                    "name": {"type": "string"},
                                    "value": {"type": "number"},
                                    "unit": NULLABLE_STRING,
                                    "period": NULLABLE_STRING,
                                },
                                "required": ["name", "value", "unit", "period"],
                            },
                        },
                        "sentiment": {
                            "type": "string",
                            "enum": ["positive", "negative", "neutral", "mixed"],
                        },
                        "magnitude": {"type": "integer", "minimum": 1, "maximum": 10},
                        "time_relevance": {
                            "type": "string",
                            "enum": [
                                "current_quarter",
                                "past_event",
                                "general_info",
                                "future_outlook",
                            ],
                        },
                        "source_quote": {"type": "string"},
                        "info": {"type": "string"},
                    },
                    "required": [
                        "page",
                        "category",
                        "event_type",
                        "themes",
                        "entities",
                        "metrics",
                        "sentiment",
                        "magnitude",
                        "time_relevance",
                        "source_quote",
                        "info",
                    ],
                },
            }
        },
        "required": ["Extractions"],
    },
}


def get_openai_api_key():
    key = os.environ.get("OPENAI_API_KEY")
    if not key:
        raise RuntimeError("Set OPENAI_API_KEY in the environment before extraction.")
    return key


class OpenAIError(RuntimeError):
    """An OpenAI request failed. ``fatal`` means every later request would fail too."""

    def __init__(self, message, fatal=False):
        super().__init__(message)
        self.fatal = fatal


def post_openai(url, payload, *, retries=OPENAI_RETRIES, backoff=OPENAI_RETRY_BACKOFF):
    """POST with retries on 429/5xx/network errors; never retries permanent errors."""
    api_key = get_openai_api_key()

    def send():
        return requests.post(
            url,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=120,
        )

    try:
        return send_with_retry(
            send,
            retries=retries,
            retry_backoff=backoff,
            rate_limit_backoff=backoff * 3,
            label=url,
            gate=OPENAI_GATE,
            # An exhausted quota will not recover by waiting a few seconds.
            retry_if=lambda response: "insufficient_quota" not in response.text,
        )
    except requests.HTTPError as exc:
        response = exc.response
        body = response.text[:1000] if response is not None else str(exc)
        status = response.status_code if response is not None else None
        fatal = status in FATAL_HTTP_STATUS or "insufficient_quota" in body
        raise OpenAIError(
            f"OpenAI request failed (HTTP {status}): {body}", fatal=fatal
        ) from exc
    except requests.RequestException as exc:
        raise OpenAIError(f"OpenAI request failed: {exc}") from exc


def extraction_prompt_hash():
    prompt_payload = {
        "system_prompt": EXTRACTION_SYSTEM_PROMPT,
        "schema": EXTRACTION_SCHEMA,
    }
    return hashlib.sha256(
        json.dumps(prompt_payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()


def parse_report_filename(path):
    stem = Path(path).stem
    parts = stem.split("_")
    if len(parts) < 3:
        raise ValueError(f"Unexpected activity report filename: {path}")

    symbol = parts[0].strip().upper()
    year = int(parts[1])
    period = int(parts[2])
    if period not in (1, 2, 3, 4):
        raise ValueError(f"Invalid activity report quarter: {path}")
    disclosure_index = int(parts[3]) if len(parts) > 3 and parts[3].isdigit() else None
    return symbol, year, period, disclosure_index


def list_activity_report_pdfs(symbols=None, root_dir=ACTIVITY_REPORTS_DIR):
    root_dir = Path(root_dir)
    symbols_set = {symbol.upper() for symbol in symbols} if symbols else None
    pdfs = []
    if not root_dir.exists():
        return pdfs

    for path in root_dir.rglob("*.pdf"):
        try:
            symbol, year, period, disclosure_index = parse_report_filename(path)
        except ValueError:
            logger.warning(
                "Skipping activity report with unexpected filename: %s", path
            )
            continue
        if symbols_set and symbol.upper() not in symbols_set:
            continue
        pdfs.append((path, symbol, year, period, disclosure_index))

    return sorted(pdfs)


def page_should_keep(text, min_chars=500):
    if len(text.strip()) < min_chars:
        return False

    normalized = unidecode.unidecode(text).lower()
    return not any(keyword in normalized for keyword in EXCLUDE_PAGE_KEYWORDS)


def extract_pdf_pages(path, min_chars=500):
    pages = []
    with pdfplumber.open(path) as pdf:
        for index, page in enumerate(pdf.pages, start=1):
            # Text-flow order follows the PDF's content stream, which keeps
            # multi-column layouts readable; the default reads straight across columns
            # and interleaves them (measured: 85-89% vs 46-66% of quotes matching).
            text = page.extract_text(use_text_flow=True) or ""
            text = "\n".join(line.rstrip() for line in text.splitlines()).strip()
            if page_should_keep(text, min_chars=min_chars):
                pages.append({"page": index, "text": text, "char_count": len(text)})
            page.flush_cache()

    return pages


def looks_like_heading(line):
    cleaned = line.strip()
    if not cleaned or len(cleaned) > 120:
        return False
    if cleaned.endswith((".", ",", ";", ":")):
        return False
    letters = [char for char in cleaned if char.isalpha()]
    if len(letters) < 4:
        return False
    uppercase_ratio = sum(char.isupper() for char in letters) / len(letters)
    title_words = len(cleaned.split()) <= 10 and uppercase_ratio > 0.45
    numbered_heading = bool(re.match(r"^\d+(\.\d+)*\s+\S+", cleaned))
    return title_words or numbered_heading


def split_text_blocks(text, soft_block_chars=900):
    blocks = []
    current = []
    current_chars = 0

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            if current:
                blocks.append(" ".join(current).strip())
                current = []
                current_chars = 0
            continue

        if current and looks_like_heading(line):
            blocks.append(" ".join(current).strip())
            current = [line]
            current_chars = len(line)
            continue

        current.append(line)
        current_chars += len(line) + 1
        if current_chars >= soft_block_chars and line.endswith(
            (".", ":", ";", "!", "?")
        ):
            blocks.append(" ".join(current).strip())
            current = []
            current_chars = 0

    if current:
        blocks.append(" ".join(current).strip())

    return [block for block in blocks if block]


def split_long_text(text, max_chars):
    parts = []
    remaining = text.strip()
    while len(remaining) > max_chars:
        split_at = remaining.rfind(". ", 0, max_chars)
        if split_at < max_chars * 0.5:
            split_at = remaining.rfind(" ", 0, max_chars)
        if split_at < max_chars * 0.5:
            split_at = max_chars
        parts.append(remaining[:split_at].strip())
        remaining = remaining[split_at:].strip()
    if remaining:
        parts.append(remaining)
    return parts


def build_text_segments(pages, max_chars):
    segments = []
    for page in pages:
        page_header = f"Page {page['page']}\n"
        segment_max_chars = max(1000, max_chars - len(page_header))
        for block in split_text_blocks(page["text"]):
            for part in split_long_text(block, max_chars=segment_max_chars):
                segments.append(
                    {
                        "page": page["page"],
                        "text": f"{page_header}{part}",
                        "is_overlap": False,
                    }
                )
    return segments


def unique_pages(segments):
    return sorted(
        {segment["page"] for segment in segments if not segment.get("is_overlap")}
    )


def chunk_from_segments(segments):
    pages = unique_pages(segments)
    text = "\n\n".join(segment["text"] for segment in segments)
    return {
        "start_page": pages[0],
        "end_page": pages[-1],
        "included_pages": pages,
        "text": text,
    }


def overlap_segments(segments, overlap_chars):
    if not overlap_chars:
        return []

    selected = []
    selected_chars = 0
    for segment in reversed(segments):
        selected.insert(
            0,
            {
                "page": segment["page"],
                "text": segment["text"],
                "is_overlap": True,
            },
        )
        selected_chars += len(segment["text"])
        if selected_chars >= overlap_chars:
            break

    return selected


def chunk_pages(
    pages,
    pages_per_chunk=None,
    target_chars=DEFAULT_CHUNK_TARGET_CHARS,
    max_chars=DEFAULT_CHUNK_MAX_CHARS,
    overlap_chars=DEFAULT_CHUNK_OVERLAP_CHARS,
):
    chunks = []
    current_segments = []
    current_chars = 0
    max_pages_per_chunk = (
        pages_per_chunk if pages_per_chunk and pages_per_chunk > 0 else None
    )

    for segment in build_text_segments(pages, max_chars=max_chars):
        actual_current_pages = unique_pages(current_segments)
        would_exceed_chars = (
            current_chars + len(segment["text"]) > target_chars and current_segments
        )
        would_exceed_page_count = (
            max_pages_per_chunk
            and segment["page"] not in actual_current_pages
            and len(actual_current_pages) >= max_pages_per_chunk
        )
        if would_exceed_chars or would_exceed_page_count:
            chunks.append(chunk_from_segments(current_segments))
            current_segments = overlap_segments(current_segments, overlap_chars)
            current_chars = sum(len(item["text"]) for item in current_segments)
            if current_segments and current_chars + len(segment["text"]) > max_chars:
                current_segments = []
                current_chars = 0

        current_segments.append(segment)
        current_chars += len(segment["text"])

    if current_segments:
        chunks.append(chunk_from_segments(current_segments))

    return chunks


def text_hash(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def chunk_signature(chunks):
    return [
        (
            chunk["start_page"],
            chunk["end_page"],
            tuple(chunk.get("included_pages", [])),
            text_hash(chunk["text"]),
        )
        for chunk in chunks
    ]


MAX_AUTO_WORKERS = 8


def default_ingest_workers():
    return max(1, min((os.cpu_count() or 2) - 1, MAX_AUTO_WORKERS))


def parse_and_chunk_pdf(path, min_chars, chunk_options):
    pages = extract_pdf_pages(path, min_chars=min_chars)
    return pages, chunk_pages(pages, **chunk_options)


def _parse_in_worker(path, min_chars, chunk_options):
    try:
        return True, parse_and_chunk_pdf(path, min_chars, chunk_options)
    except Exception:  # noqa: BLE001 - reported to the parent with its traceback
        return False, traceback.format_exc()


def parsed_pdfs(pdfs, workers, min_chars, chunk_options):
    """Yield ``(pdf, ok, payload)``; payload is ``(pages, chunks)``.

    PDF parsing is CPU-bound pure Python, so threads would be serialized by the GIL.
    Worker processes parse in parallel while the caller writes to the database.
    Results are yielded as soon as any worker finishes (not in input order), so one
    long annual report never leaves the other workers idle. The largest files start
    first to avoid a long tail at the end. Only ``2 * workers`` PDFs are in flight,
    which caps memory use.
    """
    if workers <= 1:
        for pdf in pdfs:
            try:
                result = (True, parse_and_chunk_pdf(pdf[0], min_chars, chunk_options))
            except Exception:  # noqa: BLE001 - reported to the caller
                result = (False, traceback.format_exc())
            yield pdf, *result
        return

    remaining = iter(
        sorted(pdfs, key=lambda pdf: Path(pdf[0]).stat().st_size, reverse=True)
    )
    pending = {}
    with ProcessPoolExecutor(max_workers=workers) as pool:

        def submit_next():
            pdf = next(remaining, None)
            if pdf is not None:
                future = pool.submit(
                    _parse_in_worker, str(pdf[0]), min_chars, chunk_options
                )
                pending[future] = pdf

        for _ in range(workers * 2):
            submit_next()
        while pending:
            done, _ = wait(pending, return_when=FIRST_COMPLETED)
            for future in done:
                pdf = pending.pop(future)
                try:
                    result = future.result()
                except Exception:  # noqa: BLE001 - e.g. a worker killed by the OS
                    result = (False, traceback.format_exc())
                submit_next()
                yield pdf, *result


def ingest_activity_reports(
    symbols=None,
    root_dir=ACTIVITY_REPORTS_DIR,
    min_chars=500,
    pages_per_chunk=None,
    target_chars=DEFAULT_CHUNK_TARGET_CHARS,
    max_chars=DEFAULT_CHUNK_MAX_CHARS,
    overlap_chars=DEFAULT_CHUNK_OVERLAP_CHARS,
    force=False,
    workers=1,
    only_new=True,
):
    if workers < 1:
        raise ValueError("workers must be at least 1")
    if not 0 <= overlap_chars < target_chars <= max_chars or min_chars < 0:
        raise ValueError(
            "Require 0 <= overlap < target <= max characters and min_chars >= 0"
        )
    from bist.companies.loader import load_companies, resolve_symbols, symbol_aliases

    requested = set(resolve_symbols(symbols)) if symbols else None
    companies = load_companies()
    aliases = symbol_aliases()
    pdfs = []
    for path, symbol, year, period, disclosure_index in list_activity_report_pdfs(
        root_dir=root_dir
    ):
        canonical = symbol if symbol in companies else aliases.get(symbol)
        if requested and canonical not in requested:
            continue
        if canonical is None:
            raise ValueError(f"No shared PostgreSQL company for PDF ticker {symbol}")
        pdfs.append((path, canonical, year, period, disclosure_index))
    if not pdfs:
        raise FileNotFoundError(
            "No activity-report PDFs found for the requested companies."
        )
    with (
        writer_connection() as conn,
        pipeline_run(
            conn,
            "activity_report_ingestion",
            {"symbols": symbols, "only_new": only_new, "force": force},
        ) as run_id,
    ):
        if only_new:
            known = ingested_document_keys(conn)
            total = len(pdfs)
            pdfs = [
                pdf
                for pdf in pdfs
                if (pdf[1], pdf[2], pdf[3], file_sha256(pdf[0])) not in known
            ]
            logger.info(
                "%s of %s PDFs are already ingested and skipped; %s to process "
                "(use --no-only-new to reprocess everything)",
                total - len(pdfs),
                total,
                len(pdfs),
            )
            if not pdfs:
                return 0
        failures = []
        chunk_options = {
            "pages_per_chunk": pages_per_chunk,
            "target_chars": target_chars,
            "max_chars": max_chars,
            "overlap_chars": overlap_chars,
        }
        workers = min(workers, len(pdfs))
        logger.info("Ingesting %s PDFs with %s worker(s)", len(pdfs), workers)
        results = parsed_pdfs(pdfs, workers, min_chars, chunk_options)
        for position, (pdf, parsed, payload) in enumerate(results, start=1):
            path, symbol, year, period, disclosure_index = pdf
            if not parsed:
                logger.error("Failed to parse %s; continuing\n%s", path, payload)
                failures.append(f"{path}: {payload.strip().splitlines()[-1]}")
                continue
            pages, chunks = payload
            try:
                with conn.transaction():
                    document_id = upsert_document(
                        conn,
                        path,
                        symbol,
                        year,
                        period,
                        disclosure_index,
                        run_id=run_id,
                    )
                    replace_pages(conn, document_id, pages)
                    changed = existing_chunk_signature(
                        conn, document_id
                    ) != chunk_signature(chunks)
                    if force or changed:
                        replace_chunks(conn, document_id, chunks)
            except Exception as exc:
                logger.exception("Failed to store %s; continuing", path)
                failures.append(f"{path}: {exc!r}")
                continue
            logger.info(
                "[%s/%s] %s: stored %s pages and %s chunks (%s)",
                position,
                len(pdfs),
                path.name,
                len(pages),
                len(chunks),
                "updated" if force or changed else "unchanged",
            )
        if failures:
            raise RuntimeError(
                f"{len(failures)} of {len(pdfs)} PDFs failed to ingest; the rest "
                f"were saved. First error: {failures[0]}"
            )
    return len(pdfs)


def clear_activity_report_data(symbols=None):
    with (
        writer_connection() as conn,
        pipeline_run(conn, "activity_report_clear", {"symbols": symbols}),
    ):
        company_ids = resolve_company_ids(conn, symbols)
        if company_ids is None:
            conn.execute("DELETE FROM bronze.activity_report_documents")
        else:
            conn.execute(
                "DELETE FROM bronze.activity_report_documents WHERE company_id=ANY(%s)",
                (company_ids,),
            )


def build_extraction_user_input(row):
    return json.dumps(
        {
            "company_symbol": row["symbol"],
            "document_type": "activity_report",
            "year": row["year"],
            "period": row["period"],
            "report_type": (
                "annual report (full year)"
                if row["period"] == 4
                else f"interim report (Q{row['period']})"
            ),
            "start_page": row["start_page"],
            "end_page": row["end_page"],
            "included_pages": row["included_pages"],
            "instructions": (
                "Extract facts only from the pages listed in included_pages. Text "
                "from any other page is overlap context repeated from a neighbouring "
                "chunk and is extracted there; use it only to understand the text."
            ),
            "text": row["text"],
        },
        ensure_ascii=False,
    )


def model_uses_gpt6_reasoning(model):
    return model.startswith("gpt-6")


def model_supports_no_reasoning(model):
    return model.startswith(("gpt-6-luna", "gpt-6-sol"))


def chat_json(system_prompt, user_input, schema, model):
    """One chat completion constrained to ``schema``; returns ``(parsed, usage)``."""
    payload = {
        "model": model,
        "response_format": {"type": "json_schema", "json_schema": schema},
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_input},
        ],
    }
    if model_uses_gpt6_reasoning(model):
        if model_supports_no_reasoning(model):
            payload["reasoning_effort"] = "none"
        else:
            payload["reasoning_effort"] = "low"
    else:
        payload["temperature"] = 0

    data = post_openai(OPENAI_CHAT_COMPLETIONS_URL, payload).json()
    choice = data["choices"][0]
    message = choice["message"]
    if message.get("refusal"):
        raise OpenAIError(f"Model refused the request: {message['refusal']}")
    if choice.get("finish_reason") == "length":
        raise OpenAIError("Response was truncated (finish_reason=length)")
    content = message.get("content")
    if not content:
        raise OpenAIError(
            f"Empty response content (finish_reason={choice.get('finish_reason')})"
        )
    return json.loads(content), data.get("usage") or {}


def call_openai_extraction(chunk_input, model=DEFAULT_EXTRACTION_MODEL):
    result, usage = chat_json(
        EXTRACTION_SYSTEM_PROMPT, chunk_input, EXTRACTION_SCHEMA, model
    )
    result["_usage"] = usage  # stored with the run by insert_extractions
    return result


def is_fatal(exc):
    return isinstance(exc, (OpenAIError, RuntimeError)) and getattr(exc, "fatal", False)


def raise_for_failures(task, failures, attempted, total):
    """Fail the pipeline run at the end, after all salvageable work was saved."""
    if not failures:
        return
    not_attempted = total - attempted
    raise RuntimeError(
        f"{len(failures)} of {total} {task} failed"
        + (f" and {not_attempted} were not attempted" if not_attempted else "")
        + "; completed work is saved and a rerun retries only the rest. "
        f"First error: {failures[0]}"
    )


def run_concurrently(
    rows, call, on_success, on_failure, *, workers, max_consecutive_failures
):
    """Run ``call(row)`` for each row in worker threads; save results in this thread.

    Only the API call runs in the pool. ``on_success`` and ``on_failure`` (which use
    the single database connection) always run in the calling thread, so the
    connection is never shared. At most ``workers`` requests are in flight.

    Nothing paid for is thrown away: after a fatal error, too many consecutive
    failures, or Ctrl+C, no new request is started, but requests already in flight
    are still collected and saved. A second Ctrl+C aborts immediately.

    Returns ``(failures, attempted)``.
    """
    failures = []
    attempted = 0
    consecutive = 0
    stopping = False
    interrupted = False
    pending = {}
    remaining = iter(rows)
    # Failures can arrive in a burst when several requests are in flight.
    failure_limit = max(max_consecutive_failures, workers)

    with ThreadPoolExecutor(max_workers=workers) as pool:

        def submit_next():
            nonlocal attempted
            if stopping:
                return
            row = next(remaining, None)
            if row is not None:
                attempted += 1
                pending[pool.submit(call, row)] = row

        for _ in range(workers):
            submit_next()
        while pending:
            try:
                done, _ = wait(pending, return_when=FIRST_COMPLETED)
            except KeyboardInterrupt:
                if interrupted:
                    raise
                interrupted = stopping = True
                logger.warning(
                    "Interrupted: saving the %s request(s) already in flight; "
                    "press Ctrl+C again to abort immediately",
                    len(pending),
                )
                continue
            for future in done:
                row = pending.pop(future)
                try:
                    on_success(row, future.result())
                except Exception as exc:
                    on_failure(row, exc)
                    label = row.get("label") or f"chunk {row.get('chunk_id')}"
                    failures.append(f"{label}: {exc!r}")
                    consecutive += 1
                    logger.exception("%s failed", label)
                    if is_fatal(exc) and not stopping:
                        logger.error("Error will repeat for every request; stopping")
                        stopping = True
                    elif consecutive >= failure_limit and not stopping:
                        logger.error("%s consecutive failures; stopping", consecutive)
                        stopping = True
                else:
                    consecutive = 0
                submit_next()
    if interrupted:
        raise KeyboardInterrupt
    return failures, attempted


def measured_usage(conn, model, extraction_version, prompt_hash):
    """Actual average tokens per completed request, if any usage was recorded."""
    try:
        return conn.execute(
            """SELECT count(*) AS runs, avg(r.input_tokens) AS input_tokens,
                      avg(r.output_tokens) AS output_tokens,
                      avg(length(c.text)) AS chars
               FROM ops.activity_report_extraction_runs r
               JOIN silver.activity_report_chunks c ON c.id=r.chunk_id
               WHERE r.model=%s AND r.extraction_version=%s AND r.prompt_hash=%s
                 AND r.status='completed' AND r.input_tokens IS NOT NULL""",
            (model, extraction_version, prompt_hash),
        ).fetchone()
    except Exception:  # noqa: BLE001 - usage columns not migrated yet
        conn.rollback()
        return None


def summarize_totals(rows):
    return {
        key: sum(row[key] for row in rows)
        for key in (
            "active_chunks",
            "extracted_chunks",
            "pending_chunks",
            "pending_reports",
            "pending_chars",
        )
    }


def summarize_extraction(symbols, model, extraction_version, force=False, years=None):
    """Read-only: what is already extracted (and skipped) and what would be sent."""
    prompt_hash = extraction_prompt_hash()
    with connect_metadata_db() as conn:
        rows = extraction_summary(
            conn, symbols, model, extraction_version, prompt_hash, force, years
        )
        totals = summarize_totals(rows)
        totals["measured"] = measured_usage(
            conn, model, extraction_version, prompt_hash
        )
    return rows, totals


def log_extraction_summary(totals):
    logger.info(
        "Already extracted and skipped: %s of %s chunks. To process: %s chunks in %s "
        "reports (about %.1fM input tokens, rough estimate)",
        totals["extracted_chunks"],
        totals["active_chunks"],
        totals["pending_chunks"],
        totals["pending_reports"],
        totals["pending_chars"] / CHARS_PER_TOKEN / 1e6,
    )


def print_extraction_dry_run(rows, totals):
    print(
        f"{'symbol':<10}{'extracted':>10}{'pending':>9}{'reports':>9}{'~Mtokens':>10}"
    )
    for row in rows:
        print(
            f"{row['symbol']:<10}{row['extracted_chunks']:>10}"
            f"{row['pending_chunks']:>9}{row['pending_reports']:>9}"
            f"{row['pending_chars'] / CHARS_PER_TOKEN / 1e6:>10.2f}"
        )
    print(
        f"{'TOTAL':<10}{totals['extracted_chunks']:>10}{totals['pending_chunks']:>9}"
        f"{totals['pending_reports']:>9}{totals['pending_chars'] / CHARS_PER_TOKEN / 1e6:>10.2f}"
    )
    print(
        "No API calls were made. Token figures are rough (about 2.25 characters per token, as measured)."
    )


def extract_activity_report_facts(
    symbols=None,
    model=DEFAULT_EXTRACTION_MODEL,
    extraction_version=DEFAULT_EXTRACTION_VERSION,
    limit=None,
    request_sleep=0.25,
    force=False,
    max_consecutive_failures=MAX_CONSECUTIVE_FAILURES,
    workers=1,
    years=None,
):
    """Extract facts chunk by chunk, with ``workers`` API requests in flight.

    Each successful chunk is committed immediately, so a later error never discards
    paid results. A failed chunk is recorded and skipped; the run stops early only
    for errors that will repeat (bad key, exhausted quota, unknown model) or after
    repeated consecutive failures, so tokens are not wasted on requests that cannot
    succeed. The run is reported as failed at the end if any chunk failed, and a
    rerun picks up only the chunks that are still pending.

    ``request_sleep`` is the minimum spacing between request starts across all
    workers; an HTTP 429 pauses every worker.
    """
    if workers < 1:
        raise ValueError("workers must be at least 1")
    prompt_hash = extraction_prompt_hash()
    with (
        writer_connection() as conn,
        pipeline_run(
            conn,
            "activity_report_extraction",
            {
                "symbols": symbols,
                "model": model,
                "extraction_version": extraction_version,
                "prompt_hash": prompt_hash,
                "force": force,
                "workers": workers,
                "years": years,
            },
        ) as run_id,
    ):
        log_extraction_summary(
            summarize_totals(
                extraction_summary(
                    conn, symbols, model, extraction_version, prompt_hash, force, years
                )
            )
        )
        rows = fetch_pending_chunks(
            conn,
            symbols,
            limit,
            model,
            extraction_version,
            force,
            prompt_hash,
            years=years,
        )
        conn.commit()  # Never keep a transaction open during an API request.
        workers = max(1, min(workers, len(rows)))
        logger.info(
            "Found %s pending chunks for extraction (%s worker(s))", len(rows), workers
        )
        OPENAI_GATE.interval = request_sleep
        completed = 0
        tokens = {"input": 0, "output": 0}

        def call(row):
            return call_openai_extraction(build_extraction_user_input(row), model=model)

        def on_success(row, result):
            nonlocal completed
            with conn.transaction():
                insert_extractions(
                    conn,
                    row,
                    result,
                    model,
                    extraction_version,
                    prompt_hash,
                    run_id=run_id,
                )
            completed += 1
            usage = result.get("_usage") or {}
            tokens["input"] += usage.get("prompt_tokens") or 0
            tokens["output"] += usage.get("completion_tokens") or 0
            logger.info(
                "[%s/%s] Saved %s %s Q%s pages %s-%s",
                completed,
                len(rows),
                row["symbol"],
                row["year"],
                row["period"],
                row["start_page"],
                row["end_page"],
            )

        def on_failure(row, exc):
            conn.rollback()
            record_extraction_failure(
                conn,
                row["chunk_id"],
                model,
                extraction_version,
                prompt_hash,
                exc,
                run_id,
            )
            conn.commit()

        failures, attempted = run_concurrently(
            rows,
            call,
            on_success,
            on_failure,
            workers=workers,
            max_consecutive_failures=max_consecutive_failures,
        )
        if completed:
            logger.info(
                "Token usage: %s input, %s output (%s and %s per chunk)",
                tokens["input"],
                tokens["output"],
                tokens["input"] // completed,
                tokens["output"] // completed,
            )
        raise_for_failures("chunks", failures, attempted, len(rows))
    return len(rows)


def call_openai_embedding(text, model=DEFAULT_EMBEDDING_MODEL):
    response = post_openai(OPENAI_EMBEDDINGS_URL, {"model": model, "input": text})
    return response.json()["data"][0]["embedding"]


def embed_activity_report_chunks(
    symbols=None,
    model=DEFAULT_EMBEDDING_MODEL,
    limit=None,
    request_sleep=0.05,
    max_consecutive_failures=MAX_CONSECUTIVE_FAILURES,
    workers=1,
):
    if workers < 1:
        raise ValueError("workers must be at least 1")
    with (
        writer_connection() as conn,
        pipeline_run(
            conn,
            "activity_report_embeddings",
            {"symbols": symbols, "model": model, "workers": workers},
        ),
    ):
        rows = fetch_pending_chunks(
            conn, symbols=symbols, limit=limit, model=model, embeddings=True
        )
        conn.commit()
        workers = max(1, min(workers, len(rows)))
        logger.info(
            "Found %s pending chunks for embeddings (%s worker(s))", len(rows), workers
        )
        OPENAI_GATE.interval = request_sleep

        def call(row):
            return call_openai_embedding(row["text"], model=model)

        def on_success(row, embedding):
            with conn.transaction():
                store_embedding(conn, row["chunk_id"], model, embedding)

        def on_failure(row, exc):
            conn.rollback()

        failures, attempted = run_concurrently(
            rows,
            call,
            on_success,
            on_failure,
            workers=workers,
            max_consecutive_failures=max_consecutive_failures,
        )
        raise_for_failures("embeddings", failures, attempted, len(rows))
    return len(rows)


def parse_args():
    parser = argparse.ArgumentParser(
        description="Build structured datasets from downloaded activity reports."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser(
        "migrate-db", help="Apply pending PostgreSQL report migrations."
    )

    clear = subparsers.add_parser("clear")
    clear.add_argument("--symbols", nargs="+")
    clear.add_argument(
        "--yes",
        action="store_true",
        required=True,
        help="Confirm deletion of report data.",
    )

    ingest = subparsers.add_parser("ingest")
    ingest.add_argument("--symbols", nargs="+")
    ingest.add_argument("--root-dir", type=Path, default=ACTIVITY_REPORTS_DIR)
    ingest.add_argument("--min-chars", type=int, default=500)
    ingest.add_argument(
        "--pages-per-chunk",
        type=int,
        default=0,
        help="Optional maximum distinct pages per chunk. Default 0 disables the page-count cap.",
    )
    ingest.add_argument("--target-chars", type=int, default=DEFAULT_CHUNK_TARGET_CHARS)
    ingest.add_argument("--max-chars", type=int, default=DEFAULT_CHUNK_MAX_CHARS)
    ingest.add_argument(
        "--overlap-chars", type=int, default=DEFAULT_CHUNK_OVERLAP_CHARS
    )
    ingest.add_argument(
        "--only-new",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Skip PDFs whose content is already ingested (default). Use "
        "--no-only-new to re-parse and refresh every matching PDF, for example "
        "after changing chunking options.",
    )
    ingest.add_argument(
        "--workers",
        type=int,
        default=None,
        help="Parallel PDF-parsing processes (default: CPU count - 1, at most 8). "
        "Use 1 to disable.",
    )
    ingest.add_argument(
        "--force",
        action="store_true",
        help="Rebuild chunks even if the PDF text/chunk signature is unchanged.",
    )

    extract = subparsers.add_parser("extract")
    extract.add_argument("--symbols", nargs="+")
    extract.add_argument("--model", default=DEFAULT_EXTRACTION_MODEL)
    extract.add_argument("--extraction-version", default=DEFAULT_EXTRACTION_VERSION)
    extract.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what is already extracted and what would be sent, without any "
        "API calls or database writes.",
    )
    extract.add_argument("--limit", type=int)
    extract.add_argument(
        "--years",
        type=int,
        nargs="+",
        help="Only reports for these fiscal years, e.g. --years 2025 2026.",
    )
    extract.add_argument(
        "--request-sleep",
        type=float,
        default=0.25,
        help="Minimum seconds between request starts, across all workers.",
    )
    extract.add_argument(
        "--workers",
        type=int,
        default=DEFAULT_API_WORKERS,
        help="Concurrent API requests (default: %(default)s). Lower it if you "
        "see many rate-limit warnings; 1 disables concurrency.",
    )
    extract.add_argument(
        "--force",
        action="store_true",
        help="Reprocess chunks even when this model/version already has completed results.",
    )

    embed = subparsers.add_parser("embed")
    embed.add_argument("--symbols", nargs="+")
    embed.add_argument("--model", default=DEFAULT_EMBEDDING_MODEL)
    embed.add_argument("--limit", type=int)
    embed.add_argument("--request-sleep", type=float, default=0.05)
    embed.add_argument("--workers", type=int, default=DEFAULT_API_WORKERS)

    analyze = subparsers.add_parser(
        "analyze-facts",
        help="Embed facts, then label novelty across reports and duplicates within "
        "a report. No re-extraction; embeddings are paid once per fact.",
    )
    analyze.add_argument("--symbols", nargs="+")
    analyze.add_argument("--model", default=DEFAULT_EXTRACTION_MODEL)
    analyze.add_argument("--extraction-version", default=DEFAULT_EXTRACTION_VERSION)
    analyze.add_argument("--embedding-model", default="text-embedding-3-small")
    analyze.add_argument("--dimensions", type=int, default=512)
    analyze.add_argument("--workers", type=int, default=DEFAULT_API_WORKERS)
    analyze.add_argument(
        "--window", type=int, default=4, help="Previous reports to compare with."
    )
    analyze.add_argument("--repeated-threshold", type=float, default=0.90)
    analyze.add_argument("--related-threshold", type=float, default=0.80)
    analyze.add_argument("--duplicate-threshold", type=float, default=0.95)

    check = subparsers.add_parser(
        "quality-check",
        help="Score a random sample of facts against their source text with an LLM judge.",
    )
    check.add_argument("--symbols", nargs="+")
    check.add_argument("--model", default=DEFAULT_EXTRACTION_MODEL)
    check.add_argument("--extraction-version", default=DEFAULT_EXTRACTION_VERSION)
    check.add_argument("--judge-model", default=DEFAULT_EXTRACTION_MODEL)
    check.add_argument("--sample", type=int, default=100)
    check.add_argument("--workers", type=int, default=DEFAULT_API_WORKERS)

    return parser.parse_args()


def main():
    configure_logging()
    args = parse_args()
    if args.command == "migrate-db":
        from bist.companies.metadata_store import connect_metadata_db

        with connect_metadata_db() as conn:
            applied = init_activity_report_tables(conn)
        logger.info(
            "PostgreSQL activity report schema %s",
            "applied" if applied else "already current",
        )
    elif args.command == "clear":
        clear_activity_report_data(
            symbols=args.symbols,
        )
    elif args.command == "ingest":
        ingest_activity_reports(
            symbols=args.symbols,
            root_dir=args.root_dir,
            min_chars=args.min_chars,
            pages_per_chunk=args.pages_per_chunk or None,
            target_chars=args.target_chars,
            max_chars=args.max_chars,
            overlap_chars=args.overlap_chars,
            force=args.force,
            workers=args.workers or default_ingest_workers(),
            only_new=args.only_new,
        )
    elif args.command == "extract" and args.dry_run:
        print_extraction_dry_run(
            *summarize_extraction(
                args.symbols,
                args.model,
                args.extraction_version,
                args.force,
                args.years,
            )
        )
    elif args.command == "extract":
        extract_activity_report_facts(
            symbols=args.symbols,
            model=args.model,
            extraction_version=args.extraction_version,
            limit=args.limit,
            request_sleep=args.request_sleep,
            force=args.force,
            workers=args.workers,
            years=args.years,
        )
    elif args.command == "embed":
        embed_activity_report_chunks(
            symbols=args.symbols,
            model=args.model,
            limit=args.limit,
            request_sleep=args.request_sleep,
            workers=args.workers,
        )

    elif args.command == "analyze-facts":
        from bist.activity_reports.fact_analysis import analyze_facts

        analyze_facts(
            symbols=args.symbols,
            model=args.model,
            extraction_version=args.extraction_version,
            embedding_model=args.embedding_model,
            dimensions=args.dimensions,
            workers=args.workers,
            window=args.window,
            repeated=args.repeated_threshold,
            related=args.related_threshold,
            duplicate=args.duplicate_threshold,
        )
    elif args.command == "quality-check":
        from bist.activity_reports.fact_analysis import (
            print_quality_summary,
            quality_check,
        )

        print_quality_summary(
            *quality_check(
                symbols=args.symbols,
                model=args.model,
                extraction_version=args.extraction_version,
                judge_model=args.judge_model,
                sample_size=args.sample,
                workers=args.workers,
            )
        )


if __name__ == "__main__":
    main()
