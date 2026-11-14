"""PostgreSQL persistence for reports; no API requests or PDF parsing live here."""

import hashlib
import re
from contextlib import contextmanager

import unidecode
from psycopg.types.json import Jsonb

from bist.companies.metadata_store import connect_metadata_db
from bist.paths import MIGRATIONS_DIR


def init_activity_report_tables(conn):
    """Apply this project's versioned migrations atomically, once each."""
    with conn.transaction():
        conn.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
            ("activity_report_migrations",),
        )
        required = conn.execute(
            "SELECT to_regclass('metadata.companies') AS companies, "
            "to_regclass('ops.schema_migrations') AS migrations"
        ).fetchone()
        if not required["companies"] or not required["migrations"]:
            raise RuntimeError(
                "The shared bist_finance metadata and ops schemas must exist first."
            )
        applied = False
        for migration in sorted(
            MIGRATIONS_DIR.glob("activity_reports_*.sql")
        ):
            if conn.execute(
                "SELECT 1 FROM ops.schema_migrations WHERE version=%s",
                (migration.stem,),
            ).fetchone():
                continue
            conn.execute(migration.read_text(encoding="utf-8"))
            conn.execute(
                "INSERT INTO ops.schema_migrations (version) VALUES (%s)",
                (migration.stem,),
            )
            applied = True
    return applied


@contextmanager
def writer_connection():
    """Serialize report writers, including API work, to avoid duplicate paid calls."""
    with connect_metadata_db() as conn:
        init_activity_report_tables(conn)
        locked = conn.execute(
            "SELECT pg_try_advisory_lock(hashtextextended('activity_reports_writer', 0)) AS locked"
        ).fetchone()["locked"]
        conn.commit()
        if not locked:
            raise RuntimeError(
                "Another activity-report writer is running; retry after it finishes."
            )
        yield conn
        # Closing the connection releases the session advisory lock on every exit path.


@contextmanager
def pipeline_run(conn, name, parameters):
    run_id = conn.execute(
        "INSERT INTO ops.pipeline_runs (pipeline_name,status,parameters) "
        "VALUES (%s,'running',%s) RETURNING run_id",
        (name, Jsonb(parameters)),
    ).fetchone()["run_id"]
    conn.commit()
    try:
        yield run_id
        conn.execute(
            "UPDATE ops.pipeline_runs SET status='succeeded',finished_at=now() WHERE run_id=%s",
            (run_id,),
        )
        conn.commit()
    except BaseException as exc:
        conn.rollback()
        conn.execute(
            "UPDATE ops.pipeline_runs SET status='failed',finished_at=now(),error_text=%s WHERE run_id=%s",
            (str(exc) or type(exc).__name__, run_id),
        )
        conn.commit()
        raise


def _loose(text):
    """Letters and digits only, transliterated: ignores spacing, punctuation, case,
    curly quotes and line-break hyphens that differ between the PDF text and a quote."""
    return re.sub(r"[^0-9a-z]+", "", unidecode.unidecode(text or "").lower())


def quote_in_text(quote, text):
    """Whether every verbatim excerpt of the quote appears in the text.

    Quotes may join separate excerpts with "..."; each excerpt is checked on its own.
    Returns None when the text is unknown.
    """
    if text is None:
        return None
    excerpts = [_loose(part) for part in re.split(r"\.\.\.|…", quote or "")]
    excerpts = [part for part in excerpts if len(part) >= 12] or [
        part for part in excerpts if part
    ]
    if not excerpts:
        return False
    haystack = _loose(text)
    return all(part in haystack for part in excerpts)


def file_sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def company_identity(conn, symbol):
    rows = conn.execute(
        """SELECT DISTINCT c.company_id, c.symbol FROM metadata.companies c
           LEFT JOIN metadata.company_symbol_aliases a USING (company_id)
           WHERE c.symbol=%s OR a.alias_symbol=%s""",
        (symbol.strip().upper(),) * 2,
    ).fetchall()
    if len(rows) != 1:
        raise ValueError(
            f"Expected one PostgreSQL company for {symbol!r}; found {len(rows)}."
        )
    return rows[0]


