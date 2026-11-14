import logging
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests

from bist.http_utils import send_with_retry
from bist.paths import FINANCIAL_STATEMENTS_DIR

logger = logging.getLogger(__name__)


def download_file(company, year, retries=5, delay=5, request_sleep=20.0, rate_limit_delay=300):
    base_url = "https://www.kap.org.tr/tr/api/home-financial/download-file"

    symbol = company.symbol
    oid = company.oid

    url = f"{base_url}/{oid}/{year}/T"
    company_folder = FINANCIAL_STATEMENTS_DIR / symbol
    company_folder.mkdir(parents=True, exist_ok=True)
    filename = company_folder / f"{symbol}_{year}.zip"

    def send():
        response = requests.get(url, stream=True, timeout=20)
        if request_sleep:
            time.sleep(request_sleep)
        return response

    try:
        response = send_with_retry(
            send,
            retries=retries,
            retry_backoff=delay,
            rate_limit_backoff=rate_limit_delay,
            label=f"{symbol} {year}",
        )
    except requests.RequestException as e:
        logger.error("Failed to download %s %s after %s attempts: %s", symbol, year, retries, e)
        return

    if len(response.content) < 200:
        logger.info("%s does not have a file for %s", symbol, year)
        return
    # Replace an existing archive only after the new one is fully written.
    partial = filename.with_name(filename.name + ".part")
    with open(partial, "wb") as f:
        f.write(response.content)
    partial.replace(filename)
    logger.info("Downloaded %s", filename)


def download(begin_year, end_year, companies, max_workers=1, request_sleep=20.0, rate_limit_delay=300):
    tasks = []
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        for company in companies:
            for year in range(begin_year, end_year + 1):
                tasks.append(executor.submit(
                    download_file,
                    company,
                    year,
                    request_sleep=request_sleep,
                    rate_limit_delay=rate_limit_delay,
                ))

        for future in as_completed(tasks):
            try:
                future.result()
            except Exception:
                logger.exception("Download task failed")
