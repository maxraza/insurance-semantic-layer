"""Synthetic personal-motor book, generated forward in time.

Quotes convert into terms, terms renew or lapse, cancel or change mid-term, and
suffer claims that are reported late, reserved, paid, re-reserved and closed. At
every quarter-end a reserving actuary books IBNR from the data known on that date.
Facts dated after the horizon are dropped from the published tables, so "as of"
slicing is genuine. The final cost of every accident up to the horizon (reported
or not) is kept in `truth_claim_cost` for evaluating estimates; it is never shown
to a model under test.

Everything is synthetic and seeded. No real policyholder, insurer or employer data.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path

import duckdb
import numpy as np
import pyarrow as pa

CHANNELS = ["PCW", "DIRECT", "BROKER"]
REGIONS = ["NORTH", "MIDLANDS", "LONDON", "SOUTH", "SCOTLAND"]
VEHICLE_GROUPS = ["A", "B", "C", "D", "E"]
AGE_BANDS = ["17-24", "25-39", "40-59", "60+"]
PERILS = ["OWN_DAMAGE", "THIRD_PARTY_PROPERTY", "BODILY_INJURY", "WINDSCREEN", "THEFT"]

# Relativities (multiplicative), used both for rating and for true risk.
REGION_REL = {"NORTH": 1.00, "MIDLANDS": 1.05, "LONDON": 1.35, "SOUTH": 0.95, "SCOTLAND": 0.85}
VEHICLE_REL = {"A": 0.75, "B": 0.90, "C": 1.00, "D": 1.20, "E": 1.50}
AGE_REL = {"17-24": 2.40, "25-39": 1.10, "40-59": 0.85, "60+": 0.95}
CHANNEL_MIX = [0.68, 0.20, 0.12]
REGION_MIX = [0.22, 0.20, 0.18, 0.28, 0.12]
VEHICLE_MIX = [0.18, 0.27, 0.27, 0.18, 0.10]
AGE_MIX = [0.10, 0.36, 0.38, 0.16]

# Claim frequency per car-year at base relativities; severity (mean, lognormal sigma) at start-year prices.
FREQ = {"OWN_DAMAGE": 0.050, "THIRD_PARTY_PROPERTY": 0.035, "BODILY_INJURY": 0.010,
        "WINDSCREEN": 0.030, "THEFT": 0.005}
SEVERITY = {"OWN_DAMAGE": (2600, 0.9), "THIRD_PARTY_PROPERTY": (3100, 1.0), "BODILY_INJURY": (21000, 1.3),
            "WINDSCREEN": (380, 0.4), "THEFT": (6200, 0.7)}
# Mean report lag and mean settlement time after report, in days.
REPORT_LAG = {"OWN_DAMAGE": 6, "THIRD_PARTY_PROPERTY": 25, "BODILY_INJURY": 140, "WINDSCREEN": 3, "THEFT": 4}
SETTLE = {"OWN_DAMAGE": 55, "THIRD_PARTY_PROPERTY": 150, "BODILY_INJURY": 700, "WINDSCREEN": 10, "THEFT": 80}
NIL_RATE = {"OWN_DAMAGE": 0.12, "THIRD_PARTY_PROPERTY": 0.18, "BODILY_INJURY": 0.20, "WINDSCREEN": 0.03,
            "THEFT": 0.25}
# Mean initial case-reserve adequacy (reserve / true cost). Bodily injury starts under-reserved.
RESERVE_ADEQUACY = {"OWN_DAMAGE": 1.05, "THIRD_PARTY_PROPERTY": 0.95, "BODILY_INJURY": 0.70,
                    "WINDSCREEN": 1.00, "THEFT": 1.00}

TABLES = ["quote", "policy", "policy_term", "premium_txn", "renewal_offer", "claim", "claim_txn",
          "ibnr_valuation", "expense", "truth_claim_cost"]


@dataclass
class Config:
    seed: int = 7
    start: date = date(2019, 1, 1)
    horizon: date = date(2025, 12, 31)          # last day the insurer has data for
    quotes_per_month: int = 9000
    base_premium: float = 520.0
    claims_inflation: dict[int, float] = field(default_factory=lambda: {
        2019: 0.03, 2020: 0.02, 2021: 0.04, 2022: 0.11, 2023: 0.12, 2024: 0.06, 2025: 0.04, 2026: 0.04})
    rate_changes: dict[int, float] = field(default_factory=lambda: {
        2019: 0.02, 2020: -0.03, 2021: 0.00, 2022: 0.06, 2023: 0.18, 2024: 0.04, 2025: -0.04, 2026: 0.0})
    cancel_rate: float = 0.07
    mta_rate: float = 0.12
    renewal_base: float = 0.80                   # renewal probability at zero price change, non-PCW
    renewal_elasticity: float = 6.4              # drop in renewal logit per +100% price change
    tenure_margin: float = 0.05                  # pre-reform "price walking" per completed term
    pricing_reform: date = date(2022, 1, 1)      # renewals capped at the equivalent new-business price


def _index_at(rates: dict[int, float], start_year: int, d: date) -> float:
    """Compounded index from 1 Jan of start_year to d."""
    f = 1.0
    for y in range(start_year, d.year):
        f *= 1.0 + rates.get(y, 0.0)
    return f * (1.0 + rates.get(d.year, 0.0)) ** ((d.timetuple().tm_yday - 1) / 365.0)


def _add_year(d: date) -> date:
    try:
        return d.replace(year=d.year + 1)
    except ValueError:  # 29 Feb
        return d.replace(year=d.year + 1, day=28)


def _month(d: date) -> date:
    return d.replace(day=1)


def _next_month(d: date) -> date:
    return (d.replace(day=28) + timedelta(days=4)).replace(day=1)


class Book:
    def __init__(self) -> None:
        self.rows: dict[str, list[tuple]] = {k: [] for k in TABLES}
        self.ids: dict[str, int] = {}

    def next_id(self, table: str) -> int:
        self.ids[table] = self.ids.get(table, 0) + 1
        return self.ids[table]


def generate(cfg: Config | None = None) -> dict[str, list[tuple]]:
    cfg = cfg or Config()
    rng = np.random.default_rng(cfg.seed)
    book = Book()
    queue = _quotes(cfg, rng, book)
    truth: list[tuple] = []
    i = 0
    while i < len(queue):  # renewals are appended while we iterate
        _term(cfg, rng, book, queue[i], truth, queue)
        i += 1
    _expenses(cfg, rng, book)
    tables = _cut_at_horizon(cfg, book)
    tables["ibnr_valuation"] = _ibnr_valuations(cfg, tables)
    tables["truth_claim_cost"] = [(c, a.year, p, v) for c, a, p, v in truth if a <= cfg.horizon]
    return tables


# --------------------------------------------------------------------------- quotes → new business

def _quotes(cfg: Config, rng, book: Book) -> list[dict]:
    queue: list[dict] = []
    month = cfg.start
    while month <= cfg.horizon:
        nxt = _next_month(month)
        season = 1.0 + 0.18 * math.cos(2 * math.pi * (month.month - 1) / 12)  # January peak
        growth = 1.0 + 0.06 * (month.year - cfg.start.year)
        n = int(rng.poisson(cfg.quotes_per_month * season * growth))
        rate_idx = _index_at(cfg.rate_changes, cfg.start.year, month)
        qdays = np.sort(rng.integers(0, (nxt - month).days, size=n))
        ch_i = rng.choice(3, p=CHANNEL_MIX, size=n)
        rg_i = rng.choice(5, p=REGION_MIX, size=n)
        vg_i = rng.choice(5, p=VEHICLE_MIX, size=n)
        ab_i = rng.choice(4, p=AGE_MIX, size=n)
        noise = rng.lognormal(0, 0.12, size=n)
        rivals = rng.lognormal(0.04, 0.14, size=(n, 9))
        u_bind = rng.random(n)
        lead = rng.integers(0, 15, size=n)
        ncd = rng.integers(0, 9, size=n)
        for j in range(n):
            qd = month + timedelta(days=int(qdays[j]))
            if qd > cfg.horizon:
                break
            ch, rg, vg, ab = CHANNELS[ch_i[j]], REGIONS[rg_i[j]], VEHICLE_GROUPS[vg_i[j]], AGE_BANDS[ab_i[j]]
            tech = cfg.base_premium * REGION_REL[rg] * VEHICLE_REL[vg] * AGE_REL[ab] * rate_idx
            premium = round(tech * float(noise[j]), 2)
            rank = None
            if ch == "PCW":
                # Rivals price around our technical price; rank = 1 + number of cheaper rivals.
                rank = 1 + int((rivals[j] * tech < premium).sum())
                p_bind = 0.24 * math.exp(-0.55 * (rank - 1))
            else:
                p_bind = 0.22 if ch == "DIRECT" else 0.30
            qid = book.next_id("quote")
            term_id = None
            if u_bind[j] < p_bind:
                inception = qd + timedelta(days=int(lead[j]))
                pid = book.next_id("policy")
                book.rows["policy"].append((pid, inception, ch, rg))
                term_id = book.next_id("policy_term")
                queue.append(dict(term_id=term_id, policy_id=pid, term_number=1, bound=qd, inception=inception,
                                  channel=ch, region=rg, vehicle=vg, age=ab, ncd=int(ncd[j]), premium=premium,
                                  txn_type="NEW"))
            book.rows["quote"].append((qid, qd, ch, rg, vg, ab, premium, rank, term_id))
        month = nxt
    return queue


# --------------------------------------------------------------------------- terms

def _term(cfg: Config, rng, book: Book, t: dict, truth: list, queue: list) -> None:
    tid, inception = t["term_id"], t["inception"]
    expiry = _add_year(inception)
    term_days = (expiry - inception).days
    premium = t["premium"]

    cancel_date = None
    if rng.random() < cfg.cancel_rate:
        cancel_date = inception + timedelta(days=int(rng.integers(14, term_days - 7)))
    book.rows["policy_term"].append((tid, t["policy_id"], t["term_number"], t["bound"], inception, expiry,
                                     cancel_date, t["channel"], t["region"], t["vehicle"], t["age"], t["ncd"]))

    ptx = [(book.next_id("premium_txn"), tid, t["txn_type"], t["bound"], inception, expiry, premium, 1.0, 1)]
    cover_end = cancel_date or expiry
    if rng.random() < cfg.mta_rate and (cover_end - inception).days > 40:
        eff = inception + timedelta(days=int(rng.integers(20, (cover_end - inception).days - 10)))
        annual = premium * float(rng.choice([-0.12, -0.06, 0.08, 0.15, 0.25]))
        ptx.append((book.next_id("premium_txn"), tid, "MTA", eff + timedelta(days=int(rng.integers(0, 4))),
                    eff, expiry, round(annual * (expiry - eff).days / term_days, 2), 0.0, 0))
    if cancel_date:
        # Return the unearned part of everything written (all cover ends at expiry), pro rata.
        unearned = sum(r[6] * (expiry - cancel_date).days / (expiry - r[4]).days for r in ptx)
        ptx.append((book.next_id("premium_txn"), tid, "CANCEL", cancel_date + timedelta(days=int(rng.integers(0, 6))),
                    cancel_date, expiry, -round(unearned, 2),
                    -round((expiry - cancel_date).days / term_days, 6), -1))
    book.rows["premium_txn"].extend(ptx)
    if inception > cfg.horizon:
        return  # bound before the horizon but not yet on cover: nothing further to simulate

    # Claims, Poisson over the covered days.
    had_claim = False
    risk = REGION_REL[t["region"]] * VEHICLE_REL[t["vehicle"]] * AGE_REL[t["age"]] * (1 - 0.03 * t["ncd"])
    covered = (cover_end - inception).days
    for peril in PERILS:
        rel = 1.0 if peril == "WINDSCREEN" else risk  # glass is driver-independent
        for _ in range(int(rng.poisson(FREQ[peril] * rel * covered / 365.0))):
            acc = inception + timedelta(days=int(rng.integers(0, covered)))
            had_claim |= peril != "WINDSCREEN"
            _claim(cfg, rng, book, tid, peril, acc, truth)

    # Renewal offer three weeks before expiry; price elasticity drives the decision.
    if cancel_date is None:
        # ENBP: what a new customer with the same risk and claims history would be quoted today.
        tech = (cfg.base_premium * REGION_REL[t["region"]] * VEHICLE_REL[t["vehicle"]] * AGE_REL[t["age"]]
                * _index_at(cfg.rate_changes, cfg.start.year, expiry) * (1.25 if had_claim else 1.0))
        enbp = round(tech * float(rng.lognormal(0, 0.05)), 2)
        offered = round(enbp * (1 + cfg.tenure_margin * t["term_number"]) * float(rng.lognormal(0, 0.03)), 2)
        if expiry >= cfg.pricing_reform:
            offered = min(offered, enbp)
        change = offered / premium - 1
        logit = (math.log(cfg.renewal_base / (1 - cfg.renewal_base)) - cfg.renewal_elasticity * max(change, -0.1)
                 - (0.45 if t["channel"] == "PCW" else 0.0))
        renews = rng.random() < 1 / (1 + math.exp(-logit))
        new_tid = book.next_id("policy_term") if renews else None
        book.rows["renewal_offer"].append((book.next_id("renewal_offer"), tid, expiry - timedelta(days=21),
                                           expiry, premium, offered, enbp, new_tid))
        if renews:
            age = "25-39" if t["age"] == "17-24" and rng.random() < 0.15 else t["age"]
            queue.append(dict(term_id=new_tid, policy_id=t["policy_id"], term_number=t["term_number"] + 1,
                              bound=expiry - timedelta(days=int(rng.integers(1, 21))), inception=expiry,
                              channel=t["channel"], region=t["region"], vehicle=t["vehicle"], age=age,
                              ncd=0 if had_claim else min(t["ncd"] + 1, 9), premium=offered, txn_type="RENEWAL"))


def _claim(cfg: Config, rng, book: Book, tid: int, peril: str, acc: date, truth: list) -> None:
    cid = book.next_id("claim")
    report = acc + timedelta(days=int(rng.exponential(REPORT_LAG[peril])))
    infl = _index_at(cfg.claims_inflation, cfg.start.year, acc)
    mean, sigma = SEVERITY[peril]
    nil = rng.random() < NIL_RATE[peril]
    cost = 0.0 if nil else round(float(rng.lognormal(math.log(mean) - sigma**2 / 2, sigma)) * infl, 2)
    alae = (round(cost * float(rng.uniform(0.05, 0.15)), 2)
            if cost and peril in ("BODILY_INJURY", "THIRD_PARTY_PROPERTY") else 0.0)
    settle_days = max(3, int(rng.gamma(2.0, SETTLE[peril] / 2.0)))
    closed = report + timedelta(days=settle_days)
    txns: list[tuple] = []

    def add(d: date, kind: str, ctype: str, amt: float) -> None:
        if abs(amt) >= 0.005:
            txns.append((d, kind, ctype, round(amt, 2)))

    # Initial case reserve on report; nil claims still get a precautionary reserve.
    est = mean * infl if nil else cost * float(rng.lognormal(math.log(RESERVE_ADEQUACY[peril]), 0.25))
    reserve = round(est, 2)
    add(report, "RESERVE", "INDEMNITY", reserve)
    if nil:
        n_pay = 0
    elif peril in ("WINDSCREEN", "THEFT"):
        n_pay = 1
    else:
        n_pay = int(rng.integers(1, 6 if peril == "BODILY_INJURY" else 4))
    pay_days = sorted(int(x) for x in rng.uniform(0.15, 1.0, size=n_pay) * settle_days)
    if n_pay:
        pay_days[-1] = settle_days
    shares = rng.dirichlet(np.ones(n_pay)) if n_pay else []
    paid = 0.0
    for k, (dd, share) in enumerate(zip(pay_days, shares)):
        d = report + timedelta(days=dd)
        if n_pay > 1 and k == n_pay // 2:
            # Mid-life review: re-estimate the outstanding cost (this is where BI gets strengthened).
            target = round((cost - paid) * float(rng.lognormal(0, 0.1)), 2)
            add(max(report, d - timedelta(days=1)), "RESERVE", "INDEMNITY", target - reserve)
            reserve = target
        amt = cost * float(share) if k < n_pay - 1 else cost - paid
        paid += round(amt, 2)
        add(d, "PAYMENT", "INDEMNITY", amt)
        take = round(min(reserve, amt), 2)
        add(d, "RESERVE", "INDEMNITY", -take)
        reserve = round(reserve - take, 2)
    if alae:
        add(closed, "PAYMENT", "ALAE", alae)
    if reserve:
        add(closed, "RESERVE", "INDEMNITY", -reserve)  # release what's left at closure
    recovery = 0.0
    if cost and peril in ("OWN_DAMAGE", "THIRD_PARTY_PROPERTY") and rng.random() < 0.10:
        recovery = -round(cost * float(rng.uniform(0.3, 0.9)), 2)
        add(closed + timedelta(days=int(rng.integers(30, 240))), "RECOVERY", "INDEMNITY", recovery)

    book.rows["claim"].append((cid, tid, peril, acc, report, closed))
    for d, kind, ctype, amt in txns:
        book.rows["claim_txn"].append((book.next_id("claim_txn"), cid, d, kind, ctype, amt))
    truth.append((cid, acc, peril, round(paid + alae + recovery, 2)))


# --------------------------------------------------------------------------- expenses

def _expenses(cfg: Config, rng, book: Book) -> None:
    """Monthly acquisition cost per new term, broker commission on written premium, admin per in-force."""
    terms = {t[0]: t for t in book.rows["policy_term"]}
    new: dict[tuple[date, str], int] = {}
    in_force_delta: dict[date, int] = {}
    for t in terms.values():
        if t[2] == 1:
            new[(_month(t[3]), t[7])] = new.get((_month(t[3]), t[7]), 0) + 1
        on, off = t[4], t[6] or t[5]  # in force on the 1st of each month in [on, off)
        first = on if on.day == 1 else _next_month(on)
        last = off if off.day == 1 else _next_month(off)
        in_force_delta[first] = in_force_delta.get(first, 0) + 1
        in_force_delta[last] = in_force_delta.get(last, 0) - 1
    broker: dict[date, float] = {}
    for p in book.rows["premium_txn"]:
        if terms[p[1]][7] == "BROKER":
            broker[_month(p[3])] = broker.get(_month(p[3]), 0.0) + float(p[6])

    in_force = sum(v for k, v in in_force_delta.items() if k < cfg.start)
    month = cfg.start
    while month <= cfg.horizon:
        in_force += in_force_delta.get(month, 0)
        booked = _next_month(month) + timedelta(days=int(rng.integers(3, 12)))
        for ch, fee in (("PCW", 58.0), ("DIRECT", 31.0)):
            if new.get((month, ch)):
                book.rows["expense"].append((book.next_id("expense"), month, booked, "ACQUISITION", ch,
                                             round(new[(month, ch)] * fee * float(rng.lognormal(0, 0.05)), 2)))
        if broker.get(month):
            book.rows["expense"].append((book.next_id("expense"), month, booked, "COMMISSION", "BROKER",
                                         round(broker[month] * 0.125, 2)))
        book.rows["expense"].append((book.next_id("expense"), month, booked, "ADMIN", None,
                                     round((42000 + in_force * 3.1) * float(rng.lognormal(0, 0.04)), 2)))
        month = _next_month(month)


# --------------------------------------------------------------------------- horizon cut + IBNR

def _cut_at_horizon(cfg: Config, book: Book) -> dict[str, list[tuple]]:
    """Keep only what the insurer can know on the horizon date."""
    h, r = cfg.horizon, book.rows
    out: dict[str, list[tuple]] = {}
    terms = [t for t in r["policy_term"] if t[3] <= h]
    known_terms = {t[0] for t in terms}
    out["policy_term"] = [t[:6] + (t[6] if t[6] and t[6] <= h else None,) + t[7:] for t in terms]
    known_policies = {t[1] for t in terms}
    out["policy"] = [p for p in r["policy"] if p[0] in known_policies]
    out["quote"] = [q[:8] + (q[8] if q[8] in known_terms else None,) for q in r["quote"]]
    out["premium_txn"] = [p for p in r["premium_txn"] if p[3] <= h and p[1] in known_terms]
    out["renewal_offer"] = [o[:7] + (o[7] if o[7] in known_terms else None,) for o in r["renewal_offer"]
                            if o[2] <= h and o[1] in known_terms]
    claims = [c for c in r["claim"] if c[4] <= h and c[1] in known_terms]
    known_claims = {c[0] for c in claims}
    out["claim"] = [c[:5] + (c[5] if c[5] <= h else None,) for c in claims]
    out["claim_txn"] = [x for x in r["claim_txn"] if x[2] <= h and x[1] in known_claims]
    out["expense"] = [e for e in r["expense"] if e[2] <= h]
    return out


def quarter_ends(start: date, end: date) -> list[date]:
    out, d = [], date(start.year, 3, 31)
    while d <= end:
        if d >= start:
            out.append(d)
        m = d.month + 3
        y = d.year + (m > 12)
        m = m - 12 if m > 12 else m
        d = _next_month(date(y, m, 1)) - timedelta(days=1)
    return out


def _ibnr_valuations(cfg: Config, tables: dict[str, list[tuple]]) -> list[tuple]:
    """Chain ladder on accident-quarter × development-quarter incurred, at every quarter-end.

    Volume-weighted age-to-age factors, no tail. IBNR per accident year is the sum over its
    accident quarters of (projected ultimate − incurred to date): the bulk reserve a reserving
    team books, including development on known claims.
    """
    acc = {c[0]: c[3] for c in tables["claim"]}
    rows = sorted((x[2], acc[x[1]], float(x[5])) for x in tables["claim_txn"])

    def q(d: date) -> int:
        return d.year * 4 + (d.month - 1) // 3

    out: list[tuple] = []
    tri: dict[int, dict[int, float]] = {}
    k = 0
    for v in quarter_ends(date(cfg.start.year, 12, 31), cfg.horizon):
        while k < len(rows) and rows[k][0] <= v:
            td, a, amt = rows[k]
            tri.setdefault(q(a), {})
            tri[q(a)][q(td) - q(a)] = tri[q(a)].get(q(td) - q(a), 0.0) + amt
            k += 1
        vq = q(v)
        cum = {}
        for aq, devs in tri.items():
            run, seq = 0.0, []
            for dev in range(vq - aq + 1):
                run += devs.get(dev, 0.0)
                seq.append(run)
            cum[aq] = seq
        ages = max(len(s) for s in cum.values())
        factors = []
        for dev in range(ages - 1):
            num = sum(s[dev + 1] for s in cum.values() if len(s) > dev + 1)
            den = sum(s[dev] for s in cum.values() if len(s) > dev + 1)
            factors.append(num / den if den > 0 else 1.0)
        by_year: dict[int, float] = {}
        for aq, s in cum.items():
            cdf = math.prod(factors[len(s) - 1:])
            by_year[aq // 4] = by_year.get(aq // 4, 0.0) + max(s[-1] * cdf - s[-1], 0.0)
        out.extend((v, ay, round(x, 2)) for ay, x in sorted(by_year.items()))
    return out


# --------------------------------------------------------------------------- output

def write_duckdb(tables: dict[str, list[tuple]], path: str | Path) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.unlink(missing_ok=True)
    con = duckdb.connect(str(path))
    con.execute((Path(__file__).parent / "schema.sql").read_text())
    con.execute("CREATE TABLE truth_claim_cost (claim_id BIGINT, accident_year INTEGER, peril VARCHAR, "
                "ultimate_cost DECIMAL(14,2))")
    for name in TABLES:
        rows = tables.get(name)
        if not rows:
            continue
        # Columnar insert via Arrow; DuckDB casts to the declared column types.
        cols = [r[0] for r in con.execute(f"DESCRIBE {name}").fetchall()]
        arrow = pa.table({c: pa.array([row[i] for row in rows]) for i, c in enumerate(cols)})  # noqa: F841
        con.execute(f"INSERT INTO {name} SELECT * FROM arrow")
    con.close()