def resolve_company_ids(conn, symbols):
    return (
        list(
            dict.fromkeys(
                company_identity(conn, symbol)["company_id"] for symbol in symbols
            )
        )
        if symbols
        else None
    )


def upsert_document(conn, path, symbol, year, period, disclosure_index, *, run_id=None):
    identity = company_identity(conn, symbol)
    return conn.execute(
        """INSERT INTO bronze.activity_report_documents
           (company_id,year,period,disclosure_index,file_path,source_sha256,run_id)
           VALUES (%s,%s,%s,%s,%s,%s,%s)
           ON CONFLICT (company_id,year,period,source_sha256) DO UPDATE
           SET file_path=EXCLUDED.file_path,
               disclosure_index=GREATEST(EXCLUDED.disclosure_index,bronze.activity_report_documents.disclosure_index)
           RETURNING id""",
        (
            identity["company_id"],
            year,
            period,
            disclosure_index,
            str(path),
            file_sha256(path),
            run_id,
        ),
    ).fetchone()["id"]


def ingested_document_keys(conn):
    """Set of (symbol, year, period, sha256) for every stored document.

    Each PDF is stored in one transaction (document, pages and chunks together), so a
    document row means that PDF was fully ingested.
    """
    return {
        (r["symbol"], r["year"], r["period"], r["source_sha256"])
        for r in conn.execute(
            """SELECT m.symbol, d.year, d.period, d.source_sha256
               FROM bronze.activity_report_documents d
               JOIN metadata.companies m USING (company_id)"""
        )
    }


def replace_pages(conn, document_id, pages):
    conn.execute(
        "DELETE FROM bronze.activity_report_pages WHERE document_id=%s", (document_id,)
    )
    with conn.cursor() as cursor:
        cursor.executemany(
            "INSERT INTO bronze.activity_report_pages (document_id,page,text,char_count) VALUES (%s,%s,%s,%s)",
            [(document_id, p["page"], p["text"], p["char_count"]) for p in pages],
        )


def upsert_chunk(conn, document_id, chunk):
    return conn.execute(
        """INSERT INTO silver.activity_report_chunks
           (document_id,start_page,end_page,included_pages,text,text_hash)
           VALUES (%s,%s,%s,%s,%s,%s)
           ON CONFLICT (document_id,text_hash,start_page,end_page) DO UPDATE
           SET is_active=true, included_pages=EXCLUDED.included_pages RETURNING id""",
        (
            document_id,
            chunk["start_page"],
            chunk["end_page"],
            chunk["included_pages"],
            chunk["text"],
            hashlib.sha256(chunk["text"].encode("utf-8")).hexdigest(),
        ),
    ).fetchone()["id"]


def existing_chunk_signature(conn, document_id):
    return [
        (r["start_page"], r["end_page"], tuple(r["included_pages"]), r["text_hash"])
        for r in conn.execute(
            "SELECT * FROM silver.activity_report_chunks WHERE document_id=%s AND is_active "
            "ORDER BY start_page,end_page,id",
            (document_id,),
        )
    ]


def replace_chunks(conn, document_id, chunks):
    # Retire superseded chunks; paid results and embeddings remain attached to them.
    conn.execute(
        "UPDATE silver.activity_report_chunks SET is_active=false WHERE document_id=%s",
        (document_id,),
    )
    for chunk in chunks:
        upsert_chunk(conn, document_id, chunk)


