"""Compiler vs independent Python reference on random queries."""

import random
from datetime import date

import pytest

from isl.compiler import Compiler, MetricQuery, SemanticError, run
from isl.reference import Reference, period_start

GRAINS = ["year", "quarter"]
AS_OFS = [date(2021, 6, 30), date(2023, 12, 31), date(2025, 12, 31)]


@pytest.fixture(scope="module")
def ref(con):
    return Reference(con)


def _queries(n=60, seed=5):
    compiler = Compiler()
    model = compiler.model
    rng = random.Random(seed)
    names = sorted(model.measures) + sorted(model.metrics)
    out = []
    while len(out) < n:
        name = rng.choice(names)
        bases = model.bases(name)
        dims = model.conformed_dimensions(name)
        q = MetricQuery(metrics=[name], basis=rng.choice(bases), as_of=rng.choice(AS_OFS),
                        grain=rng.choice(GRAINS))
        if dims and rng.random() < 0.5:
            q.group_by = [rng.choice(dims)]
        if dims and rng.random() < 0.3:
            d = rng.choice(dims)
            q.filters = {d: {"region": "LONDON", "channel": "PCW", "vehicle_group": "C", "driver_age_band": "17-24",
                             "business_type": "RENEWAL", "peril": "OWN_DAMAGE", "expense_type": "ADMIN"}[d]}
        try:
            compiler.validate(q)
        except SemanticError:
            continue
        out.append(q)
    return out


def _exhaustive():
    """Every measure and metric on every basis it allows, valued mid-period so as_of clipping matters."""
    model = Compiler().model
    out = []
    for name in sorted(model.measures) + sorted(model.metrics):
        for basis in model.bases(name):
            for grain, as_of in (("year", date(2023, 6, 30)), ("quarter", date(2025, 11, 15))):
                if grain == "quarter" and basis == "accident" and "ibnr" in model.measures_for(name):
                    continue
                out.append(MetricQuery(metrics=[name], basis=basis, as_of=as_of, grain=grain))
    return out


@pytest.mark.parametrize("q", _exhaustive() + _queries(), ids=lambda q: f"{q.metrics[0]}-{q.basis}-{q.grain}-{q.as_of}"
                         f"-{'+'.join(q.group_by)}-{'+'.join(q.filters)}")
def test_compiler_matches_reference(con, ref, q):
    _cols, rows = run(con, q)
    got = {}
    for r in rows:
        got[(r[0], *r[1:1 + len(q.group_by)])] = r[-1]
    expected = {}
    for k, v in ref.metric(q.metrics[0], q).items():
        p = k[0]
        label = {"year": f"{p.year}", "quarter": f"{p.year}-Q{(p.month - 1) // 3 + 1}"}[q.grain]
        if period_start(p, q.grain) == p:
            expected[(label, *k[1:])] = v
    for k in set(got) | set(expected):
        a, b = got.get(k), expected.get(k)
        if a is None or b is None:
            # A key present on one side only must be zero/undefined on the other.
            assert (a or 0) == pytest.approx(b or 0, abs=1e-6), k
            continue
        assert a == pytest.approx(b, rel=1e-9, abs=1e-6), k
