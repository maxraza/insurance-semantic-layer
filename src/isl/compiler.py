"""Compile metric queries against the semantic model into DuckDB SQL.

The compiler knows four measure kinds (flow, earned, snapshot, valuation) and nothing
about insurance; the insurance knowledge lives in `semantic/motor.yml`. Every query must
name a time basis and a valuation date, so the choices that raw SQL lets an analyst (or
a model) skip are part of the request.
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Literal

import yaml

Basis = Literal["calendar", "accident", "policy"]
Grain = Literal["year", "quarter", "month"]
BASES: tuple[str, ...] = ("calendar", "accident", "policy")
GRAINS: tuple[str, ...] = ("year", "quarter", "month")
DATA_START = date(2019, 1, 1)
DEFAULT_MODEL = Path(__file__).resolve().parents[2] / "semantic" / "motor.yml"


class SemanticError(ValueError):
    """A query the model can't answer as asked. The message says why and what is allowed."""


@dataclass
class MetricQuery:
    metrics: list[str]
    basis: Basis
    as_of: date
    grain: Grain = "year"
    group_by: list[str] = field(default_factory=list)
    filters: dict[str, str | list[str]] = field(default_factory=dict)
    period_from: date | None = None
    period_to: date | None = None

    @classmethod
    def from_dict(cls, d: dict) -> MetricQuery:
        def as_date(v):
            return v if isinstance(v, date) or v is None else date.fromisoformat(str(v))

        return cls(metrics=list(d["metrics"]), basis=d.get("basis"), as_of=as_date(d.get("as_of")),
                   grain=d.get("grain", "year"), group_by=list(d.get("group_by") or []),
                   filters=dict(d.get("filters") or {}), period_from=as_date(d.get("period_from")),
                   period_to=as_date(d.get("period_to")))


class SemanticModel:
    def __init__(self, path: str | Path = DEFAULT_MODEL) -> None:
        raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
        self.raw = raw
        self.sources: dict = raw["sources"]
        self.measures: dict = raw["measures"]
        self.metrics: dict = raw["metrics"]
        for name, m in self.metrics.items():
            m["measures"] = sorted(_names(m["expr"]))
            unknown = set(m["measures"]) - set(self.measures)
            if unknown:
                raise SemanticError(f"metric {name} references unknown measures {sorted(unknown)}")

    # ------------------------------------------------------------------ introspection

    def measures_for(self, name: str) -> list[str]:
        if name in self.measures:
            return [name]
        if name in self.metrics:
            return self.metrics[name]["measures"]
        raise SemanticError(f"unknown metric or measure '{name}'. Known: {', '.join(self.names())}")

    def names(self) -> list[str]:
        return sorted(self.metrics) + sorted(self.measures)

    def dimensions(self, measure: str) -> dict[str, str]:
        return self.sources[self.measures[measure]["source"]]["dimensions"]

    def bases(self, name: str) -> list[str]:
        sets = [set(self.measures[m]["bases"]) for m in self.measures_for(name)]
        return [b for b in BASES if all(b in s for s in sets)]

    def conformed_dimensions(self, name: str) -> list[str]:
        sets = [set(self.dimensions(m)) for m in self.measures_for(name)]
        return sorted(set.intersection(*sets)) if sets else []

    def describe(self, name: str) -> str:
        item = self.metrics.get(name) or self.measures[name]
        return " ".join(str(item.get("description", "")).split())


def _names(expr: str) -> set[str]:
    return {n.id for n in ast.walk(ast.parse(expr, mode="eval")) if isinstance(n, ast.Name)}


# ---------------------------------------------------------------------- SQL helpers

def _lit(d: date) -> str:
    return f"DATE '{d.isoformat()}'"


def _interval(grain: str) -> str:
    return {"year": "INTERVAL 1 YEAR", "quarter": "INTERVAL 3 MONTH", "month": "INTERVAL 1 MONTH"}[grain]


def _fill(sql: str, **kw: str) -> str:
    return re.sub(r"\{(\w+)\}", lambda m: kw[m.group(1)] if m.group(1) in kw else m.group(0), sql)


def _quote(v: str) -> str:
    return "'" + str(v).replace("'", "''") + "'"


def _metric_sql(expr: str) -> str:
    """Rewrite `a / (b + c)` as SQL with NULL-safe division and zero-filled measures."""
    tree = ast.parse(expr, mode="eval").body

    def walk(n: ast.AST) -> str:
        if isinstance(n, ast.Name):
            return f"COALESCE(m.{n.id}, 0)"
        if isinstance(n, ast.Constant):
            return repr(n.value)
        if isinstance(n, ast.BinOp):
            left, right = walk(n.left), walk(n.right)
            if isinstance(n.op, ast.Div):
                return f"({left}) / NULLIF({right}, 0)"
            op = {ast.Add: "+", ast.Sub: "-", ast.Mult: "*"}[type(n.op)]
            return f"({left} {op} {right})"
        raise SemanticError(f"unsupported metric expression: {ast.unparse(n)}")

    return walk(tree)