def fetch_pending_chunks(
    conn,
    symbols=None,
    limit=None,
    model=None,
    extraction_version=None,
    force=False,
    prompt_hash=None,
    *,
    embeddings=False,
    years=None,
):
    company_ids = resolve_company_ids(conn, symbols)
    where = ["c.is_active"]
    params = []
    if company_ids is not None:
        where.append("d.company_id=ANY(%s)")
        params.append(company_ids)
    if years:
        where.append("d.year=ANY(%s)")
        params.append(list(years))
    if not force:
        if embeddings:
            where.append(
                "NOT EXISTS (SELECT 1 FROM silver.activity_report_embeddings e WHERE e.chunk_id=c.id AND e.model=%s)"
            )
            params.append(model)
        else:
            where.append("""NOT EXISTS (SELECT 1 FROM ops.activity_report_extraction_runs r
                         WHERE r.chunk_id=c.id AND r.model=%s AND r.extraction_version=%s
                         AND r.prompt_hash=%s AND r.status='completed' AND r.is_active)""")
            params.extend([model, extraction_version, prompt_hash])
    if limit is not None and limit < 1:
        raise ValueError("limit must be positive")
    query = (
        """SELECT c.id AS chunk_id,c.document_id,c.start_page,c.end_page,c.included_pages,c.text,
                      m.symbol,d.year,d.period
               FROM silver.activity_report_chunks c
               JOIN silver.current_activity_report_documents d ON d.id=c.document_id
               JOIN metadata.companies m USING (company_id)
               WHERE """
        + " AND ".join(where)
        + " ORDER BY m.symbol,d.year,d.period,c.start_page,c.id"
    )
    if limit is not None:
        query += " LIMIT %s"
        params.append(limit)
    return conn.execute(query, params).fetchall()


def extraction_summary(
    conn, symbols, model, extraction_version, prompt_hash, force=False, years=None
):
    """Per-company counts of chunks already extracted vs still to extract.

    Uses the same "already done" test as ``fetch_pending_chunks`` but never loads chunk
    text, so it is cheap and makes no API calls. With ``force`` nothing counts as done.
    """
    company_ids = resolve_company_ids(conn, symbols)
    params = [model, extraction_version, prompt_hash]
    company_filter = ""
    if company_ids is not None:
        company_filter = "AND d.company_id=ANY(%s)"
        params.append(company_ids)
    if years:
        company_filter += " AND d.year=ANY(%s)"
        params.append(list(years))
    done = (
        "false"
        if force
        else """EXISTS (SELECT 1 FROM ops.activity_report_extraction_runs r
                   WHERE r.chunk_id=c.id AND r.model=%s AND r.extraction_version=%s
                   AND r.prompt_hash=%s AND r.status='completed' AND r.is_active)"""
    )
    if force:
        params = params[3:]
    return conn.execute(
        f"""SELECT symbol,
                   count(*) AS active_chunks,
                   count(*) FILTER (WHERE done) AS extracted_chunks,
                   count(*) FILTER (WHERE NOT done) AS pending_chunks,
                   count(DISTINCT document_id) FILTER (WHERE NOT done) AS pending_reports,
                   coalesce(sum(chars) FILTER (WHERE NOT done), 0) AS pending_chars
            FROM (SELECT m.symbol, c.document_id, length(c.text) AS chars,
                         {done} AS done
                  FROM silver.activity_report_chunks c
                  JOIN silver.current_activity_report_documents d ON d.id=c.document_id
                  JOIN metadata.companies m USING (company_id)
                  WHERE c.is_active {company_filter}) t
            GROUP BY symbol ORDER BY symbol""",
        params,
    ).fetchall()


