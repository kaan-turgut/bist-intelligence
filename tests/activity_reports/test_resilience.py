"""Unit tests (no database, no network) for retry and failure-handling behavior."""

import pytest
import requests

from bist import http_utils
from bist.activity_reports import fetcher, processor
from bist.activity_reports.store import quote_in_text


class FakeResponse:
    def __init__(self, status, text="", headers=None):
        self.status_code = status
        self.text = text
        self.headers = headers or {}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(str(self.status_code), response=self)

    def json(self):
        return {}


def scripted(*responses):
    calls = []
    queue = list(responses)

    def send():
        calls.append(1)
        item = queue.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    return send, calls


@pytest.fixture
def sleeps(monkeypatch):
    recorded = []
    monkeypatch.setattr(http_utils.time, "sleep", recorded.append)
    return recorded


def test_permanent_client_errors_are_not_retried(sleeps):
    for status in (400, 401, 404, 410):
        send, calls = scripted(FakeResponse(status))
        with pytest.raises(requests.HTTPError):
            http_utils.send_with_retry(send, retries=5)
        assert len(calls) == 1
    assert sleeps == []


def test_transient_failures_are_retried_then_succeed(sleeps):
    send, calls = scripted(
        FakeResponse(503),
        requests.ConnectionError("reset"),
        FakeResponse(429, headers={"Retry-After": "7"}),
        FakeResponse(200),
    )
    response = http_utils.send_with_retry(send, retries=5, retry_backoff=5)
    assert response.status_code == 200
    assert len(calls) == 4
    assert sleeps == [5, 10, 7]


def test_retries_are_bounded_and_retry_after_is_capped(sleeps):
    send, calls = scripted(
        *[FakeResponse(429, headers={"Retry-After": "999999"}) for _ in range(3)]
    )
    with pytest.raises(requests.HTTPError):
        http_utils.send_with_retry(send, retries=3)
    assert len(calls) == 3
    assert sleeps == [http_utils.MAX_RETRY_AFTER_SECONDS] * 2


def test_retry_if_can_veto_a_retryable_status(sleeps):
    send, calls = scripted(FakeResponse(429, "insufficient_quota"))
    with pytest.raises(requests.HTTPError):
        http_utils.send_with_retry(
            send, retries=5, retry_if=lambda r: "insufficient_quota" not in r.text
        )
    assert len(calls) == 1


def test_kap_request_does_not_retry_a_404(sleeps):
    class Session:
        calls = 0

        def request(self, *args, **kwargs):
            Session.calls += 1
            return FakeResponse(404)

    with pytest.raises(requests.HTTPError):
        fetcher.request_with_backoff(Session(), "GET", "https://kap.test/x")
    assert Session.calls == 1


def test_openai_quota_error_is_fatal_and_not_retried(monkeypatch, sleeps):
    monkeypatch.setenv("OPENAI_API_KEY", "test")
    calls = []

    def post(*args, **kwargs):
        calls.append(1)
        return FakeResponse(429, '{"error": {"code": "insufficient_quota"}}')

    monkeypatch.setattr(processor.requests, "post", post)
    with pytest.raises(processor.OpenAIError) as caught:
        processor.call_openai_embedding("text")
    assert caught.value.fatal
    assert len(calls) == 1


@pytest.mark.parametrize(
    "choice, message",
    [
        ({"finish_reason": "length", "message": {"content": "{"}}, "truncated"),
        ({"finish_reason": "stop", "message": {"refusal": "no"}}, "refused"),
        ({"finish_reason": "stop", "message": {"content": None}}, "Empty"),
    ],
)
def test_extraction_rejects_truncated_refused_and_empty_responses(
    monkeypatch, choice, message
):
    class Posted(FakeResponse):
        def json(self):
            return {"choices": [choice]}

    monkeypatch.setenv("OPENAI_API_KEY", "test")
    monkeypatch.setattr(processor.requests, "post", lambda *a, **kw: Posted(200))
    with pytest.raises(processor.OpenAIError, match=message):
        processor.call_openai_extraction("{}")


