import argparse
import logging
import re
import time
from datetime import date, datetime
from pathlib import Path
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from bist.activity_reports.catalog import (
    KAP_TIME_ZONE,
    QUERY_VERSION,
    catalog_connection,
    catalog_rows,
    checkpoint_search,
    company_lookup,
    complete_download,
    fail_download,
    search_plan,
    selected_companies,
    set_download_url,
    start_download,
    upsert_disclosure,
    valid_pdf,
)
from bist.activity_reports.store import pipeline_run
from bist.http_utils import send_with_retry
from bist.logging_config import configure_logging
from bist.paths import ACTIVITY_REPORTS_DIR

logger = logging.getLogger(__name__)

KAP_BASE_URL = "https://www.kap.org.tr"
KAP_DISCLOSURE_SEARCH_URL = f"{KAP_BASE_URL}/tr/api/disclosure/members/byCriteria"
ACTIVITY_REPORT_SUBJECT_OID = "4028328d594c04f201594c5155dd0076"
DEFAULT_RATE_LIMIT_BACKOFF = 300


def chunked(items, chunk_size):
    return [
        items[index : index + chunk_size] for index in range(0, len(items), chunk_size)
    ]


def request_with_backoff(
    session,
    method,
    url,
    retries=5,
    retry_backoff=5,
    rate_limit_backoff=DEFAULT_RATE_LIMIT_BACKOFF,
    request_sleep=3,
    request_context=None,
    **kwargs,
):
    response = send_with_retry(
        lambda: session.request(method, url, timeout=30, **kwargs),
        retries=retries,
        retry_backoff=retry_backoff,
        rate_limit_backoff=rate_limit_backoff,
        label=f"{url} {request_context}" if request_context else url,
    )
    if request_sleep:
        time.sleep(request_sleep)
    return response


def post_activity_report_search(
    session,
    interval,
    oids,
    retries=5,
    retry_backoff=5,
    rate_limit_backoff=DEFAULT_RATE_LIMIT_BACKOFF,
    request_sleep=3,
    request_context=None,
):
    headers = {
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "tr",
        "Content-Type": "application/json",
        "Origin": KAP_BASE_URL,
        "Referer": f"{KAP_BASE_URL}/tr/bildirim-sorgu",
        "User-Agent": "Mozilla/5.0",
    }
    payload = {
        "fromDate": interval[0],
        "toDate": interval[1],
        "memberType": "IGS",
        "mkkMemberOidList": oids,
        "inactiveMkkMemberOidList": [],
        "disclosureClass": "",
        "subjectList": [ACTIVITY_REPORT_SUBJECT_OID],
        "isLate": "",
        "mainSector": "",
        "sector": "",
        "subSector": "",
        "marketOid": "",
        "index": "",
        "bdkReview": "",
        "bdkMemberOidList": [],
        "year": "",
        "term": "",
        "ruleType": "",
        "period": "",
        "fromSrc": False,
        "srcCategory": "",
        "disclosureIndexList": [],
    }

    response = request_with_backoff(
        session,
        "POST",
        KAP_DISCLOSURE_SEARCH_URL,
        retries=retries,
        retry_backoff=retry_backoff,
        rate_limit_backoff=rate_limit_backoff,
        request_sleep=request_sleep,
        headers=headers,
        json=payload,
    )
    return response.json()


def extract_pdf_link(html):
    soup = BeautifulSoup(html, "html.parser")
    for div in soup.find_all("div"):
        if div.get_text(" ", strip=True).startswith("Bildirim Ekleri"):
            anchor = div.find_next("a", href=True)
            if anchor:
                return urljoin(KAP_BASE_URL, anchor["href"])
    return None


