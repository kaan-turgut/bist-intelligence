"""Regressions for EPS unit errors found in published data (2026-11 audit)."""

from datetime import date, timedelta
from decimal import Decimal

import pytest

from bist.financials.models.eps_capital import CapitalHistory, implausible_verified_eps
from bist.financials.models.financial_values import CpiSeries
from bist.financials.parsing import eps as extractor
from bist.financials.parsing.eps import _nominal_values, parse_number
from bist.financials.services.eps_coverage import calculate_eps_coverage


@pytest.mark.parametrize(
    "text",
    [
        # PAPIL: a buyback of shares worth 214.000 TL in total.
        "sermayesinin %0,62'sine denk gelen toplam 214.000 TL nominal değerli pay",
        # KRVGD: a share-class holding, not the nominal value of one share.
        "529.920 TL nominal değerli 529.920 adet B grubu payların",
        # DZGYO: privileged shares worth 5.000 TL in total.
        "Şirket'in 5.000 TL nominal değerli imtiyazlı A tipi pay bulunmaktadır.",
        # GSDDE: the C-group block is worth 2,815 TL in total.
        "31 Mart 2017 tarihi itibarıyla 2,815 TL nominal değerli C grubu",
    ],
)
def test_aggregate_nominal_amounts_are_not_per_share_values(text):
    assert _nominal_values(text) == set()


@pytest.mark.parametrize(
    "text",
    [
        # ZERAY: "her biri" belongs to the 1 TL value, not the class total.
        "her biri 1TL nominal değerde, 51.000.000TL nominal değerli A grubu",
        # ÜNLÜ: a subsidiary stake described after its per-share value.
        "250.000.000 adet her biri 1 kuruş toplam 2.500.000 TL nominal değerde",
    ],
)
def test_per_share_cue_must_directly_precede_the_amount(text):
    assert max(_nominal_values(text)) <= 1


@pytest.mark.parametrize(
    "text,expected",
    [
        # TTRAK: "beheri 1 kuruş olan", next to "nominal değeri 53.369.000 TL".
        (
            (
                "sermayesi beheri 1 kuruş olan 5.336.900.000 adet hisseden oluşmakta "
                "olup, nominal değeri 53.369.000 TL'dir."
            ),
            Decimal("0.01"),
        ),
        ("beheri 1 tam TL nominal değerde 66.000.000 adet", Decimal(1)),
        ("her biri 2.500.000,50 TL değerinde", Decimal("2500000.50")),
        ("Beheri 1 kr. nominal değerli hisse senetleri", Decimal("0.01")),
        ("her biri 0,3127595 TL nominal değerli", Decimal("0.3127595")),
        ("nominal değeri 0,1 kuruş olan 120.000.000.000 adet", Decimal("0.001")),
        ("1 TL nominal değerli paylardan oluşur", Decimal(1)),
        ("her biri 2,5 TL nominal değerli", Decimal("2.5")),
    ],
)
def test_per_share_nominal_values_use_turkish_separators(text, expected):
    assert _nominal_values(text) == {expected}


@pytest.mark.parametrize(
    "text",
    [
        # KBORU means 1 TL here (274,273,161 shares make 274,273,161 TL) ...
        "Şirket'in bir hissesinin nominal değeri 1.000 TL'dir.",
        # ... while this subsidiary text really means 1,000 TL per share.
        "her biri 1.000 TL nominal değerindeki 50 adet payı 50.000 TL bedel ile",
        "her biri 5.000 TL değerinde 10.000 adet",
    ],
)
def test_single_dotted_group_nominal_value_is_not_guessed(text):
    assert _nominal_values(text) == set()


def test_dot_decimal_reading_is_opt_in():
    assert parse_number("(25.870)") == Decimal(-25870)
    assert parse_number("(25.870)", dot_decimal=True) == Decimal("-25.870")
    # Multiple groups are unambiguous thousands separators.
    assert parse_number("1.234.567", dot_decimal=True) == Decimal(1234567)


OTKAR_NOTE = """KONSOLİDE FİNANSAL TABLOLARA İLİŞKİN AÇIKLAYICI NOTLAR
(Tutarlar aksi belirtilmedikçe bin Türk Lirası olarak ifade edilmiştir.)
27. Pay başına kazanç
                                             1 Ocak -                 1 Ocak -
                                       31 Aralık 2024            31 Aralık 2023
Net dönem (zararı)/karı                     (3.104.429)                2.841.321
Beheri 1 kr. nominal değerli
hisselerin ağırlıklı ortalama adedi      12.000.000.000           12.000.000.000
Pay başına (kayıp)/kazanç (Kuruş)              (25.870)                  23,678
28. İlişkili taraf açıklamaları
"""


