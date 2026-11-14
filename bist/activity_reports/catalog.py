"""Persistent KAP catalog, completed search ranges, and local download state."""

import re
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from psycopg.types.json import Jsonb

from bist.activity_reports.store import (
    file_sha256,
    init_activity_report_tables,
    resolve_company_ids,
)
from bist.companies.metadata_store import connect_metadata_db

KAP_TIME_ZONE = ZoneInfo("Europe/Istanbul")
QUERY_VERSION = "kap_activity_reports_v1"


@contextmanager
def catalog_connection():
    """Allow one catalog/downloader writer, independently of the extraction writer."""
    with connect_metadata_db() as conn:
        init_activity_report_tables(conn)
        locked = conn.execute(
            "SELECT pg_try_advisory_lock(hashtextextended('activity_report_fetcher',0)) AS locked"
        ).fetchone()["locked"]
        conn.commit()
        if not locked:
            raise RuntimeError("Another activity-report indexer/downloader is running.")
        yield conn


def company_lookup(conn):
    lookup = {
        r["symbol"]: r["company_id"]
        for r in conn.execute("SELECT symbol,company_id FROM metadata.companies")
    }
    for row in conn.execute(
        "SELECT alias_symbol,company_id FROM metadata.company_symbol_aliases"
    ):
        lookup.setdefault(row["alias_symbol"], row["company_id"])
    return lookup


def selected_companies(conn, symbols=None):
    ids = resolve_company_ids(conn, symbols)
    query = "SELECT company_id,symbol,mkk_member_oid FROM metadata.companies"
    params = []
    if ids is not None:
        query += " WHERE company_id=ANY(%s)"
        params.append(ids)
    return conn.execute(query + " ORDER BY company_id", params).fetchall()


def parse_publication_time(value):
    if not value:
        return None
    try:
        parsed = datetime.strptime(value, "%d.%m.%Y %H:%M:%S")
    except ValueError:
        parsed = datetime.fromisoformat(value)
    return parsed.replace(tzinfo=KAP_TIME_ZONE) if parsed.tzinfo is None else parsed


def upsert_disclosure(conn, item, lookup):
    if not isinstance(item, dict):
        raise TypeError("A KAP disclosure must be an object")
    disclosure_id = int(item["disclosureIndex"])
    codes = str(item.get("stockCodes") or "").strip()
    ids = {lookup[c] for c in re.split(r"[,;\s]+", codes.upper()) if c in lookup}
    company_id = next(iter(ids)) if len(ids) == 1 else None
    year = int(item["year"]) if item.get("year") not in (None, "") else None
    quarter = int(item["period"]) if item.get("period") not in (None, "") else None
    row = conn.execute(
        """INSERT INTO metadata.activity_report_disclosures
           (disclosure_index,company_id,source_stock_codes,fiscal_year,fiscal_quarter,
            published_at,subject,summary,download_url,source_payload)
           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
           ON CONFLICT (disclosure_index) DO UPDATE SET
             company_id=COALESCE(EXCLUDED.company_id,activity_report_disclosures.company_id),
             source_stock_codes=EXCLUDED.source_stock_codes,fiscal_year=EXCLUDED.fiscal_year,
             fiscal_quarter=EXCLUDED.fiscal_quarter,published_at=EXCLUDED.published_at,
             subject=EXCLUDED.subject,summary=EXCLUDED.summary,
             download_url=COALESCE(EXCLUDED.download_url,activity_report_disclosures.download_url),
             source_payload=EXCLUDED.source_payload,last_seen_at=now()
           RETURNING disclosure_index,company_id""",
        (
            disclosure_id,
            company_id,
            codes,
            year,
            quarter,
            parse_publication_time(item.get("publishDate")),
            item.get("subject"),
            item.get("summary"),
            item.get("downloadLink") or None,
            Jsonb(item),
        ),
    ).fetchone()
    conn.execute(
        "INSERT INTO ops.activity_report_downloads (disclosure_index) VALUES (%s) ON CONFLICT DO NOTHING",
        (disclosure_id,),
    )
    return row


def subtract_coverage(start, end, intervals):
    """Return uncovered inclusive ranges; independent of previous request batching."""
    cursor = start
    gaps = []
    for left, right in sorted(intervals):
        if right < cursor or left > end:
            continue
        if left > cursor:
            gaps.append((cursor, left - timedelta(days=1)))
        cursor = max(cursor, right + timedelta(days=1))
        if cursor > end:
            break
    if cursor <= end:
        gaps.append((cursor, end))
    return gaps


def search_plan(conn, companies, start, end, *, refresh=False, today=None):
    today = today or datetime.now(KAP_TIME_ZONE).date()
    if start > end or start > today:
        raise ValueError("Search start must not exceed the end date or today")
    end = min(end, today)
    coverage = {}
    if not refresh:
        for row in conn.execute(
            """SELECT company_id,begin_date,end_date FROM ops.activity_report_index_searches
               WHERE query_version=%s AND company_id=ANY(%s)
                 AND end_date >= %s AND begin_date <= %s""",
            (QUERY_VERSION, [c["company_id"] for c in companies], start, end),
        ):
            coverage.setdefault(row["company_id"], []).append(
                (row["begin_date"], min(row["end_date"], today - timedelta(days=1)))
            )
    grouped = {}
    for company in companies:
        gaps = subtract_coverage(start, end, coverage.get(company["company_id"], []))
        for left, right in gaps:
            while left <= right:
                stop = min(date(left.year, 12, 31), right)
                grouped.setdefault((left.isoformat(), stop.isoformat()), []).append(
                    company
                )
                left = stop + timedelta(days=1)
    return sorted(grouped.items())


