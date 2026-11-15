"""Dated nominal-capital arithmetic; estimates never become verified EPS."""

from bisect import bisect_right
from datetime import timedelta
from decimal import Decimal


class CapitalHistory:
    def __init__(self, rows):
        self.rows = []
        for row in sorted(rows, key=lambda r: r["price_date"]):
            value = Decimal(str(row["capital"])) if row["capital"] is not None else None
            self.rows.append(
                {
                    **row,
                    "capital": value
                    if value is not None and value.is_finite()
                    else None,
                }
            )
        self.dates = [r["price_date"] for r in self.rows]
        self.regimes = []
        regime, previous = None, None
        for row in self.rows:
            capital = row["capital"]
            if capital != previous:
                regime = f"{row['price_date']}:{capital}"
            self.regimes.append(regime)
            previous = capital

    def period(self, start, end, *, allow_opening_estimate=False):
        """Calendar-day weighting, carrying trading observations at most 10 days."""
        if start is None or end is None or end < start:
            return None
        index = bisect_right(self.dates, start) - 1
        opening_assumption = None
        if index < 0:
            if (
                not allow_opening_estimate
                or not self.dates
                or not 0 < (self.dates[0] - start).days <= 7
                or self.dates[0] > end
            ):
                return None
            index = 0
            opening_assumption = {
                "method": "first_observed_capital_carried_back_to_period_open",
                "observed_date": str(self.dates[0]),
                "assumed_from": str(start),
            }
        cursor, total, regimes = start, Decimal(0), set()
        while cursor <= end:
            row = self.rows[index]
            stop = min(
                end + timedelta(days=1),
                self.dates[index + 1]
                if index + 1 < len(self.dates)
                else end + timedelta(days=1),
            )
            if (
                row["capital"] is None
                or row["capital"] <= 0
                or (stop - timedelta(days=1) - row["price_date"]).days > 10
            ):
                return None
            total += row["capital"] * (stop - cursor).days
            regimes.add(self.regimes[index])
            cursor = stop
            index += 1
        return {
            "weighted_capital": total / Decimal((end - start).days + 1),
            "stable_basis": next(iter(regimes)) if len(regimes) == 1 else None,
            "regimes": sorted(regimes),
            "opening_assumption": opening_assumption,
        }


    def bounds(self, start, end):
        """Lowest/highest issued capital in effect between two dates."""
        if start is None or end is None or end < start or not self.dates:
            return None
        first = max(bisect_right(self.dates, start) - 1, 0)
        stop = bisect_right(self.dates, end)
        values = [
            r["capital"]
            for r in self.rows[first:stop]
            if r["capital"] is not None and r["capital"] > 0
        ]
        if not values or self.dates[first] > end:
            return None
        return min(values), max(values)


# A verified EPS implies a nominal denominator (profit / EPS). Unit and nominal
# extraction errors are powers of ten; corporate actions inside the band stay.
PLAUSIBLE_DENOMINATOR_LOW = Decimal("0.25")
PLAUSIBLE_DENOMINATOR_HIGH = Decimal(2)
# IAS 33 restates EPS for bonus issues approved before the report is issued.
POST_PERIOD_CAPITAL_DAYS = 120


def implausible_verified_eps(verified, profit, start, end, capital, paid_capital=None):
    """Return evidence when verified EPS contradicts dated issued capital.

    Historical capital is only a plausibility bound here; it never replaces the
    disclosed denominator. Returns None when the check passes or is not possible.
    """
    if verified is None or profit is None or verified == 0 or profit == 0:
        return None
    bounds = (
        capital.bounds(start, end + timedelta(days=POST_PERIOD_CAPITAL_DAYS))
        if capital is not None
        else None
    )
    source = "IsYatirim.SERMAYE"
    if bounds is None and paid_capital:
        bounds, source = (paid_capital, paid_capital), "reported_paid_in_capital"
    if bounds is None:
        return None
    # Only magnitude is tested: a sign disagreement can also come from the
    # Gold profit (for example a derived quarter), so it is not proof of an
    # EPS unit error.
    implied = abs(profit / verified)
    low = bounds[0] * PLAUSIBLE_DENOMINATOR_LOW
    high = bounds[1] * PLAUSIBLE_DENOMINATOR_HIGH
    if low <= implied <= high:
        return None
    return {
        "reason": "verified_eps_implies_implausible_nominal_denominator",
        "verified_value": str(verified),
        "parent_profit": str(profit),
        "implied_nominal_denominator": str(implied),
        "capital_bounds": [str(bounds[0]), str(bounds[1])],
        "capital_source": source,
        "accepted_band": [str(PLAUSIBLE_DENOMINATOR_LOW), str(PLAUSIBLE_DENOMINATOR_HIGH)],
    }


def verified_share_basis(observation, capital):
    """A constant issued-capital regime rules out unhandled bonus-capital changes.

    The observation itself must disclose the actual outstanding denominator;
    price-feed capital is never substituted for that verified count.
    """
    if observation.share_basis_id or not observation.has_inputs or capital is None:
        return observation
    period = capital.period(observation.period_start, observation.period_end)
    if not period or not period["stable_basis"]:
        return observation
    presentation_quantum = Decimal(
        observation.normalization.get("shares_multiplier", "1")
    ) * Decimal(observation.normalization.get("share_nominal_value", "1"))
    if (
        observation.weighted_average_shares
        > period["weighted_capital"] + presentation_quantum
    ):
        return observation
    from dataclasses import replace

    return replace(
        observation,
        share_basis_id="verified_nominal_regime:" + period["stable_basis"],
        normalization={
            **observation.normalization,
            "share_basis_evidence": {
                "method": "disclosed_outstanding_denominator_with_stable_issued_capital",
                "period_start": str(observation.period_start),
                "period_end": str(observation.period_end),
                "issued_capital": str(period["weighted_capital"]),
            },
        },
    )
