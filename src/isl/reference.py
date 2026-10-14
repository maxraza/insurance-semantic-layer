"""Independent reference implementation of every measure, in plain Python.

It reads the raw tables and applies the definitions directly, with no SQL and no shared
code with the compiler beyond the query type. Tests compare the two on random queries;
a disagreement means one of them has the semantics wrong.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date, timedelta

from .compiler import MetricQuery, SemanticModel

Key = tuple


def period_start(d: date, grain: str) -> date:
    if grain == "year":
        return date(d.year, 1, 1)
    if grain == "quarter":
        return date(d.year, 3 * ((d.month - 1) // 3) + 1, 1)
    return date(d.year, d.month, 1)


def next_period(p: date, grain: str) -> date:
    months = {"year": 12, "quarter": 3, "month": 1}[grain]
    m = p.month - 1 + months
    return date(p.year + m // 12, m % 12 + 1, 1)


def periods(grain: str, last: date) -> list[tuple[date, date]]:
    out, p = [], period_start(date(2019, 1, 1), grain)
    while p <= last:
        out.append((p, next_period(p, grain)))
        p = next_period(p, grain)
    return out


class Reference:
    def __init__(self, con) -> None:
        def rows(sql: str) -> list[dict]:
            rel = con.execute(sql)
            cols = [c[0] for c in rel.description]
            return [dict(zip(cols, r)) for r in rel.fetchall()]

        self.terms = {r["term_id"]: r for r in rows("SELECT * FROM policy_term")}
        self.premium = rows("SELECT * FROM premium_txn")
        self.claims = {r["claim_id"]: r for r in rows("SELECT * FROM claim")}
        self.claim_txn = rows("SELECT * FROM claim_txn")
        self.ibnr = rows("SELECT * FROM ibnr_valuation")
        self.expense = rows("SELECT * FROM expense")
        self.quotes = rows("SELECT * FROM quote")
        self.offers = rows("SELECT * FROM renewal_offer")
        self.model = SemanticModel()

    # ------------------------------------------------------------------ dimensions

    @staticmethod
    def _term_dims(t: dict) -> dict:
        return dict(region=t["region"], channel=t["channel"], vehicle_group=t["vehicle_group"],
                    driver_age_band=t["driver_age_band"],
                    business_type="NEW" if t["term_number"] == 1 else "RENEWAL")

    def _keep(self, dims: dict, q: MetricQuery) -> Key | None:
        for d, v in q.filters.items():
            vals = v if isinstance(v, list) else [v]
            if dims.get(d) not in vals:
                return None
        return tuple(dims[d] for d in q.group_by)

    # ------------------------------------------------------------------ measures

    def measure(self, name: str, q: MetricQuery) -> dict[tuple, float]:
        """Returns {(period, *group_values): value}."""
        out: dict[tuple, float] = defaultdict(float)
        g, a, b = q.grain, q.as_of, q.basis
        last = q.period_to or a
        grid = [(s, e) for s, e in periods(g, last) if s <= a]

        def add(d: date, dims: dict, v: float) -> None:
            k = self._keep(dims, q)
            if k is not None:
                out[(period_start(d, g), *k)] += float(v)

        if name in ("written_premium", "written_exposure"):
            col = "written_premium" if name == "written_premium" else "exposure_units"
            for p in self.premium:
                if p["booked_date"] <= a:
                    t = self.terms[p["term_id"]]
                    add(p["booked_date"] if b == "calendar" else t["inception_date"], self._term_dims(t), p[col])

        elif name in ("earned_premium", "earned_exposure"):
            col = "written_premium" if name == "earned_premium" else "exposure_units"
            for p in self.premium:
                if p["booked_date"] > a:
                    continue
                t = self.terms[p["term_id"]]
                s, e = p["effective_date"], p["cover_end_date"]
                span = (e - s).days
                if b == "policy":
                    if s <= a:
                        add(t["inception_date"], self._term_dims(t),
                            float(p[col]) * (min(e, a + timedelta(days=1)) - s).days / span)
                    continue
                for ps, pe in grid:
                    days = (min(e, pe, a + timedelta(days=1)) - max(s, ps)).days
                    if days > 0:
                        add(ps, self._term_dims(t), float(p[col]) * days / span)

        elif name in ("in_force_count", "unearned_premium"):
            for ps, pe in grid:
                d = min(pe - timedelta(days=1), a)
                for p in self.premium:
                    if p["booked_date"] > d:
                        continue
                    t = self.terms[p["term_id"]]
                    s, e = p["effective_date"], p["cover_end_date"]
                    if name == "in_force_count" and s <= d < e:
                        add(ps, self._term_dims(t), p["vehicle_delta"])
                    if name == "unearned_premium" and e > d + timedelta(days=1):
                        days = (e - max(s, d + timedelta(days=1))).days
                        add(ps, self._term_dims(t), float(p["written_premium"]) * days / (e - s).days)

        elif name in ("paid_loss", "reported_loss", "paid_alae", "case_reserve", "paid_claim_count"):
            seen: set = set()
            snap = name == "case_reserve" and b == "calendar"
            for x in self.claim_txn:
                if x["txn_date"] > a:
                    continue
                kind, ctype = x["txn_type"], x["cost_type"]
                ok = {"paid_loss": kind in ("PAYMENT", "RECOVERY") and ctype == "INDEMNITY",
                      "reported_loss": ctype == "INDEMNITY",
                      "paid_alae": ctype == "ALAE",
                      "case_reserve": kind == "RESERVE",
                      "paid_claim_count": kind == "PAYMENT" and ctype == "INDEMNITY"}[name]
                if not ok:
                    continue
                c = self.claims[x["claim_id"]]
                t = self.terms[c["term_id"]]
                dims = {**self._term_dims(t), "peril": c["peril"]}
                if snap:
                    for ps, pe in grid:
                        if x["txn_date"] <= min(pe - timedelta(days=1), a):
                            add(ps, dims, x["amount"])
                    continue
                when = {"calendar": x["txn_date"], "accident": c["accident_date"], "policy": t["inception_date"]}[b]
                if name == "paid_claim_count":
                    key = (period_start(when, g), x["claim_id"])
                    if key in seen:
                        continue
                    seen.add(key)
                    add(when, dims, 1)
                else:
                    add(when, dims, x["amount"])

        elif name in ("reported_claim_count", "open_claim_count"):
            for c in self.claims.values():
                t = self.terms[c["term_id"]]
                dims = {**self._term_dims(t), "peril": c["peril"]}
                if name == "open_claim_count":
                    for ps, pe in grid:
                        d = min(pe - timedelta(days=1), a)
                        if c["report_date"] <= d and (c["closed_date"] is None or c["closed_date"] > d):
                            add(ps, dims, 1)
                elif c["report_date"] <= a:
                    add({"calendar": c["report_date"], "accident": c["accident_date"],
                         "policy": t["inception_date"]}[b], dims, 1)

        elif name == "ibnr":
            def total_at(d: date) -> float:
                vs = [v["valuation_date"] for v in self.ibnr if v["valuation_date"] <= d]
                return sum(float(v["ibnr"]) for v in self.ibnr if vs and v["valuation_date"] == max(vs))

            if b == "accident":
                vs = [v["valuation_date"] for v in self.ibnr if v["valuation_date"] <= a]
                for v in self.ibnr:
                    if vs and v["valuation_date"] == max(vs):
                        out[(date(v["accident_year"], 1, 1),)] += float(v["ibnr"])
            else:
                for ps, pe in grid:
                    out[(ps,)] += total_at(min(pe - timedelta(days=1), a)) - total_at(ps - timedelta(days=1))

        elif name == "expenses":
            for e in self.expense:
                if e["booked_date"] <= a:
                    add(e["expense_month"], dict(channel=e["channel"] or "UNALLOCATED", expense_type=e["expense_type"]), e["amount"])

        elif name in ("quotes", "bound_quotes"):
            for r in self.quotes:
                if r["quote_date"] <= a and (name == "quotes" or r["bound_term_id"] is not None):
                    add(r["quote_date"], {k: r[k] for k in ("region", "channel", "vehicle_group", "driver_age_band")}, 1)

        elif name in ("renewals_due", "renewals_accepted"):
            for o in self.offers:
                if o["offer_date"] > a or o["due_date"] > a:
                    continue
                if name == "renewals_accepted":
                    rt = self.terms.get(o["renewed_term_id"])
                    if rt is None or rt["bound_date"] > a:
                        continue
                add(o["due_date"], self._term_dims(self.terms[o["expiring_term_id"]]), 1)

        else:
            raise KeyError(name)

        return {k: v for k, v in out.items() if k[0] <= last and (q.period_from is None or
                                                                  k[0] >= period_start(q.period_from, g))}

    def metric(self, name: str, q: MetricQuery) -> dict[tuple, float | None]:
        if name in self.model.measures:
            return self.measure(name, q)
        expr = self.model.metrics[name]["expr"]
        parts = {m: self.measure(m, q) for m in self.model.metrics[name]["measures"]}
        keys = set().union(*parts.values())
        out = {}
        for k in keys:
            env = {m: parts[m].get(k, 0.0) for m in parts}
            try:
                out[k] = eval(expr, {"__builtins__": {}}, env)  # expr comes from the model file
            except ZeroDivisionError:
                out[k] = None
        return out