def test_failure_summary_reports_unattempted_work():
    processor.raise_for_failures("chunks", [], 3, 3)  # no failures: no error
    with pytest.raises(RuntimeError, match=r"5 of 20 chunks failed and 15 were not"):
        processor.raise_for_failures("chunks", ["a"] * 5, 5, 20)


def test_quote_check_ignores_whitespace_and_case_and_flags_invention():
    text = "Page 3\nThe company will add\n  20 new aircraft."
    assert quote_in_text("will add 20 NEW aircraft", text) is True
    assert quote_in_text("will add 30 new aircraft", text) is False
    assert quote_in_text("", text) is False
    assert quote_in_text("anything", None) is None


def test_extraction_prompt_tells_model_overlap_pages_are_context_only():
    row = {
        "symbol": "THYAO",
        "year": 2026,
        "period": 1,
        "start_page": 3,
        "end_page": 4,
        "included_pages": [3, 4],
        "text": "Page 2\nold\n\nPage 3\nnew",
    }
    assert "included_pages" in processor.build_extraction_user_input(row)
    assert "overlap" in processor.build_extraction_user_input(row)


@pytest.mark.parametrize("workers", [1, 2])
def test_parallel_parsing_keeps_order_and_isolates_bad_pdfs(tmp_path, workers):
    pdfs = []
    for number in range(5):
        path = tmp_path / f"THYAO_2026_{number % 4 + 1}_{number}.pdf"
        path.write_bytes(b"not a pdf")
        pdfs.append((path, "THYAO", 2026, number % 4 + 1, number))
    options = {
        "pages_per_chunk": None,
        "target_chars": 9000,
        "max_chars": 12000,
        "overlap_chars": 700,
    }
    results = list(processor.parsed_pdfs(pdfs, workers, 500, options))
    assert sorted(r[0] for r in results) == sorted(pdfs)  # each PDF exactly once
    assert all(ok is False and "Error" in detail for _, ok, detail in results)


