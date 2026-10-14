"""Accounting invariants of the synthetic book."""

import hashlib

import pytest

from isl.generate import Config, generate
from tests.conftest import SMALL


def _scalar(con, sql):
    return con.execute(sql).fetchone()[0]


def test_deterministic():
    a, b = generate(Config(seed=3, quotes_per_month=150)), generate(Config(seed=3, quotes_per_month=150))
    for name in a:
        assert hashlib.sha256(repr(a[name]).encode()).digest() == hashlib.sha256(repr(b[name]).encode()).digest()


def test_claims_reported_after_accident_and_within_cover(con):
    assert _scalar(con, "SELECT count(*) FROM claim WHERE report_date < accident_date") == 0
    assert _scalar(con, """
        SELECT count(*) FROM claim c JOIN policy_term t USING (term_id)
        WHERE c.accident_date < t.inception_date
           OR c.accident_date >= COALESCE(t.cancel_date, t.expiry_date)""") == 0


def test_nothing_known_after_horizon(con):
    h = f"DATE '{SMALL.horizon}'"
    for table, col in [("premium_txn", "booked_date"), ("claim", "report_date"), ("claim_txn", "txn_date"),
                       ("ibnr_valuation", "valuation_date"), ("expense", "booked_date"), ("quote", "quote_date")]:
        assert _scalar(con, f"SELECT count(*) FROM {table} WHERE {col} > {h}") == 0, table


def test_case_reserve_never_negative(con):
    # Running case reserve per claim, at every transaction date.
    assert _scalar(con, """
        SELECT count(*) FROM (
          SELECT sum(amount) OVER (PARTITION BY claim_id ORDER BY txn_date, claim_txn_id) AS bal
          FROM claim_txn WHERE txn_type = 'RESERVE') WHERE bal < -0.05""") == 0


def test_closed_claims_have_no_case_reserve(con):
    h = f"DATE '{SMALL.horizon}'"
    assert _scalar(con, f"""
        SELECT count(*) FROM (
          SELECT c.claim_id, sum(x.amount) AS reserve FROM claim c JOIN claim_txn x USING (claim_id)
          WHERE x.txn_type = 'RESERVE' AND c.closed_date <= {h} GROUP BY 1) WHERE abs(reserve) > 0.05""") == 0


def test_expired_terms_earn_exactly_their_written_premium(con):
    # Every row earns over [effective, cover_end); integrated over the whole interval it earns its amount,
    # so a fully expired term's earned premium equals its net written premium.
    from isl.compiler import MetricQuery, run

    _cols, rows = run(con, MetricQuery(metrics=["earned_premium", "written_premium"], basis="policy",
                                      as_of=SMALL.horizon))
    for period, earned, written in rows:
        if int(period) <= 2023:  # every term incepting by 2023 has expired by the horizon
            assert earned == pytest.approx(written, abs=0.5), period
    # A cancelled term's net written premium equals the premium earned up to the cancellation date.
    worst = _scalar(con, """
        WITH t AS (SELECT term_id, cancel_date FROM policy_term WHERE cancel_date IS NOT NULL)
        SELECT max(abs(net - earned_to_cancel)) FROM (
          SELECT p.term_id, sum(p.written_premium) AS net,
                 sum(CASE WHEN p.txn_type = 'CANCEL' THEN 0 ELSE p.written_premium *
                     date_diff('day', p.effective_date, t.cancel_date) /
                     date_diff('day', p.effective_date, p.cover_end_date) END) AS earned_to_cancel
          FROM premium_txn p JOIN t USING (term_id)
          WHERE p.booked_date <= DATE '2025-12-31'
          GROUP BY 1 HAVING count(*) FILTER (WHERE p.txn_type = 'CANCEL') = 1)""")
    assert worst < 0.05


def test_renewal_prices_capped_after_reform(con):
    assert _scalar(con, "SELECT count(*) FROM renewal_offer WHERE due_date >= DATE '2022-01-01' "
                        "AND offered_premium > enbp") == 0


def test_truth_covers_reported_closed_claims(con):
    # For claims closed well before the horizon (no later recoveries pending), truth equals paid + ALAE.
    mismatches = _scalar(con, """
        SELECT count(*) FROM truth_claim_cost t JOIN claim c USING (claim_id)
        JOIN (SELECT claim_id, sum(amount) FILTER (WHERE txn_type <> 'RESERVE') AS paid
              FROM claim_txn GROUP BY 1) x USING (claim_id)
        WHERE c.closed_date <= DATE '2024-06-30' AND abs(t.ultimate_cost - x.paid) > 0.05""")
    assert mismatches == 0


def test_earned_exposure_matches_term_days(con):
    """Earned car-years from transactions must equal covered days / term days, summed over terms."""
    from datetime import date

    from isl.compiler import MetricQuery, run

    _cols, rows = run(con, MetricQuery(metrics=["earned_exposure"], basis="calendar", as_of=SMALL.horizon))
    got = {r[0]: r[1] for r in rows}
    for year in (2022, 2024):
        start, end = date(year, 1, 1), date(year + 1, 1, 1)
        expected = _scalar(con, f"""
            SELECT sum(greatest(0, date_diff('day', greatest(inception_date, DATE '{start}'),
                                             least(coalesce(cancel_date, expiry_date), DATE '{end}')))
                       / date_diff('day', inception_date, expiry_date))
            FROM policy_term t
            WHERE cancel_date IS NULL OR EXISTS (
                SELECT 1 FROM premium_txn p WHERE p.term_id = t.term_id AND p.txn_type = 'CANCEL')""")
        assert got[str(year)] == pytest.approx(float(expected), rel=1e-6), year