def get_activity_report_pdf_link(
    session,
    disclosure_index,
    retries=5,
    retry_backoff=5,
    rate_limit_backoff=DEFAULT_RATE_LIMIT_BACKOFF,
    request_sleep=3,
    request_context=None,
):
    page_url = f"{KAP_BASE_URL}/tr/Bildirim/{disclosure_index}"
    response = request_with_backoff(
        session,
        "GET",
        page_url,
        retries=retries,
        retry_backoff=retry_backoff,
        rate_limit_backoff=rate_limit_backoff,
        request_sleep=request_sleep,
        request_context=request_context,
        headers={"User-Agent": "Mozilla/5.0", "Accept-Language": "tr"},
    )
    return extract_pdf_link(response.text)


def index_activity_reports(
    symbols=None,
    begin_date="2017-01-01",
    end_date=None,
    chunk_size=100,
    sleep_seconds=0,
    request_sleep=3,
    retries=5,
    retry_backoff=5,
    rate_limit_backoff=DEFAULT_RATE_LIMIT_BACKOFF,
    refresh=False,
    today=None,
):
    today = today or datetime.now(KAP_TIME_ZONE).date()
    end_date = end_date or today.isoformat()
    start, end = date.fromisoformat(begin_date), date.fromisoformat(end_date)
    if chunk_size < 1:
        raise ValueError("chunk_size must be positive")
    with (
        catalog_connection() as conn,
        requests.Session() as session,
        pipeline_run(
            conn,
            "activity_report_indexing",
            {
                "symbols": symbols,
                "begin_date": begin_date,
                "end_date": end_date,
                "refresh": refresh,
                "query_version": QUERY_VERSION,
            },
        ) as run_id,
    ):
        companies = selected_companies(conn, symbols)
        lookup = company_lookup(conn)
        plan = search_plan(conn, companies, start, end, refresh=refresh, today=today)
        conn.commit()
        logger.info(
            "Planned %s date ranges; completed company/range searches are skipped",
            len(plan),
        )
        requests_made = 0
        failures = []
        for interval, missing_companies in plan:
            for group in chunked(missing_companies, chunk_size):
                logger.info("Searching %s to %s (%s companies)", *interval, len(group))
                requests_made += 1
                try:
                    items = post_activity_report_search(
                        session,
                        interval,
                        [c["mkk_member_oid"] for c in group],
                        retries=retries,
                        retry_backoff=retry_backoff,
                        rate_limit_backoff=rate_limit_backoff,
                        request_sleep=request_sleep,
                    )
                    if not isinstance(items, list):
                        raise TypeError(
                            "Expected a complete KAP disclosure array; refusing to checkpoint"
                        )
                    with conn.transaction():
                        for item in items:
                            row = upsert_disclosure(conn, item, lookup)
                            if row["company_id"] is None:
                                logger.warning(
                                    "Preserved unmapped issuer for disclosure %s",
                                    row["disclosure_index"],
                                )
                        checkpoint_search(conn, group, interval, run_id, today=today)
                except Exception as exc:
                    conn.rollback()
                    failures.append(str(exc))
                    logger.exception(
                        "Search failed for %s to %s; it remains pending", *interval
                    )
                if sleep_seconds:
                    time.sleep(sleep_seconds)
        missing_links = catalog_rows(
            conn,
            symbols=symbols,
            missing_links=True,
            publication_range=(start, min(end, today)),
        )
        conn.commit()
        total_missing_links = len(missing_links)
        logger.info(
            "Starting PDF-link resolution for %s reports without cached attachment links",
            total_missing_links,
        )
        links_resolved = 0
        link_failures = 0
        for position, row in enumerate(missing_links, start=1):
            context = (
                f"{row['symbol']} {row['fiscal_year']} Q{row['fiscal_quarter']} "
                f"disclosure {row['disclosure_index']}"
            )
            if position == 1 or position % 10 == 0 or position == total_missing_links:
                logger.info(
                    "Resolving PDF link %s/%s: %s",
                    position,
                    total_missing_links,
                    context,
                )
            try:
                link = get_activity_report_pdf_link(
                    session,
                    row["disclosure_index"],
                    retries=retries,
                    retry_backoff=retry_backoff,
                    rate_limit_backoff=rate_limit_backoff,
                    request_sleep=request_sleep,
                    request_context=context,
                )
                if not link:
                    raise ValueError("No PDF attachment link found")
                with conn.transaction():
                    set_download_url(conn, row["disclosure_index"], link)
                links_resolved += 1
            except Exception as exc:
                conn.rollback()
                with conn.transaction():
                    fail_download(conn, row["disclosure_index"], exc)
                link_failures += 1
                logger.exception(
                    "PDF link lookup failed for %s; it remains pending", context
                )
        logger.info(
            "PDF-link resolution finished: %s/%s links resolved; %s need retry",
            links_resolved,
            total_missing_links,
            link_failures,
        )
        if failures:
            raise RuntimeError(
                f"{len(failures)} KAP searches failed and remain pending; rerun to retry"
            )
    return requests_made


