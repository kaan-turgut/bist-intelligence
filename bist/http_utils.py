"""HTTP retry logic shared by the KAP, İş Yatırım and OpenAI clients."""

import logging
import threading
import time

import requests

logger = logging.getLogger(__name__)

RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})
TRANSIENT_ERRORS = (
    requests.ConnectionError,
    requests.Timeout,
    requests.exceptions.ChunkedEncodingError,
)
MAX_RETRY_AFTER_SECONDS = 900.0


def retry_after_seconds(response):
    """Numeric Retry-After header, capped so a bad header cannot stall a run."""
    value = response.headers.get("Retry-After")
    if value and value.strip().isdecimal():
        return min(float(value), MAX_RETRY_AFTER_SECONDS)
    return None


class RateGate:
    """Coordinates concurrent callers of one API.

    ``pause`` makes every thread wait after any one of them is rate limited, so a
    429 is not followed by a burst of doomed requests from the other threads.
    ``wait`` also spaces request starts at least ``interval`` seconds apart.
    """

    def __init__(self, interval=0.0):
        self.interval = interval
        self._lock = threading.Lock()
        self._blocked_until = 0.0
        self._next_start = 0.0

    def pause(self, seconds):
        with self._lock:
            self._blocked_until = max(self._blocked_until, time.monotonic() + seconds)

    def wait(self):
        while True:
            with self._lock:
                now = time.monotonic()
                start = max(now, self._blocked_until, self._next_start)
                if start <= now:
                    self._next_start = now + self.interval
                    return
            time.sleep(start - now)


def send_with_retry(
    send,
    *,
    retries=5,
    retry_backoff=5,
    rate_limit_backoff=300,
    label="",
    retry_if=None,
    gate=None,
):
    """Call ``send()`` until it returns a non-retryable response.

    Only HTTP 429/5xx and transient network errors are retried. Any other status
    (404, 401, ...) raises ``requests.HTTPError`` immediately, because repeating
    the request cannot change the answer. ``retry_if(response)`` can veto a retry
    of a retryable status, e.g. a quota error that will not clear by waiting. With a
    ``gate`` shared between threads, a 429 pauses all of them, not just this caller.
    """
    if retries < 1:
        raise ValueError("retries must be at least 1")
    context = f" [{label}]" if label else ""
    last_error = None
    for attempt in range(1, retries + 1):
        if gate is not None:
            gate.wait()
        try:
            response = send()
        except TRANSIENT_ERRORS as exc:
            last_error = exc
            if attempt >= retries:
                break
            delay = retry_backoff * attempt
            logger.warning(
                "Request failed%s: %s; retrying in %.1fs (%s/%s)",
                context,
                exc,
                delay,
                attempt,
                retries,
            )
            time.sleep(delay)
            continue

        if (
            response.status_code in RETRYABLE_STATUS
            and attempt < retries
            and (retry_if is None or retry_if(response))
        ):
            backoff = (
                rate_limit_backoff if response.status_code == 429 else retry_backoff
            )
            delay = retry_after_seconds(response) or backoff * attempt
            logger.warning(
                "HTTP %s%s; retrying in %.1fs (%s/%s)",
                response.status_code,
                context,
                delay,
                attempt,
                retries,
            )
            if gate is not None and response.status_code == 429:
                gate.pause(delay)  # the next gate.wait() sleeps for every thread
            else:
                time.sleep(delay)
            continue

        response.raise_for_status()
        return response

    raise last_error
