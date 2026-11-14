"""Post-processing of extracted facts; no re-extraction is needed.

* Novelty: each fact is compared with the same company's previous reports and labelled
  new, updated, repeated or first_report.
* Duplicates: near-identical facts within one report point to the first of them.
* Quality checks: an LLM judge scores a random sample of facts against their source.
"""

import json
import logging

import numpy as np

from bist.activity_reports.processor import (
    DEFAULT_EXTRACTION_MODEL,
    DEFAULT_EXTRACTION_VERSION,
    MAX_CONSECUTIVE_FAILURES,
    OPENAI_EMBEDDINGS_URL,
    OPENAI_GATE,
    OpenAIError,
    chat_json,
    post_openai,
    raise_for_failures,
    run_concurrently,
)
from bist.activity_reports.store import (
    pipeline_run,
    resolve_company_ids,
    writer_connection,
)

logger = logging.getLogger(__name__)

DEFAULT_FACT_EMBEDDING_MODEL = "text-embedding-3-small"
DEFAULT_FACT_EMBEDDING_DIMENSIONS = 512
EMBEDDING_BATCH_SIZE = 100
# Cosine-similarity thresholds. They are heuristics: tune them with the CLI options
# after looking at a few labelled facts.
REPEATED_THRESHOLD = 0.90
RELATED_THRESHOLD = 0.80
DUPLICATE_THRESHOLD = 0.95
HISTORY_WINDOW = 4  # previous reports to compare with (one year of quarters)


def embedding_model_key(model, dimensions):
    return f"{model}@{dimensions}"


def call_fact_embeddings(texts, model, dimensions):
    payload = {"model": model, "input": texts, "dimensions": dimensions}
    data = post_openai(OPENAI_EMBEDDINGS_URL, payload).json()["data"]
    vectors = [item["embedding"] for item in sorted(data, key=lambda d: d["index"])]
    if len(vectors) != len(texts):
        raise OpenAIError(f"Expected {len(texts)} embeddings, got {len(vectors)}")
    return vectors


def _company_filter(company_ids, column="f.company_id"):
    if company_ids is None:
        return "", []
    return f" AND {column}=ANY(%s)", [company_ids]


def facts_missing_embeddings(conn, company_ids, model, version, embedding_key):
    company_sql, company_params = _company_filter(company_ids)
    return conn.execute(
        f"""SELECT f.id, f.info FROM gold.current_activity_report_facts f
            WHERE f.model=%s AND f.extraction_version=%s{company_sql}
              AND NOT EXISTS (SELECT 1 FROM silver.activity_report_fact_embeddings x
                              WHERE x.extraction_id=f.id AND x.model=%s)
            ORDER BY f.id""",
        [model, version, *company_params, embedding_key],
    ).fetchall()


def embed_facts(
    conn, company_ids, model, version, embedding_model, dimensions, workers
):
    key = embedding_model_key(embedding_model, dimensions)
    facts = facts_missing_embeddings(conn, company_ids, model, version, key)
    conn.commit()  # no open transaction during API requests
    batches = [
        {
            "label": f"facts {batch[0]['id']}-{batch[-1]['id']}",
            "facts": batch,
        }
        for batch in (
            facts[i : i + EMBEDDING_BATCH_SIZE]
            for i in range(0, len(facts), EMBEDDING_BATCH_SIZE)
        )
    ]
    logger.info("Embedding %s facts in %s batches", len(facts), len(batches))

    def call(batch):
        texts = [fact["info"] or "" for fact in batch["facts"]]
        return call_fact_embeddings(texts, embedding_model, dimensions)

    def on_success(batch, vectors):
        with conn.transaction(), conn.cursor() as cursor:
            cursor.executemany(
                """INSERT INTO silver.activity_report_fact_embeddings
                   (extraction_id, model, embedding) VALUES (%s,%s,%s)
                   ON CONFLICT (extraction_id, model) DO UPDATE
                   SET embedding=EXCLUDED.embedding, created_at=now()""",
                [
                    (fact["id"], key, vector)
                    for fact, vector in zip(batch["facts"], vectors, strict=True)
                ],
            )

    failures, attempted = run_concurrently(
        batches,
        call,
        on_success,
        lambda batch, exc: conn.rollback(),
        workers=max(1, min(workers, len(batches) or 1)),
        max_consecutive_failures=MAX_CONSECUTIVE_FAILURES,
    )
    return failures, attempted, len(batches)