def safe_filename_part(value):
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", str(value)).strip("_")


def activity_report_path(item, output_dir=ACTIVITY_REPORTS_DIR):
    symbol = safe_filename_part(item.get("stockCodes") or "UNKNOWN")
    year = item.get("year")
    period = item.get("period")
    disclosure_index = item.get("disclosureIndex")
    folder = Path(output_dir) / symbol
    filename = f"{symbol}_{year}_{period}_{disclosure_index}.pdf"
    return folder / filename


def download_pdf(
    session,
    url,
    output_path,
    retries=5,
    retry_backoff=5,
    rate_limit_backoff=DEFAULT_RATE_LIMIT_BACKOFF,
    request_sleep=3,
):
    response = request_with_backoff(
        session,
        "GET",
        url,
        retries=retries,
        retry_backoff=retry_backoff,
        rate_limit_backoff=rate_limit_backoff,
        request_sleep=request_sleep,
        headers={"User-Agent": "Mozilla/5.0", "Accept-Language": "tr"},
    )
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = output_path.with_suffix(output_path.suffix + ".part")
    try:
        temporary.write_bytes(response.content)
        if not valid_pdf(temporary):
            raise ValueError("Downloaded body lacks a PDF header or EOF marker")
        temporary.replace(output_path)
    finally:
        temporary.unlink(missing_ok=True)


def download_activity_reports(
    symbols=None,
    begin_year=None,
    begin_period=None,
    end_year=None,
    end_period=None,
    output_dir=ACTIVITY_REPORTS_DIR,
    sleep_seconds=0,
    request_sleep=3,
    retries=5,
    retry_backoff=5,
    rate_limit_backoff=DEFAULT_RATE_LIMIT_BACKOFF,
):
    bounds = (begin_year, begin_period, end_year, end_period)
    if any(v is not None for v in bounds):
        if any(v is None for v in bounds):
            raise ValueError("Supply all four report year/quarter bounds or none")
        if begin_period not in (1, 2, 3, 4) or end_period not in (1, 2, 3, 4):
            raise ValueError("Report quarters must be 1 through 4")
        if (begin_year, begin_period) > (end_year, end_period):
            raise ValueError("Report period start exceeds end")
    begin = (begin_year, begin_period) if begin_year is not None else None
    end = (end_year, end_period) if end_year is not None else None
    failures = 0
    downloaded = 0
    with (
        catalog_connection() as conn,
        requests.Session() as session,
        pipeline_run(
            conn,
            "activity_report_downloads",
            {"symbols": symbols, "begin_period": begin, "end_period": end},
        ) as run_id,
    ):
        rows = catalog_rows(conn, symbols, begin, end)
        conn.commit()
        for row in rows:
            disclosure_id = row["disclosure_index"]
            item = {
                "stockCodes": row["symbol"],
                "year": row["fiscal_year"],
                "period": row["fiscal_quarter"],
                "disclosureIndex": disclosure_id,
            }
            output_path = activity_report_path(item, output_dir)
            # Disk is authoritative: a stale downloaded flag cannot hide a deleted file.
            candidates = [output_path]
            previous = row["downloaded_file_path"]
            if previous:
                recorded_path = Path(previous)
                if recorded_path.resolve().is_relative_to(Path(output_dir).resolve()):
                    candidates.append(recorded_path)
            existing = next((p for p in candidates if valid_pdf(p)), None)
            if existing:
                with conn.transaction():
                    complete_download(conn, disclosure_id, existing, run_id)
                logger.info("Already downloaded %s", existing)
                continue
            with conn.transaction():
                start_download(conn, disclosure_id, run_id)
            try:
                link = row["download_url"]
                if not link:
                    link = get_activity_report_pdf_link(
                        session,
                        disclosure_id,
                        retries=retries,
                        retry_backoff=retry_backoff,
                        rate_limit_backoff=rate_limit_backoff,
                        request_sleep=request_sleep,
                    )
                    if not link:
                        raise ValueError("No PDF attachment link found")
                    with conn.transaction():
                        set_download_url(conn, disclosure_id, link)
                download_pdf(
                    session,
                    link,
                    output_path,
                    retries=retries,
                    retry_backoff=retry_backoff,
                    rate_limit_backoff=rate_limit_backoff,
                    request_sleep=request_sleep,
                )
                if not valid_pdf(output_path):
                    raise ValueError("Download did not produce a valid PDF file")
                with conn.transaction():
                    complete_download(conn, disclosure_id, output_path, run_id)
                downloaded += 1
                logger.info("Downloaded %s", output_path)
            except Exception as exc:
                conn.rollback()
                with conn.transaction():
                    fail_download(conn, disclosure_id, exc)
                    if (
                        isinstance(exc, requests.HTTPError)
                        and exc.response is not None
                        and exc.response.status_code in (404, 410)
                    ):
                        set_download_url(conn, disclosure_id, None)
                failures += 1
                logger.exception("Failed disclosure %s; rerun to retry", disclosure_id)
            if sleep_seconds:
                time.sleep(sleep_seconds)
        if failures:
            raise RuntimeError(
                f"{failures} downloads failed and remain pending; rerun to retry"
            )
    return downloaded