def test_dot_decimal_eps_is_accepted_only_when_profit_and_shares_prove_it(monkeypatch):
    monkeypatch.setattr(extractor, "pdf_pages", lambda _: [OTKAR_NOTE])
    values, _ = extractor.extract_pdf_eps(b"fixture", 2024, 4)
    assert len(values) == 1
    # -3,104,429k TL / 12bn shares = -25.87 kuruş per 1 kuruş share.
    assert values[0].value == Decimal("-25.870")
    assert values[0].normalization["raw_reported_eps"] == "-25.870"


def test_ambiguous_face_eps_without_inputs_is_not_guessed(monkeypatch):
    face = """KONSOLİDE KAR VEYA ZARAR TABLOSU
(Tutarlar aksi belirtilmedikçe bin Türk Lirası olarak ifade edilmiştir.)
                                             1 Ocak -                 1 Ocak -
                                       31 Aralık 2024            31 Aralık 2023
Beheri 1 kr. nominal değerli
Pay Başına Kazanç/(Zarar) (Kuruş)              (25.870)                  35.275
"""
    monkeypatch.setattr(extractor, "pdf_pages", lambda _: [face])
    values, payload = extractor.extract_pdf_eps(b"fixture", 2024, 4)
    assert values == []
    assert any("ambiguous" in w for w in payload["extraction_warnings"])


def capital(days, value, start=date(2024, 1, 1)):
    return CapitalHistory(
        [
            {"price_date": start + timedelta(days=i), "capital": Decimal(value)}
            for i in range(days)
        ]
    )


def test_plausibility_accepts_consistent_and_bonus_adjusted_denominators():
    start, end = date(2024, 1, 1), date(2024, 12, 31)
    history = capital(400, 120_000_000)
    assert implausible_verified_eps(
        Decimal("-25.87"), Decimal(-3_104_429_000), start, end, history
    ) is None
    # A 900% bonus issue after period end but before publication is restated
    # retrospectively; the denominator matches post-period capital.
    rows = [
        {"price_date": date(2024, 1, 1) + timedelta(days=i), "capital": Decimal(280)}
        for i in range(366)
    ] + [{"price_date": date(2025, 2, 1), "capital": Decimal(2800)}]
    assert implausible_verified_eps(
        Decimal(5), Decimal(14000), start, end, CapitalHistory(rows)
    ) is None


@pytest.mark.parametrize(
    "verified",
    [Decimal(-258700), Decimal("-0.2587"), Decimal("-2.587"), Decimal(2587)],
)
def test_plausibility_rejects_power_of_ten_unit_errors(verified):
    rejection = implausible_verified_eps(
        verified,
        Decimal(-3_104_429_000),
        date(2024, 1, 1),
        date(2024, 12, 31),
        capital(400, 120_000_000),
    )
    assert rejection["reason"] == "verified_eps_implies_implausible_nominal_denominator"


def test_sign_disagreement_alone_is_not_treated_as_a_unit_error():
    # The Gold profit can be the wrong side (e.g. a derived quarter).
    assert implausible_verified_eps(
        Decimal("25.87"),
        Decimal(-3_104_429_000),
        date(2024, 1, 1),
        date(2024, 12, 31),
        capital(400, 120_000_000),
    ) is None


def test_plausibility_uses_reported_capital_and_skips_without_any_capital():
    args = (Decimal("0.051"), Decimal(14_269_748_000), date(2024, 1, 1), date(2024, 9, 30))
    assert implausible_verified_eps(*args, None) is None
    assert implausible_verified_eps(*args, None, Decimal(2_798_078_602))[
        "capital_source"
    ] == "reported_paid_in_capital"