@pytest.fixture
def fake_clock(monkeypatch):
    clock = [0.0]
    monkeypatch.setattr(http_utils.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(
        http_utils.time,
        "sleep",
        lambda seconds: clock.__setitem__(0, clock[0] + seconds),
    )
    return clock


def test_rate_gate_spaces_starts_and_honours_pauses(fake_clock):
    gate = http_utils.RateGate(interval=1.0)
    gate.wait()
    assert fake_clock[0] == 0
    gate.wait()
    assert fake_clock[0] == 1.0
    gate.pause(10)
    gate.wait()
    assert fake_clock[0] == 11.0


def test_a_429_pauses_the_shared_gate_instead_of_sleeping_the_thread(fake_clock):
    gate = http_utils.RateGate()
    send, calls = scripted(
        FakeResponse(429, headers={"Retry-After": "7"}), FakeResponse(200)
    )
    assert http_utils.send_with_retry(send, gate=gate).status_code == 200
    assert len(calls) == 2
    assert fake_clock[0] == 7  # the retry waited out the gate's pause
    gate.wait()  # every other thread would wait on the same gate
    assert fake_clock[0] == 7


def test_concurrent_runner_overlaps_requests_but_saves_on_the_calling_thread():
    import threading
    import time

    rows = [{"chunk_id": n} for n in range(8)]
    main = threading.get_ident()
    in_flight, peak, lock = [0], [0], threading.Lock()
    saved_on = set()

    def call(row):
        with lock:
            in_flight[0] += 1
            peak[0] = max(peak[0], in_flight[0])
        time.sleep(0.1)
        with lock:
            in_flight[0] -= 1
        return row["chunk_id"]

    def on_success(row, result):
        saved_on.add(threading.get_ident())

    started = time.monotonic()
    failures, attempted = processor.run_concurrently(
        rows, call, on_success, lambda r, e: None, workers=4, max_consecutive_failures=5
    )
    elapsed = time.monotonic() - started
    assert (failures, attempted) == ([], 8)
    assert peak[0] == 4  # never more than `workers` in flight
    assert elapsed < 0.6  # serial would take 0.8s
    assert saved_on == {main}  # the database connection is never shared


def test_fatal_error_saves_requests_already_in_flight_but_starts_no_more():
    import time

    rows = [{"chunk_id": n} for n in range(10)]
    saved, failed = [], []

    def call(row):
        if row["chunk_id"] == 0:
            raise processor.OpenAIError("bad key", fatal=True)
        time.sleep(0.1)
        return row["chunk_id"]

    failures, attempted = processor.run_concurrently(
        rows,
        call,
        lambda row, result: saved.append(result),
        lambda row, exc: failed.append(row["chunk_id"]),
        workers=3,
        max_consecutive_failures=5,
    )
    assert attempted == 3  # only the initial window was ever sent
    assert failed == [0] and len(failures) == 1
    assert sorted(saved) == [1, 2]  # paid-for in-flight results are kept


def test_ctrl_c_saves_in_flight_requests_and_then_stops(monkeypatch):
    import time

    real_wait = processor.wait
    interrupts = []

    def wait_once_interrupted(*args, **kwargs):
        if not interrupts:
            interrupts.append(1)
            raise KeyboardInterrupt
        return real_wait(*args, **kwargs)

    monkeypatch.setattr(processor, "wait", wait_once_interrupted)
    saved = []

    def call(row):
        time.sleep(0.05)
        return row["chunk_id"]

    with pytest.raises(KeyboardInterrupt):
        processor.run_concurrently(
            [{"chunk_id": n} for n in range(10)],
            call,
            lambda row, result: saved.append(result),
            lambda row, exc: None,
            workers=3,
            max_consecutive_failures=5,
        )
    assert sorted(saved) == [0, 1, 2]  # the 3 requests in flight were kept


def test_classify_facts_labels_novelty_and_duplicates():
    import numpy as np

    from bist.activity_reports.fact_analysis import classify_facts

    def unit(*values):
        v = np.array(values, dtype=np.float32)
        return v / np.linalg.norm(v)

    a, b, c = unit(1, 0, 0), unit(0, 1, 0), unit(0, 0, 1)
    a_updated = unit(0.85, 0.527, 0)  # cosine 0.85 with a
    documents = [
        ([1, 2], np.vstack([a, b])),
        ([3, 4, 5, 6], np.vstack([a, a_updated, c, c])),
    ]
    result = classify_facts(documents, window=4)
    assert result[1][0] == result[2][0] == "first_report"
    assert result[3][:1] == ("repeated",) and result[3][2] == 1
    assert result[4][0] == "updated" and abs(result[4][1] - 0.85) < 0.01
    assert result[5][0] == "new" and result[5][3] is None
    assert result[6][3] == 5  # near-identical to fact 5 in the same report


def test_extraction_schema_and_input_carry_the_new_fields():
    item = processor.EXTRACTION_SCHEMA["schema"]["properties"]["Extractions"]["items"]
    assert {"themes", "entities", "metrics"} <= set(item["required"])
    assert "confidence" not in item["properties"]
    assert item["properties"]["themes"]["items"]["enum"] == processor.THEMES
    assert "9-10 transformative" in processor.EXTRACTION_SYSTEM_PROMPT
    row = {
        "symbol": "THYAO",
        "year": 2025,
        "period": 4,
        "start_page": 1,
        "end_page": 2,
        "included_pages": [1, 2],
        "text": "x",
    }
    assert "annual report" in processor.build_extraction_user_input(row)
    assert "interim report (Q2)" in processor.build_extraction_user_input(
        {**row, "period": 2}
    )


def test_quote_check_accepts_joined_excerpts_and_pdf_punctuation():
    text = (
        "Yolcu Doluluk Oranı (%) 83.3%\nbaşka sütun metni\nYolcu Sayısı 26,306,627 kişi"
    )
    assert quote_in_text(
        "Yolcu Doluluk Oranı (%) 83.3% ... Yolcu Sayısı 26,306,627", text
    )
    assert not quote_in_text(
        "Yolcu Doluluk Oranı (%) 83.3% ... Yolcu Sayısı 99,999,999", text
    )
    assert quote_in_text("Havalimanı’nın", "Havalimanı'nın işletme hakları")


def test_prompt_forbids_added_inference_and_covers_banks():
    assert "State only what the text says" in processor.EXTRACTION_SYSTEM_PROMPT
    assert {"capital_liquidity", "asset_quality", "lending_deposits"} <= set(
        processor.THEMES
    )