# ---------------------------------------------------------------------- compiler

class Compiler:
    def __init__(self, model: SemanticModel | None = None) -> None:
        self.model = model or SemanticModel()

    def validate(self, q: MetricQuery) -> list[str]:
        """Checks the query and returns the measures it needs."""
        if not q.metrics:
            raise SemanticError("at least one metric is required")
        if q.basis not in BASES:
            raise SemanticError(f"basis is required: one of {', '.join(BASES)} (got {q.basis!r}). "
                                "Calendar = accounting period; accident = when the loss happened; "
                                "policy = when the policy incepted.")
        if not isinstance(q.as_of, date):
            raise SemanticError("as_of (valuation date) is required: facts known after it are excluded")
        if q.grain not in GRAINS:
            raise SemanticError(f"grain must be one of {', '.join(GRAINS)}")
        measures: list[str] = []
        for name in q.metrics:
            needed = self.model.measures_for(name)
            if q.basis not in self.model.bases(name):
                raise SemanticError(f"'{name}' is not defined on a {q.basis} basis. "
                                    f"Allowed: {', '.join(self.model.bases(name)) or 'none'}")
            dims = self.model.conformed_dimensions(name)
            for d in [*q.group_by, *q.filters]:
                if d not in dims:
                    raise SemanticError(f"'{name}' can't be split by '{d}' (not available on every measure it "
                                        f"uses). Allowed: {', '.join(dims) or 'none'}")
            measures += [m for m in needed if m not in measures]
        for m in measures:
            spec = self.model.measures[m]["bases"][q.basis]
            if spec["kind"] == "valuation" and q.basis == "accident" and q.grain != "year":
                raise SemanticError(f"'{m}' is booked by accident year; use grain=year on an accident basis")
        return measures

    def compile(self, q: MetricQuery) -> str:
        measures = self.validate(q)
        dims = list(q.group_by)
        last = q.period_to or q.as_of
        ctx = dict(as_of=_lit(q.as_of), grain=q.grain)
        ctes = [self._grid(q.grain, last)]
        for m in measures:
            ctes.append(f"{m} AS (\n{self._measure(m, q, dims, ctx)}\n)")

        key_cols = ", ".join(["period", *dims])
        keys = "\n  UNION\n  ".join(f"SELECT {key_cols} FROM {m}" for m in measures)
        ctes.append(f"keys AS (\n  {keys}\n)")
        joins = "\n".join(f"LEFT JOIN {m} USING ({key_cols})" for m in measures)
        measure_cols = ",\n    ".join(f"{m}.value::DOUBLE AS {m}" for m in measures)
        ctes.append(f"m AS (\n  SELECT keys.*,\n    {measure_cols}\n  FROM keys\n{joins}\n)")

        where = [f"period <= {_lit(last)}"]
        if q.period_from:
            where.append(f"period >= date_trunc('{q.grain}', {_lit(q.period_from)})")
        outputs = []
        for name in q.metrics:
            expr = name if name in self.model.measures else self.model.metrics[name]["expr"]
            outputs.append(f"{_metric_sql(expr)} AS {name}")
        select = ",\n  ".join([_period_label(q.grain), *[f"m.{d}" for d in dims], *outputs])
        order = ", ".join(["period", *dims])
        return ("WITH " + ",\n".join(ctes)
                + f"\nSELECT\n  {select}\nFROM m\nWHERE {' AND '.join(where)}\nORDER BY {order}")

    # ------------------------------------------------------------------ pieces

    def _grid(self, grain: str, last: date) -> str:
        return (f"grid AS (\n  SELECT p_start::DATE AS period, (p_start + {_interval(grain)})::DATE AS p_end\n"
                f"  FROM range(date_trunc('{grain}', {_lit(DATA_START)}), {_lit(last)} + INTERVAL 1 DAY, "
                f"{_interval(grain)}) t(p_start)\n)")

    def _measure(self, name: str, q: MetricQuery, dims: list[str], ctx: dict) -> str:
        meas = self.model.measures[name]
        src = self.model.sources[meas["source"]]
        spec = meas["bases"][q.basis]
        dim_map = src["dimensions"]
        dim_sel = "".join(f", {dim_map[d]} AS {d}" for d in dims)
        filt = []
        for d, v in q.filters.items():
            vals = v if isinstance(v, list) else [v]
            filt.append(f"{dim_map[d]} IN ({', '.join(_quote(x) for x in vals)})")
        where = [w for w in (meas.get("where"), spec.get("where")) if w] + filt
        known = f"{src['knowledge_date']} <= {ctx['as_of']}"
        agg = {"count": "COUNT(*)", "count_distinct": f"COUNT(DISTINCT {meas.get('key')})"}.get(
            meas.get("agg", "sum"), f"SUM({meas.get('amount')})")
        kind = spec["kind"]
        g = q.grain

        if kind == "flow":
            w = " AND ".join([known, *where])
            return (f"  SELECT date_trunc('{g}', {spec['date']})::DATE AS period{dim_sel}, {agg} AS value\n"
                    f"  FROM {src['from']}\n  WHERE {_fill(w, **ctx)}\n  GROUP BY ALL")

        if kind == "earned":
            start, end = spec["start"], spec["end"]
            share = f"date_diff('day', {start}, {end})"
            if "cohort" in spec:  # policy basis: earned to as_of, assigned to the inception period
                days = f"date_diff('day', {start}, LEAST({end}, {ctx['as_of']} + 1))"
                w = " AND ".join([known, f"{start} <= {ctx['as_of']}", *where])
                return (f"  SELECT date_trunc('{g}', {spec['cohort']})::DATE AS period{dim_sel}, "
                        f"SUM({meas['amount']} * {days} / {share}) AS value\n"
                        f"  FROM {src['from']}\n  WHERE {_fill(w, **ctx)}\n  GROUP BY ALL")
            lo = f"GREATEST({start}, g.period)"
            hi = f"LEAST({end}, g.p_end, {ctx['as_of']} + 1)"
            w = " AND ".join([known, f"{start} < g.p_end", f"{end} > g.period",
                              f"g.period <= {ctx['as_of']}", *where])
            return (f"  SELECT g.period{dim_sel}, "
                    f"SUM({meas['amount']} * GREATEST(date_diff('day', {lo}, {hi}), 0) / {share}) AS value\n"
                    f"  FROM grid g JOIN {src['from']} ON {_fill(w, **ctx)}\n  GROUP BY ALL")

        if kind == "snapshot":
            d = f"LEAST(g.p_end - 1, {ctx['as_of']})"
            w = " AND ".join([known, f"g.period <= {ctx['as_of']}", *where])
            amount = _fill(meas.get("amount", "1"), d=d)
            value = agg if meas.get("agg") else f"SUM({amount})"
            return (f"  SELECT g.period{dim_sel}, {value} AS value\n"
                    f"  FROM grid g JOIN {src['from']} ON {_fill(w, d=d, **ctx)}\n  GROUP BY ALL")

        if kind == "valuation":
            totals = (f"SELECT {src['knowledge_date']} AS vdate, SUM({meas['amount']}) AS total "
                      f"FROM {src['from']} GROUP BY 1")
            if spec.get("change"):
                # Change in booked IBNR between the end of the previous period and the end of this one.
                return (f"  SELECT g.period, COALESCE(e.total, 0) - COALESCE(b.total, 0) AS value\n"
                        f"  FROM (SELECT period, LEAST(p_end - 1, {ctx['as_of']}) AS d_end, period - 1 AS d_begin "
                        f"FROM grid WHERE period <= {ctx['as_of']}) g\n"
                        f"  ASOF LEFT JOIN ({totals}) e ON g.d_end >= e.vdate\n"
                        f"  ASOF LEFT JOIN ({totals}) b ON g.d_begin >= b.vdate")
            latest = (f"(SELECT max({src['knowledge_date']}) FROM {src['from']} "
                      f"WHERE {src['knowledge_date']} <= {ctx['as_of']})")
            return (f"  SELECT make_date({spec['cohort_year']}, 1, 1) AS period, SUM({meas['amount']}) AS value\n"
                    f"  FROM {src['from']}\n  WHERE {src['knowledge_date']} = {latest}\n  GROUP BY ALL")

        raise SemanticError(f"unknown measure kind {kind}")


def _period_label(grain: str) -> str:
    if grain == "year":
        return "CAST(year(m.period) AS VARCHAR) AS period"
    if grain == "quarter":
        return "year(m.period) || '-Q' || quarter(m.period) AS period"
    return "strftime(m.period, '%Y-%m') AS period"


def run(con, q: MetricQuery | dict, compiler: Compiler | None = None):
    """Compile and execute; returns (columns, rows)."""
    compiler = compiler or Compiler()
    q = q if isinstance(q, MetricQuery) else MetricQuery.from_dict(q)
    rel = con.execute(compiler.compile(q))
    return [d[0] for d in rel.description], rel.fetchall()