def classify_facts(
    documents,
    *,
    window=HISTORY_WINDOW,
    repeated=REPEATED_THRESHOLD,
    related=RELATED_THRESHOLD,
    duplicate=DUPLICATE_THRESHOLD,
):
    """Label facts by novelty and within-report duplication.

    ``documents`` is one company's reports, oldest first, as ``(fact_ids, vectors)``
    with unit-length rows. Returns ``{fact_id: (novelty, max_similarity,
    most_similar_fact_id, duplicate_of)}``.
    """
    results = {}
    for position, (ids, vectors) in enumerate(documents):
        if not ids:
            continue
        duplicate_of = [None] * len(ids)
        if len(ids) > 1:
            within = vectors @ vectors.T
            for j in range(1, len(ids)):
                for k in np.nonzero(within[j, :j] >= duplicate)[0]:
                    if duplicate_of[k] is None:  # point at the original, not a copy
                        duplicate_of[j] = ids[k]
                        break
        history = [
            doc for doc in documents[max(0, position - window) : position] if doc[0]
        ]
        if not history:
            for j, fact_id in enumerate(ids):
                results[fact_id] = ("first_report", None, None, duplicate_of[j])
            continue
        history_ids = [fact_id for doc in history for fact_id in doc[0]]
        similarities = vectors @ np.vstack([doc[1] for doc in history]).T
        best = similarities.argmax(axis=1)
        for j, fact_id in enumerate(ids):
            score = float(similarities[j, best[j]])
            if score >= repeated:
                novelty = "repeated"
            elif score >= related:
                novelty = "updated"
            else:
                novelty = "new"
            results[fact_id] = (novelty, score, history_ids[best[j]], duplicate_of[j])
    return results


def _unit_rows(rows):
    vectors = np.asarray(rows, dtype=np.float32)
    norms = np.linalg.norm(vectors, axis=1, keepdims=True)
    norms[norms == 0] = 1
    return vectors / norms


def compute_novelty(conn, company_ids, model, version, embedding_key, **thresholds):
    company_sql, company_params = _company_filter(company_ids, "company_id")
    companies = [
        r["company_id"]
        for r in conn.execute(
            f"""SELECT DISTINCT company_id FROM gold.current_activity_report_facts
                WHERE model=%s AND extraction_version=%s{company_sql}
                ORDER BY company_id""",
            [model, version, *company_params],
        )
    ]
    counts = {}
    for company_id in companies:
        rows = conn.execute(
            """SELECT f.id, f.document_id, x.embedding
               FROM gold.current_activity_report_facts f
               JOIN silver.activity_report_fact_embeddings x
                 ON x.extraction_id=f.id AND x.model=%s
               WHERE f.company_id=%s AND f.model=%s AND f.extraction_version=%s
               ORDER BY f.fiscal_year, f.fiscal_quarter, f.document_id, f.id""",
            (embedding_key, company_id, model, version),
        ).fetchall()
        if not rows:
            continue
        documents, current_doc, ids, vectors = [], None, [], []
        for row in rows:
            if row["document_id"] != current_doc and ids:
                documents.append((ids, _unit_rows(vectors)))
                ids, vectors = [], []
            current_doc = row["document_id"]
            ids.append(row["id"])
            vectors.append(row["embedding"])
        documents.append((ids, _unit_rows(vectors)))
        results = classify_facts(documents, **thresholds)
        with conn.transaction():
            conn.execute(
                "DELETE FROM silver.activity_report_fact_novelty WHERE extraction_id=ANY(%s)",
                (list(results),),
            )
            with conn.cursor() as cursor:
                cursor.executemany(
                    """INSERT INTO silver.activity_report_fact_novelty
                       (extraction_id, embedding_model, novelty, max_similarity,
                        most_similar_extraction_id, duplicate_of)
                       VALUES (%s,%s,%s,%s,%s,%s)""",
                    [
                        (fact_id, embedding_key, *result)
                        for fact_id, result in results.items()
                    ],
                )
        for novelty, *_ in results.values():
            counts[novelty] = counts.get(novelty, 0) + 1
    return counts


