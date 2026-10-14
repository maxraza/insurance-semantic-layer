"""Export the fixture the hraza.online simulator runs on.

Everything in it is computed by the compiler on the seed-7 book, so the web page shows
the same numbers the paper reports. Usage: uv run python scripts/export_simulator.py
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import duckdb

from isl.compiler import BASES, Compiler, MetricQuery, SemanticError, run
from isl.generate import quarter_ends

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "exports" / "simulator.json"
LOSS_METRICS = ["loss_ratio_paid", "loss_ratio_reported", "loss_ratio_ultimate", "loss_alae_ratio_ultimate"]
EXPLORER = ["earned_premium", "written_premium", "reported_loss", "paid_loss", "ibnr", "case_reserve",
            "in_force_count", "claim_frequency", "severity_reported", "retention_rate", "quote_conversion",
            "expense_ratio", "combined_ratio", *LOSS_METRICS]
DIMENSIONS = ["channel", "region", "driver_age_band", "peril"]


def values(con, compiler, metric: str, basis: str, as_of: date, group_by=None) -> dict:
    _cols, rows = run(con, MetricQuery(metrics=[metric], basis=basis, as_of=as_of, group_by=group_by or []),
                     compiler)
    if group_by:
        return {f"{r[0]}|{r[1]}": r[-1] for r in rows}
    return {r[0]: r[-1] for r in rows}


def main() -> None:
    con = duckdb.connect(str(ROOT / "data" / "book.duckdb"), read_only=True)
    compiler = Compiler()
    model = compiler.model
    valuations = quarter_ends(date(2019, 12, 31), date(2025, 12, 31))
    out: dict = {"generated": date.today().isoformat(), "seed": 7, "horizon": "2025-12-31",
                 "valuations": [v.isoformat() for v in valuations]}

    # 1. Loss ratios: metric × basis × valuation date → {year: value}
    lr: dict = {}
    for m in LOSS_METRICS:
        for b in model.bases(m):
            for v in valuations:
                lr.setdefault(m, {}).setdefault(b, {})[v.isoformat()] = values(con, compiler, m, b, v)
    out["loss_ratios"] = lr

    # 2. Explorer: every metric on every basis at the horizon, plus by dimension where allowed,
    #    and the exact rejection message where not.
    explorer: dict = {}
    horizon = date(2025, 12, 31)
    for m in EXPLORER:
        entry = {"description": model.describe(m), "kind": "metric" if m in model.metrics else "measure",
                 "expr": model.metrics[m]["expr"] if m in model.metrics else None,
                 "bases": {}, "dimensions": {}}
        for b in BASES:
            q = MetricQuery(metrics=[m], basis=b, as_of=horizon)
            try:
                entry["bases"][b] = {"values": values(con, compiler, m, b, horizon), "sql": compiler.compile(q)}
            except SemanticError as e:
                entry["bases"][b] = {"error": str(e)}
        for d in DIMENSIONS:
            b = model.bases(m)[0]
            try:
                entry["dimensions"][d] = {"basis": b, "values": values(con, compiler, m, b, horizon, [d])}
            except SemanticError as e:
                entry["dimensions"][d] = {"error": str(e)}
        explorer[m] = entry
    out["explorer"] = explorer

    # 3. The headline: every defensible answer to "what was the 2024 loss ratio?"
    answers = []
    for m in LOSS_METRICS:
        for b in model.bases(m):
            v = lr[m][b]["2025-12-31"].get("2024")
            if v is not None:
                answers.append({"metric": m, "basis": b, "value": v})
    out["headline"] = {"question": "What was the 2024 loss ratio?", "as_of": "2025-12-31", "answers": answers}

    # 4. Semantic model, for display.
    out["model_yaml"] = (ROOT / "semantic" / "motor.yml").read_text(encoding="utf-8")

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(out, separators=(",", ":"), default=str), encoding="utf-8")
    print(f"wrote {OUT} ({OUT.stat().st_size / 1024:.0f} KB)")


if __name__ == "__main__":
    main()