def test_coverage_withdraws_implausible_verified_eps_for_shared_sources(database):
    conn, company, run = database
    details = '{"source_ids":[7],"ttm_source_ids":[7]}'
    conn.execute(
        """INSERT INTO gold.eps_values(calculation_run_id,company_id,fiscal_year,fiscal_quarter,
        eps_kind,share_class,earnings_scope,value_quarter,value_ytd,calculation_details)
        VALUES (%s,%s,2026,1,'basic','ordinary','total',-1000,-1000,%s),
               (%s,%s,2026,1,'basic','ordinary','continuing',-1000,-1000,%s),
               (%s,%s,2026,1,'diluted','ordinary','total',-9,-9,'{"source_ids":[8]}')""",
        (run, company, details, run, company, details, run, company),
    )
    conn.execute(
        """INSERT INTO gold.financial_values(calculation_run_id,company_id,statement_type,
        fiscal_year,fiscal_quarter,line_item_path,item,value_quarter,value_ytd,period_start,
        period_end,measurement_basis,calculation_details)
        VALUES (%s,%s,'income_statement',2026,1,
        'distribution of profit (loss) for the period > ana ortaklık payları','ana ortaklık payları',
        -1000,-1000,'2026-01-01','2026-03-31','nominal','{"ytd_days":90}')""",
        (run, company),
    )
    history = capital(90, 100, date(2026, 1, 1))
    counts = calculate_eps_coverage(conn, run, {company: history}, CpiSeries([]))
    assert counts["verified_rejected"] == 2
    coverage = conn.execute("SELECT * FROM gold.eps_coverage").fetchone()
    assert coverage["estimated_value_ytd"] == -10
    quality = coverage["assessment"]["quality"]
    assert quality["ytd"]["status"] == "estimated"
    assert quality["ytd"]["verified_rejected"]
    assert coverage["assessment"]["review_required"]
    rows = {
        (r["eps_kind"], r["earnings_scope"]): r
        for r in conn.execute("SELECT * FROM gold.eps_values").fetchall()
    }
    for key in (("basic", "total"), ("basic", "continuing")):
        assert rows[key]["value_ytd"] is None and rows[key]["value_quarter"] is None
        rejected = rows[key]["calculation_details"]["plausibility_rejections"]
        assert rejected["ytd"]["withdrawn_value"] == "-1000.000000000000"
    # A type backed by a different source is left for its own review.
    assert rows[("diluted", "total")]["value_ytd"] == -9


def test_same_raw_eps_with_corrected_nominal_replaces_legacy_observation():
    from types import SimpleNamespace

    from bist.financials.models.earnings_per_share import normalize_observation
    from bist.financials.services.earnings_per_share import preserve_verified_eps

    def dzgyo(nominal):
        return normalize_observation(
            period_start=date(2017, 1, 1),
            period_end=date(2017, 12, 31),
            period_kind="ytd",
            reported_eps="0.31576",
            share_nominal_value=nominal,
            normalization={"validation": "reconciled_note", "source_page": 30},
        )

    # DZGYO once divided by "5.000 TL nominal değerli imtiyazlı A tipi pay".
    old, current = dzgyo("5.000"), dzgyo("1")
    conn = SimpleNamespace(
        execute=lambda *args: SimpleNamespace(
            fetchall=lambda: [{**vars(old), "source_id": 9196}]
        )
    )
    payload = {}
    observations = preserve_verified_eps(
        conn,
        {"company_id": 1, "fiscal_year": 2017, "fiscal_quarter": 4, "filing_key": 1},
        [current],
        payload,
    )
    assert [o.value for o in observations] == [Decimal("0.31576")]
    assert payload["corrected_legacy_nominal_values"] == [
        {"source_id": 9196, "period_kind": "ytd", "from": "5.000", "to": "1"}
    ]


def test_source_whose_nominal_value_is_no_longer_established_is_retired(database):
    from bist.financials.models.earnings_per_share import normalize_observation
    from bist.financials.services.earnings_per_share import (
        quarantine_conflicting_eps_source,
        store_eps_source,
    )

    conn, company, run = database
    filing = conn.execute(
        """INSERT INTO bronze.statement_filings
        (company_id,fiscal_year,fiscal_quarter,statement_type,source_filename,source_path,source_sha256,run_id)
        VALUES (%s,2017,1,'income_statement','t.xls','t.zip/t.xls','t',%s) RETURNING filing_key""",
        (company, run),
    ).fetchone()["filing_key"]
    obs = normalize_observation(
        period_start=date(2017, 1, 1),
        period_end=date(2017, 3, 31),
        period_kind="ytd",
        reported_eps=-121,
        share_nominal_value="2.815",
    )
    store_eps_source(
        conn, company_id=company, year=2017, quarter=1, source_path="t",
        source_hash="t", source_kind="kap_html", payload={}, observations=[obs],
        run_id=run, filing_key=filing,
    )
    error = ValueError(
        "No EPS with verified units and matching reporting dates; page 30: "
        "EPS source does not establish one unambiguous nominal share value."
    )
    row = {
        "company_id": company,
        "fiscal_year": 2017,
        "fiscal_quarter": 1,
        "filing_key": filing,
    }
    assert quarantine_conflicting_eps_source(conn, row, run, error) is True
    assert conn.execute("SELECT count(*) n FROM ops.active_eps_sources").fetchone()["n"] == 0
    # Bronze and Silver history stay available for review.
    assert conn.execute("SELECT count(*) n FROM silver.eps_observations").fetchone()["n"] == 1