def analyze_facts(
    symbols=None,
    model=DEFAULT_EXTRACTION_MODEL,
    extraction_version=DEFAULT_EXTRACTION_VERSION,
    embedding_model=DEFAULT_FACT_EMBEDDING_MODEL,
    dimensions=DEFAULT_FACT_EMBEDDING_DIMENSIONS,
    workers=1,
    request_sleep=0.05,
    window=HISTORY_WINDOW,
    repeated=REPEATED_THRESHOLD,
    related=RELATED_THRESHOLD,
    duplicate=DUPLICATE_THRESHOLD,
):
    """Embed facts that have no embedding yet, then relabel novelty and duplicates.

    Embeddings are paid for once per fact; novelty is recomputed for every fact in
    scope on each run (local computation only), so it stays correct when reports are
    extracted out of order.
    """
    if not (0 < related <= repeated <= 1 and 0 < duplicate <= 1 and window >= 1):
        raise ValueError(
            "Require 0 < related <= repeated <= 1, duplicate <= 1, window >= 1"
        )
    key = embedding_model_key(embedding_model, dimensions)
    with (
        writer_connection() as conn,
        pipeline_run(
            conn,
            "activity_report_fact_analysis",
            {
                "symbols": symbols,
                "model": model,
                "extraction_version": extraction_version,
                "embedding_model": key,
                "window": window,
                "thresholds": [repeated, related, duplicate],
            },
        ),
    ):
        company_ids = resolve_company_ids(conn, symbols)
        OPENAI_GATE.interval = request_sleep
        failures, attempted, total = embed_facts(
            conn,
            company_ids,
            model,
            extraction_version,
            embedding_model,
            dimensions,
            workers,
        )
        conn.commit()
        counts = compute_novelty(
            conn,
            company_ids,
            model,
            extraction_version,
            key,
            window=window,
            repeated=repeated,
            related=related,
            duplicate=duplicate,
        )
        duplicates = conn.execute(
            f"""SELECT count(*) AS n FROM gold.current_activity_report_facts f
                WHERE f.duplicate_of IS NOT NULL AND f.model=%s
                  AND f.extraction_version=%s{_company_filter(company_ids)[0]}""",
            [model, extraction_version, *_company_filter(company_ids)[1]],
        ).fetchone()["n"]
        conn.commit()
        logger.info("Novelty: %s; within-report duplicates: %s", counts, duplicates)
        raise_for_failures("embedding batches", failures, attempted, total)
    return counts, duplicates


JUDGE_SYSTEM_PROMPT = """
You audit facts that another model extracted from a company activity report.
You get the source text and one extracted fact (an English summary, the quoted source
text, the page and any figures). Judge only against the source text.

supported: true if everything the fact states, including every figure, date and
name, is supported by the source text.
score: 5 accurate, specific and useful to an equity analyst; 4 accurate with minor
issues (vague, slightly incomplete); 3 partly supported or low value; 2 contains a
material error (wrong figure, date, entity or meaning); 1 unsupported or fabricated.
issues: one short sentence naming the problem, or an empty string if there is none.
""".strip()

JUDGE_SCHEMA = {
    "name": "activity_report_fact_check",
    "strict": True,
    "schema": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "supported": {"type": "boolean"},
            "score": {"type": "integer", "minimum": 1, "maximum": 5},
            "issues": {"type": "string"},
        },
        "required": ["supported", "score", "issues"],
    },
}


def sample_facts_for_check(conn, company_ids, model, version, judge_model, size):
    company_sql, company_params = _company_filter(company_ids)
    return conn.execute(
        f"""SELECT f.id, f.symbol, f.fiscal_year, f.fiscal_quarter, f.page, f.info,
                   f.source_quote, c.text AS chunk_text,
                   (SELECT coalesce(json_agg(json_build_object(
                        'name', x.name, 'value', x.value, 'unit', x.unit,
                        'period', x.period) ORDER BY x.metric_number), '[]'::json)
                    FROM silver.activity_report_fact_metrics x
                    WHERE x.extraction_id=f.id) AS metrics
            FROM gold.current_activity_report_facts f
            JOIN silver.activity_report_chunks c ON c.id=f.chunk_id
            WHERE f.model=%s AND f.extraction_version=%s{company_sql}
              AND NOT EXISTS (SELECT 1 FROM ops.activity_report_quality_checks q
                              WHERE q.extraction_id=f.id AND q.judge_model=%s)
            ORDER BY random() LIMIT %s""",
        [model, version, *company_params, judge_model, size],
    ).fetchall()