def insert_extractions(
    conn,
    row,
    result,
    model,
    extraction_version,
    prompt_hash,
    *,
    run_id=None,
):
    facts = result["Extractions"]
    usage = result.get("_usage") or {}
    if not isinstance(facts, list):
        raise TypeError("Extractions must be an array")
    # The caller commits facts and completion together, including successful zero-fact chunks.
    conn.execute(
        "UPDATE ops.activity_report_extraction_runs SET is_active=false "
        "WHERE chunk_id=%s AND model=%s AND extraction_version=%s AND is_active",
        (row["chunk_id"], model, extraction_version),
    )
    extraction_run_id = conn.execute(
        """INSERT INTO ops.activity_report_extraction_runs
           (chunk_id,pipeline_run_id,model,extraction_version,prompt_hash,extraction_count,status,is_active,
            input_tokens,output_tokens)
           VALUES (%s,%s,%s,%s,%s,%s,'completed',true,%s,%s)
           ON CONFLICT (chunk_id,model,extraction_version,prompt_hash) DO UPDATE
           SET pipeline_run_id=EXCLUDED.pipeline_run_id,extraction_count=EXCLUDED.extraction_count,
               status='completed',is_active=true,error=NULL,created_at=now(),
               input_tokens=EXCLUDED.input_tokens,output_tokens=EXCLUDED.output_tokens RETURNING id""",
        (
            row["chunk_id"],
            run_id,
            model,
            extraction_version,
            prompt_hash,
            len(facts),
            usage.get("prompt_tokens"),
            usage.get("completion_tokens"),
        ),
    ).fetchone()["id"]
    conn.execute(
        "DELETE FROM silver.activity_report_extractions WHERE extraction_run_id=%s",
        (extraction_run_id,),
    )
    for number, item in enumerate(facts):
        # Facts from the current schema have themes/entities/metrics; older ones
        # (and tests) have free-form tags and a confidence score.
        themes = item.get("themes")
        entities = item.get("entities")
        fact_id = conn.execute(
            """INSERT INTO silver.activity_report_extractions
               (extraction_run_id,fact_number,page,page_in_chunk,category,event_type,tags,sentiment,
                magnitude,time_relevance,confidence,source_quote,info,quote_in_chunk,
                themes,entities)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id""",
            (
                extraction_run_id,
                number,
                item["page"],
                item["page"] in row["included_pages"],
                item["category"],
                item["event_type"],
                Jsonb(item.get("tags") or []),
                item["sentiment"],
                item["magnitude"],
                item["time_relevance"],
                item.get("confidence"),
                item["source_quote"],
                item["info"],
                quote_in_text(item["source_quote"], row.get("text")),
                Jsonb(themes) if themes is not None else None,
                Jsonb(entities) if entities is not None else None,
            ),
        ).fetchone()["id"]
        metrics = item.get("metrics") or []
        if metrics:
            with conn.cursor() as cursor:
                cursor.executemany(
                    """INSERT INTO silver.activity_report_fact_metrics
                       (extraction_id,metric_number,name,value,unit,period)
                       VALUES (%s,%s,%s,%s,%s,%s)""",
                    [
                        (
                            fact_id,
                            index,
                            metric["name"],
                            metric["value"],
                            metric.get("unit"),
                            metric.get("period"),
                        )
                        for index, metric in enumerate(metrics)
                    ],
                )
    if run_id:
        checks = {
            "activity_report_page_in_chunk": sum(
                item["page"] not in row["included_pages"] for item in facts
            ),
            "activity_report_quote_in_chunk": sum(
                quote_in_text(item["source_quote"], row.get("text")) is False
                for item in facts
            ),
        }
        for check_name, affected in checks.items():
            if affected:
                conn.execute(
                    """INSERT INTO ops.data_quality_results (run_id,check_name,severity,affected_rows,details)
                       VALUES (%s,%s,'warning',%s,%s)""",
                    (
                        run_id,
                        check_name,
                        affected,
                        Jsonb(
                            {
                                "chunk_id": row["chunk_id"],
                                "extraction_run_id": extraction_run_id,
                            }
                        ),
                    ),
                )
    return extraction_run_id


def record_extraction_failure(
    conn, chunk_id, model, version, prompt_hash, error, run_id
):
    conn.execute(
        """INSERT INTO ops.activity_report_extraction_runs
           (chunk_id,pipeline_run_id,model,extraction_version,prompt_hash,status,error)
           VALUES (%s,%s,%s,%s,%s,'failed',%s)
           ON CONFLICT (chunk_id,model,extraction_version,prompt_hash) DO UPDATE
           SET status='failed',error=EXCLUDED.error,pipeline_run_id=EXCLUDED.pipeline_run_id
           WHERE activity_report_extraction_runs.status <> 'completed'""",
        (chunk_id, run_id, model, version, prompt_hash, str(error)),
    )


def store_embedding(conn, chunk_id, model, embedding):
    conn.execute(
        """INSERT INTO silver.activity_report_embeddings (chunk_id,model,embedding)
           VALUES (%s,%s,%s) ON CONFLICT (chunk_id,model) DO UPDATE
           SET embedding=EXCLUDED.embedding,created_at=now()""",
        (chunk_id, model, embedding),
    )