def parse_args():
    parser = argparse.ArgumentParser(
        description="Index and download KAP activity report PDFs."
    )
    parser.add_argument("action", choices=["index", "download", "index-and-download"])
    parser.add_argument(
        "--symbols", nargs="+", help="Company symbols. Defaults to all companies."
    )
    parser.add_argument("--begin-date", default="2017-01-01")
    parser.add_argument(
        "--end-date", default=datetime.now(KAP_TIME_ZONE).date().isoformat()
    )
    parser.add_argument("--begin-year", type=int)
    parser.add_argument("--begin-period", type=int)
    parser.add_argument("--end-year", type=int)
    parser.add_argument("--end-period", type=int)
    parser.add_argument(
        "--refresh-index",
        action="store_true",
        help="Requery completed KAP search ranges; existing PDFs are still skipped.",
    )
    parser.add_argument("--output-dir", type=Path, default=ACTIVITY_REPORTS_DIR)
    parser.add_argument("--sleep", type=float, default=0)
    parser.add_argument("--request-sleep", type=float, default=3)
    parser.add_argument(
        "--rate-limit-backoff",
        type=float,
        default=DEFAULT_RATE_LIMIT_BACKOFF,
        help="Base wait in seconds for HTTP 429 retries (default: %(default)s).",
    )
    return parser.parse_args()


def main():
    configure_logging()
    args = parse_args()

    if args.action in {"index", "index-and-download"}:
        index_activity_reports(
            symbols=args.symbols,
            begin_date=args.begin_date,
            refresh=args.refresh_index,
            end_date=args.end_date,
            sleep_seconds=args.sleep,
            request_sleep=args.request_sleep,
            rate_limit_backoff=args.rate_limit_backoff,
        )

    if args.action in {"download", "index-and-download"}:
        download_activity_reports(
            symbols=args.symbols,
            begin_year=args.begin_year,
            begin_period=args.begin_period,
            end_year=args.end_year,
            end_period=args.end_period,
            output_dir=args.output_dir,
            sleep_seconds=args.sleep,
            request_sleep=args.request_sleep,
            rate_limit_backoff=args.rate_limit_backoff,
        )


if __name__ == "__main__":
    main()
