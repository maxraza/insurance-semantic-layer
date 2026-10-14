"""The scenarios in openspec/changes/build-insurance-semantic-layer/specs/metric-semantics."""

from datetime import date

import pytest

from isl.compiler import Compiler, MetricQuery, SemanticError, run
from tests.conftest import AS_OF


def _one(con, **kw):
    cols, rows = run(con, MetricQuery(**kw))
    return {r[0]: dict(zip(cols, r)) for r in rows}


def test_basis_is_required():
    with pytest.raises(SemanticError, match="basis is required"):
        Compiler().compile(MetricQuery(metrics=["loss_ratio_reported"], basis=None, as_of=AS_OF))


def test_as_of_is_required():
    with pytest.raises(SemanticError, match="as_of"):
        Compiler().compile(MetricQuery(metrics=["loss_ratio_reported"], basis="accident", as_of=None))


def test_valuation_date_hides_later_facts(con):
    early = _one(con, metrics=["reported_loss"], basis="accident", as_of=date(2023, 12, 31))
    late = _one(con, metrics=["reported_loss"], basis="accident", as_of=AS_OF)
    assert early["2023"]["reported_loss"] != late["2023"]["reported_loss"]
    later_txns = con.execute("""
        SELECT sum(x.amount) FROM claim_txn x JOIN claim c USING (claim_id)
        WHERE year(c.accident_date) = 2023 AND x.cost_type = 'INDEMNITY' AND x.txn_date > DATE '2023-12-31'
    """).fetchone()[0]
    assert late["2023"]["reported_loss"] - early["2023"]["reported_loss"] == pytest.approx(float(later_txns), abs=0.01)
    assert "2024" not in early  # periods after the valuation date don't exist yet


def test_earned_premium_mid_year_term(tiny):
    tiny.execute("INSERT INTO policy VALUES (1, DATE '2024-07-01', 'DIRECT', 'NORTH')")
    tiny.execute("INSERT INTO policy_term VALUES (1, 1, 1, DATE '2024-06-20', DATE '2024-07-01', DATE '2025-07-01', "
                 "NULL, 'DIRECT', 'NORTH', 'C', '40-59', 3)")
    tiny.execute("INSERT INTO premium_txn VALUES (1, 1, 'NEW', DATE '2024-06-20', DATE '2024-07-01', "
                 "DATE '2025-07-01', 730, 1, 1)")
    r = _one(tiny, metrics=["earned_premium", "written_premium"], basis="calendar", as_of=AS_OF)
    assert r["2024"]["earned_premium"] == pytest.approx(368)
    assert r["2025"]["earned_premium"] == pytest.approx(362)
    assert r["2024"]["written_premium"] == pytest.approx(730)
    # Valued mid-way through 2025, only the days up to the valuation date have been earned.
    mid = _one(tiny, metrics=["earned_premium"], basis="calendar", as_of=date(2025, 3, 31))
    assert mid["2025"]["earned_premium"] == pytest.approx(730 * 90 / 365)


def test_ratio_of_sums_not_mean_of_ratios(con):
    yearly = _one(con, metrics=["loss_ratio_reported", "reported_loss", "earned_premium"], basis="accident",
                  as_of=AS_OF)["2024"]
    cols, rows = run(con, MetricQuery(metrics=["loss_ratio_reported", "reported_loss", "earned_premium"],
                                      basis="accident", as_of=AS_OF, grain="quarter",
                                      period_from=date(2024, 1, 1), period_to=date(2024, 12, 31)))
    quarters = [dict(zip(cols, r)) for r in rows]
    assert len(quarters) == 4
    assert yearly["loss_ratio_reported"] == pytest.approx(yearly["reported_loss"] / yearly["earned_premium"])
    assert yearly["loss_ratio_reported"] == pytest.approx(
        sum(q["reported_loss"] for q in quarters) / sum(q["earned_premium"] for q in quarters))
    mean = sum(q["loss_ratio_reported"] for q in quarters) / 4
    assert mean != pytest.approx(yearly["loss_ratio_reported"], rel=1e-6)


def test_non_conformed_dimension():
    c = Compiler()
    with pytest.raises(SemanticError, match="can't be split by 'peril'"):
        c.compile(MetricQuery(metrics=["loss_ratio_reported"], basis="accident", as_of=AS_OF, group_by=["peril"]))
    c.compile(MetricQuery(metrics=["reported_loss"], basis="accident", as_of=AS_OF, group_by=["peril"]))


def test_undefined_basis():
    c = Compiler()
    with pytest.raises(SemanticError, match="not defined on a accident basis"):
        c.compile(MetricQuery(metrics=["written_premium"], basis="accident", as_of=AS_OF))
    with pytest.raises(SemanticError, match="not defined on a policy basis"):
        c.compile(MetricQuery(metrics=["loss_ratio_ultimate"], basis="policy", as_of=AS_OF))


def test_overheads_not_silently_dropped_by_channel(con):
    _cols, rows = run(con, MetricQuery(metrics=["expenses"], basis="calendar", as_of=AS_OF, group_by=["channel"]))
    by_channel = sum(r[2] for r in rows if r[0] == "2024")
    total = _one(con, metrics=["expenses"], basis="calendar", as_of=AS_OF)["2024"]["expenses"]
    assert by_channel == pytest.approx(total)
    assert any(r[1] == "UNALLOCATED" for r in rows)