def checkpoint_search(conn, companies, interval, run_id, *, today=None):
    today = today or datetime.now(KAP_TIME_ZONE).date()
    start = date.fromisoformat(interval[0])
    end = min(date.fromisoformat(interval[1]), today - timedelta(days=1))
    if start > end:
        return
    with conn.cursor() as cursor:
        cursor.executemany(
            """INSERT INTO ops.activity_report_index_searches
               (company_id,query_version,begin_date,end_date,pipeline_run_id)
               VALUES (%s,%s,%s,%s,%s)
               ON CONFLICT (company_id,query_version,begin_date,end_date)
               DO UPDATE SET completed_at=now(),pipeline_run_id=EXCLUDED.pipeline_run_id""",
            [(c["company_id"], QUERY_VERSION, start, end, run_id) for c in companies],
        )


def catalog_rows(
    conn,
    symbols=None,
    begin=None,
    end=None,
    *,
    missing_links=False,
    publication_range=None,
):
    ids = resolve_company_ids(conn, symbols)
    where = [
        "d.company_id IS NOT NULL",
        "d.fiscal_year IS NOT NULL",
        "d.fiscal_quarter IS NOT NULL",
    ]
    params = []
    if ids is not None:
        where.append("d.company_id=ANY(%s)")
        params.append(ids)
    if begin is not None:
        where.extend(
            [
                "(d.fiscal_year,d.fiscal_quarter)>=(%s,%s)",
                "(d.fiscal_year,d.fiscal_quarter)<=(%s,%s)",
            ]
        )
        params.extend([*begin, *end])
    if missing_links:
        where.append("d.download_url IS NULL")
        # A downloaded PDF does not need a disclosure-page lookup just to cache
        # an attachment URL. The download command can resolve it later if the
        # local file is missing and the report needs to be fetched again.
        where.append("dl.status IS DISTINCT FROM 'downloaded'")
    if publication_range:
        where.append(
            "(d.published_at IS NULL OR (d.published_at AT TIME ZONE 'Europe/Istanbul')::date BETWEEN %s AND %s)"
        )
        params.extend(publication_range)
    return conn.execute(
        """SELECT d.*,c.symbol,dl.file_path AS downloaded_file_path,dl.status AS download_status
           FROM metadata.activity_report_disclosures d
           JOIN metadata.companies c USING (company_id)
           LEFT JOIN ops.activity_report_downloads dl USING (disclosure_index)
           WHERE """
        + " AND ".join(where)
        + " ORDER BY d.company_id,d.fiscal_year,d.fiscal_quarter,d.disclosure_index",
        params,
    ).fetchall()


def set_download_url(conn, disclosure_id, url):
    conn.execute(
        "UPDATE metadata.activity_report_disclosures SET download_url=%s WHERE disclosure_index=%s",
        (url, disclosure_id),
    )


def start_download(conn, disclosure_id, run_id):
    conn.execute(
        """INSERT INTO ops.activity_report_downloads
           (disclosure_index,status,attempt_count,last_attempt_at,pipeline_run_id)
           VALUES (%s,'downloading',1,now(),%s)
           ON CONFLICT (disclosure_index) DO UPDATE SET status='downloading',
             attempt_count=activity_report_downloads.attempt_count+1,last_attempt_at=now(),
             pipeline_run_id=EXCLUDED.pipeline_run_id,error_text=NULL""",
        (disclosure_id, run_id),
    )


def complete_download(conn, disclosure_id, path, run_id=None):
    path = Path(path).resolve()
    conn.execute(
        """INSERT INTO ops.activity_report_downloads
           (disclosure_index,status,file_path,source_sha256,file_size_bytes,downloaded_at,pipeline_run_id)
           VALUES (%s,'downloaded',%s,%s,%s,now(),%s)
           ON CONFLICT (disclosure_index) DO UPDATE SET status='downloaded',
             file_path=EXCLUDED.file_path,source_sha256=EXCLUDED.source_sha256,
             file_size_bytes=EXCLUDED.file_size_bytes,downloaded_at=COALESCE(activity_report_downloads.downloaded_at,now()),
             pipeline_run_id=COALESCE(EXCLUDED.pipeline_run_id,activity_report_downloads.pipeline_run_id),error_text=NULL""",
        (disclosure_id, str(path), file_sha256(path), path.stat().st_size, run_id),
    )


def fail_download(conn, disclosure_id, error):
    conn.execute(
        "UPDATE ops.activity_report_downloads SET status='failed',error_text=%s WHERE disclosure_index=%s",
        (str(error), disclosure_id),
    )


def valid_pdf(path):
    path = Path(path)
    if not path.is_file() or path.stat().st_size == 0:
        return False
    with path.open("rb") as stream:
        header = stream.read(1024)
        stream.seek(max(0, path.stat().st_size - 8192))
        tail = stream.read()
    return b"%PDF-" in header and b"%%EOF" in tail