def build_judge_input(row):
    return json.dumps(
        {
            "fact": {
                "info": row["info"],
                "source_quote": row["source_quote"],
                "page": row["page"],
                "metrics": row["metrics"],
            },
            "source_text": row["chunk_text"],
        },
        ensure_ascii=False,
    )


def call_judge(judge_input, model):
    return chat_json(JUDGE_SYSTEM_PROMPT, judge_input, JUDGE_SCHEMA, model)[0]


def quality_check(
    symbols=None,
    model=DEFAULT_EXTRACTION_MODEL,
    extraction_version=DEFAULT_EXTRACTION_VERSION,
    judge_model=DEFAULT_EXTRACTION_MODEL,
    sample_size=100,
    workers=1,
    request_sleep=0.25,
):
    """Score a random sample of not-yet-checked facts; returns a summary dict."""
    if sample_size < 1:
        raise ValueError("sample_size must be positive")
    with (
        writer_connection() as conn,
        pipeline_run(
            conn,
            "activity_report_quality_check",
            {
                "symbols": symbols,
                "model": model,
                "extraction_version": extraction_version,
                "judge_model": judge_model,
                "sample_size": sample_size,
            },
        ) as run_id,
    ):
        company_ids = resolve_company_ids(conn, symbols)
        rows = [
            {**row, "label": f"fact {row['id']}"}
            for row in sample_facts_for_check(
                conn, company_ids, model, extraction_version, judge_model, sample_size
            )
        ]
        conn.commit()
        logger.info("Checking %s facts with %s", len(rows), judge_model)
        OPENAI_GATE.interval = request_sleep

        def on_success(row, verdict):
            with conn.transaction():
                conn.execute(
                    """INSERT INTO ops.activity_report_quality_checks
                       (extraction_id, judge_model, supported, score, issues, pipeline_run_id)
                       VALUES (%s,%s,%s,%s,%s,%s)
                       ON CONFLICT (extraction_id, judge_model) DO UPDATE
                       SET supported=EXCLUDED.supported, score=EXCLUDED.score,
                           issues=EXCLUDED.issues, pipeline_run_id=EXCLUDED.pipeline_run_id,
                           checked_at=now()""",
                    (
                        row["id"],
                        judge_model,
                        verdict["supported"],
                        verdict["score"],
                        verdict["issues"] or None,
                        run_id,
                    ),
                )

        failures, attempted = run_concurrently(
            rows,
            lambda row: call_judge(build_judge_input(row), judge_model),
            on_success,
            lambda row, exc: conn.rollback(),
            workers=max(1, min(workers, len(rows) or 1)),
            max_consecutive_failures=MAX_CONSECUTIVE_FAILURES,
        )
        summary = conn.execute(
            """SELECT count(*) AS checked,
                      round(avg(supported::int) * 100, 1) AS supported_pct,
                      round(avg(score), 2) AS avg_score,
                      count(*) FILTER (WHERE score <= 2) AS serious_problems
               FROM ops.activity_report_quality_checks WHERE pipeline_run_id=%s""",
            (run_id,),
        ).fetchone()
        worst = conn.execute(
            """SELECT f.symbol, f.fiscal_year, f.fiscal_quarter, f.page, q.score,
                      q.issues, f.info
               FROM ops.activity_report_quality_checks q
               JOIN gold.current_activity_report_facts f ON f.id=q.extraction_id
               WHERE q.pipeline_run_id=%s AND q.score <= 2
               ORDER BY q.score, f.symbol LIMIT 10""",
            (run_id,),
        ).fetchall()
        conn.commit()
        raise_for_failures("fact checks", failures, attempted, len(rows))
    return dict(summary), worst


def print_quality_summary(summary, worst):
    print(
        f"Checked {summary['checked']} facts: {summary['supported_pct']}% fully "
        f"supported, average score {summary['avg_score']}/5, "
        f"{summary['serious_problems']} with serious problems (score <= 2)."
    )
    for row in worst:
        print(
            f"- {row['symbol']} {row['fiscal_year']} Q{row['fiscal_quarter']} p.{row['page']} "
            f"score {row['score']}: {row['issues']}\n    fact: {row['info']}"
        )
    print("All verdicts are stored in ops.activity_report_quality_checks.")
