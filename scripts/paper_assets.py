"""Regenerate every figure, table and number quoted in the paper.

Reads data/book.duckdb, bench/questions.yml and results/scores.jsonl (written by
`isl bench`). Writes paper/figures/*.pdf and paper/generated/*.tex.
Usage: uv run python scripts/paper_assets.py
"""

from __future__ import annotations

import json
import random
import re
import statistics
from collections import Counter, defaultdict
from datetime import date
from pathlib import Path

import duckdb
import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt

from isl.bench import CONDITIONS, MAX_TURNS, load_questions
from isl.compiler import Compiler, MetricQuery, run
from isl.generate import Config, quarter_ends

ROOT = Path(__file__).resolve().parents[1]
GEN = ROOT / "paper" / "generated"
FIG = ROOT / "paper" / "figures"
PRICES = {  # USD per million tokens: input, output, cache read, cache write (5-minute)
    "claude-haiku-5-5": (0.10, 0.50, 0.01, 0.125),
    "claude-sonnet-5-5": (2.00, 10.00, 0.20, 2.50),
    "claude-opus-5-5": (4.00, 20.00, 0.20, 5.00),
}
MODEL_LABEL = {"claude-haiku-5-5": "Haiku 5.5", "claude-sonnet-5-5": "Sonnet 5.5", "claude-opus-5-5": "Opus 5.5"}
CATEGORY_LABEL = {"control": "Control", "earning": "Earning", "time_basis": "Time basis", "valuation": "Valuation",
                  "loss_definition": "Loss definition", "aggregation": "Aggregation", "snapshot": "Snapshot",
                  "counting": "Counting", "retention": "Retention", "ambiguous": "Ambiguous",
                  "unanswerable": "Unanswerable"}
METRIC_LABEL = {"loss_ratio_paid": "paid", "loss_ratio_reported": "reported", "loss_ratio_ultimate": "ultimate",
                "loss_alae_ratio_ultimate": "ultimate + ALAE"}
BASIS_LABEL = {"calendar": "calendar year", "accident": "accident year", "policy": "policy year"}

plt.rcParams.update({"font.family": "serif", "font.size": 9, "axes.spines.top": False,
                     "axes.spines.right": False, "figure.dpi": 150})


def macro(name: str, value) -> str:
    return f"\\newcommand{{\\{name}}}{{{value}}}"


def tex_escape(s: str) -> str:
    return (s.replace("\\", "\\textbackslash{}").replace("&", "\\&").replace("%", "\\%").replace("$", "\\$")
            .replace("#", "\\#").replace("_", "\\_").replace("{", "\\{").replace("}", "\\}"))


def cell(con, c, metric, basis, as_of, period, **kw):
    _cols, rows = run(con, MetricQuery(metrics=[metric], basis=basis, as_of=as_of, **kw), c)
    return {r[0]: r[-1] for r in rows}.get(period)


def main() -> None:
    GEN.mkdir(parents=True, exist_ok=True)
    FIG.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(str(ROOT / "data" / "book.duckdb"), read_only=True)
    comp = Compiler()
    horizon = date(2025, 12, 31)
    nums: list[str] = []

    # ------------------------------------------------------------------ headline: ten answers
    answers = []
    for m in METRIC_LABEL:
        for b in comp.model.bases(m):
            v = cell(con, comp, m, b, horizon, "2024")
            if v is not None:
                answers.append((v, m, b))
    answers.sort()
    nums += [macro("HeadlineCount", len(answers)), macro("HeadlineMin", f"{answers[0][0]:.2f}"),
             macro("HeadlineMax", f"{answers[-1][0]:.2f}")]
    fig, ax = plt.subplots(figsize=(6.2, 2.9))
    for i, (v, m, b) in enumerate(answers):
        ax.plot([answers[0][0] - 0.02, v], [i, i], color="#ddd", lw=0.8, zorder=1)
        ax.plot([v], [i], "o", color="#1f4fd1", ms=5.5, zorder=2)
        ax.text(v + 0.006, i, f"{v:.3f}", va="center", fontsize=7, color="#333")
    ax.set_yticks(range(len(answers)))
    ax.set_yticklabels([f"{BASIS_LABEL[b]}, {METRIC_LABEL[m]}" for _v, m, b in answers], fontsize=7.5)
    ax.tick_params(axis="y", length=0)
    ax.spines["left"].set_visible(False)
    ax.set_xlim(answers[0][0] - 0.02, answers[-1][0] + 0.05)
    ax.set_xlabel("2024 loss ratio, valued at 31 December 2025")
    fig.tight_layout()
    fig.savefig(FIG / "answers.pdf")
    plt.close(fig)

    # ------------------------------------------------------------------ development figure
    vals = quarter_ends(date(2019, 12, 31), horizon)
    fig, axes = plt.subplots(1, 2, figsize=(6.4, 2.4), sharey=True)
    for ax, metric in zip(axes, ["loss_ratio_reported", "loss_ratio_ultimate"]):
        for year, color in zip(["2021", "2022", "2023", "2024"], ["#999", "#666", "#1f4fd1", "#0b1f5c"]):
            xs, ys = [], []
            for v in vals:
                if v.year < int(year):
                    continue
                y = cell(con, comp, metric, "accident", v, year)
                if y is not None:
                    xs.append(v)
                    ys.append(y)
            ax.plot(xs, ys, color=color, lw=1.2, label=f"AY {year}")
        ax.set_title(f"Accident-year {METRIC_LABEL[metric]} loss ratio", fontsize=8.5)
        ax.set_xlabel("valuation date")
        ax.tick_params(axis="x", labelsize=7)
    axes[0].set_ylabel("loss ratio")
    axes[1].legend(frameon=False, fontsize=7)
    fig.tight_layout()
    fig.savefig(FIG / "development.pdf")
    plt.close(fig)

    # ------------------------------------------------------------------ book statistics
    def q1(sql):
        v = con.execute(sql).fetchone()[0]
        return float(v) if v is not None and not isinstance(v, int) else v

    pcw_share = q1("SELECT avg((channel = 'PCW')::int) FROM quote")
    stats = [
        ("Quotes", f"{q1('SELECT count(*) FROM quote'):,}"),
        ("Policy terms (incl.\\ renewals)", f"{q1('SELECT count(*) FROM policy_term'):,}"),
        ("Premium transactions", f"{q1('SELECT count(*) FROM premium_txn'):,}"),
        ("Claims reported", f"{q1('SELECT count(*) FROM claim'):,}"),
        ("Claim transactions", f"{q1('SELECT count(*) FROM claim_txn'):,}"),
        ("Quarterly IBNR valuations", f"{q1('SELECT count(DISTINCT valuation_date) FROM ibnr_valuation')}"),
        ("Written premium 2025 (GBP m)", f"{q1('SELECT sum(written_premium) FROM premium_txn WHERE year(booked_date)=2025') / 1e6:.1f}"),
        ("Vehicles in force at horizon", f"{cell(con, comp, 'in_force_count', 'calendar', horizon, '2025'):,.0f}"),
        ("PCW share of quotes", f"{pcw_share:.0%}".replace("%", "\\%")),
        ("Renewal retention 2025", f"{cell(con, comp, 'retention_rate', 'calendar', horizon, '2025'):.1%}".replace("%", "\\%")),
        ("Claim frequency AY2024 (per car-year)", f"{cell(con, comp, 'claim_frequency', 'accident', horizon, '2024'):.3f}"),
    ]
    rows = "\n".join(f"{k} & {v} \\\\" for k, v in stats)
    (GEN / "book_table.tex").write_text(
        "\\begin{table}[t]\\centering\\small\n\\caption{The synthetic book (seed 7, horizon 31 December 2025).}"
        "\\label{tab:book}\n\\begin{tabular}{lr}\\toprule\n" + rows + "\n\\bottomrule\\end{tabular}\\end{table}\n",
        encoding="utf-8")
    cfg = Config()
    nums += [macro("BookStart", f"{cfg.start.day} {cfg.start:%B %Y}"),
             macro("BookHorizon", f"{cfg.horizon.day} {cfg.horizon:%B %Y}")]

    drift = []
    for y in (2022, 2023, 2024):
        a = cell(con, comp, "earned_premium", "calendar", date(y, 12, 31), str(y))
        b = cell(con, comp, "earned_premium", "calendar", horizon, str(y))
        drift.append(abs(b - a) / a * 100)
    nums.append(macro("EarnedDriftPct", f"at most {max(drift):.4f}\\%"))
    errs = con.execute("""
        WITH truth AS (SELECT accident_year ay, sum(ultimate_cost) ult FROM truth_claim_cost GROUP BY 1),
        rep AS (SELECT year(c.accident_date) ay, sum(x.amount) rep FROM claim_txn x JOIN claim c USING (claim_id)
                GROUP BY 1),
        ib AS (SELECT accident_year ay, ibnr FROM ibnr_valuation WHERE valuation_date = DATE '2025-12-31')
        SELECT (rep + ibnr) / ult - 1 FROM truth JOIN rep USING (ay) JOIN ib USING (ay)""").fetchall()
    errs = [e[0] for e in errs]
    nums.append(macro("IbnrErrRange", f"{min(errs) * 100:+.1f}\\% to {max(errs) * 100:+.1f}\\%"))

    # ------------------------------------------------------------------ appendices
    sql = comp.compile(MetricQuery(metrics=["loss_ratio_ultimate"], basis="accident", as_of=horizon))
    (GEN / "example.sql").write_text(sql, encoding="utf-8")
    yml = (ROOT / "semantic" / "motor.yml").read_text(encoding="utf-8").splitlines()
    keep, on, in_measures = [], False, False
    wanted = re.compile(r"^  (earned_premium|reported_loss|ibnr|case_reserve):")
    for line in yml:
        if re.match(r"^\w", line):  # top-level key: sources / measures / metrics
            in_measures = line.startswith("measures:")
            on = False
            continue
        if in_measures and re.match(r"^  \w+:", line):
            on = bool(wanted.match(line))
        if on:
            keep.append(line)
    metrics_block = [ln for ln in yml if re.match(r"^  (loss_ratio_ultimate|retention_rate):", ln) or
                     ln.startswith("    expr: (reported_loss + ibnr) / earned_premium")]
    (GEN / "model_excerpt.yml").write_text("measures:\n" + "\n".join(keep) + "\n\nmetrics:\n  ...\n" +
                                           "\n".join(metrics_block[:2]) + "\n", encoding="utf-8")

    qs, _defaults = load_questions()
    by_cat = Counter(q.category for q in qs)
    nums.append(macro("BenchQuestions", len(qs)))
    nums.append(macro("MaxTurns", MAX_TURNS))
    qrows = "\n".join(f"\\texttt{{{q.id}}} & {CATEGORY_LABEL[q.category]} & {tex_escape(q.question)} \\\\"
                      for q in qs)
    (GEN / "questions.tex").write_text(
        "{\\small\\begin{longtable}{p{0.6cm}p{2.2cm}p{11.5cm}}\\toprule id & category & question \\\\ \\midrule\n"
        + qrows + "\n\\bottomrule\\end{longtable}}\n", encoding="utf-8")

    # ------------------------------------------------------------------ benchmark results
    scores_path = ROOT / "results" / "scores.jsonl"
    scores = [json.loads(line) for line in scores_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    models = [m for m in PRICES if any(s["model"] == m for s in scores)]
    rng = random.Random(0)

    def boot(xs, n=2000):
        if not xs:
            return (0, 0)
        means = sorted(statistics.fmean(rng.choices(xs, k=len(xs))) for _ in range(n))
        return means[int(0.025 * n)], means[int(0.975 * n)]

    def cost(model, u):
        pi, po, pr, pw = PRICES[model]
        return (u["input"] * pi + u["output"] * po + u["cache_read"] * pr + u["cache_write"] * pw) / 1e6

    main_rows, cat_cols = [], defaultdict(dict)
    acc = {}
    for m in models:
        for cnd in CONDITIONS:
            rs = [s for s in scores if s["model"] == m and s["condition"] == cnd]
            if not rs:
                continue
            xs = [float(s["correct"]) for s in rs]
            lo, hi = boot(xs)
            core = [s for s in rs if s["category"] not in ("ambiguous", "unanswerable")]
            amb = [s for s in rs if s["category"] == "ambiguous"]
            una = [s for s in rs if s["category"] == "unanswerable"]
            wrong_n = sum(not s["correct"] for s in rs)
            quoted = sum(bool(s.get("gold_in_text")) for s in rs if not s["correct"])
            usd = sum(cost(m, s["usage"]) for s in rs)
            acc[(m, cnd)] = statistics.fmean(xs)
            main_rows.append(
                f"{MODEL_LABEL[m]} & {cnd} & {statistics.fmean(xs):.0%} [{lo:.0%}, {hi:.0%}] & "
                f"{statistics.fmean([s['correct'] for s in core]):.0%} & "
                f"{sum(s['correct'] for s in amb)}/{len(amb)} ({sum(bool(s['disclosed']) for s in amb)}) & "
                f"{sum(s['correct'] for s in una)}/{len(una)} & {wrong_n} ({quoted}) & \\${usd:.2f} \\\\".replace("%", "\\%"))
            for cat in CATEGORY_LABEL:
                cs = [s for s in rs if s["category"] == cat]
                if cs:
                    cat_cols[(m, cnd)][cat] = (sum(s["correct"] for s in cs), len(cs))
    for (m, cnd), v in acc.items():
        nums.append(macro("Acc" + MODEL_LABEL[m].split()[0]
                          + cnd.title(), f"{v:.0%}".replace("%", "\\%")))

    header = " & ".join(f"{MODEL_LABEL[m].split()[0]} {c[:3]}" for (m, c) in cat_cols)
    cat_rows = []
    for cat in CATEGORY_LABEL:
        cells = []
        for k in cat_cols:
            c, n = cat_cols[k].get(cat, (0, 0))
            cells.append(f"{c}/{n}")
        cat_rows.append(f"{CATEGORY_LABEL[cat]} ({by_cat[cat]}) & " + " & ".join(cells) + " \\\\")

    # Error taxonomy (manual labels from the models' own explanations).
    import yaml

    tax = yaml.safe_load((ROOT / "results" / "error_taxonomy.yml").read_text(encoding="utf-8"))
    class_of = {(m, c, q): k for m, conds in tax["errors"].items() for c, qs_ in conds.items()
                for q, k in qs_.items()}
    wrong = {(s["model"], s["condition"], s["qid"]) for s in scores if not s["correct"]}
    missing = wrong - set(class_of)
    stale = set(class_of) - wrong
    if missing or stale:
        raise SystemExit(f"error_taxonomy.yml is out of date: unlabelled {sorted(missing)}, stale {sorted(stale)}")
    class_label = {"alae": "ALAE included in losses", "recoveries": "Recoveries not netted",
                   "day_count": "Day-count convention", "cohort": "Wrong cohort definition",
                   "substituted": "Answered a different question", "estimated": "Estimated instead of declining"}
    outcome_rows = []
    for k, label in class_label.items():
        outcome_rows.append(f"{label} & " + " & ".join(
            str(sum(1 for (m, c, _q), kk in class_of.items() if kk == k and (m, c) == key)) for key in cat_cols)
            + " \\\\")
    convention = set(tax["classes"]["convention"])
    for m in models:
        rs = [s for s in scores if s["model"] == m and s["condition"] == "raw"]
        adj = sum(s["correct"] or class_of.get((m, "raw", s["qid"])) in convention for s in rs) / len(rs)
        nums.append(macro("AdjRaw" + MODEL_LABEL[m].split()[0], f"{adj:.0%}".replace("%", "\\%")))
        conv = sum(1 for (mm, c, _q), k in class_of.items() if mm == m and c == "raw" and k in convention)
        nums.append(macro("ConvErrRaw" + MODEL_LABEL[m].split()[0], conv))
        nums.append(macro("WrongRaw" + MODEL_LABEL[m].split()[0], sum(not s["correct"] for s in rs)))
        nums.append(macro("GoldMentionedRaw" + MODEL_LABEL[m].split()[0],
                          sum(bool(s.get("gold_in_text")) for s in rs if not s["correct"])))

    cache_dir = ROOT / "results" / "cache"
    per = defaultdict(list)
    for f in cache_dir.glob("*.json"):
        r = json.loads(f.read_text(encoding="utf-8"))
        if r["model"] in models:
            per[(r["model"], r["condition"])].append(len(r["tool_calls"]))
    calls = {k: statistics.fmean(v) for k, v in per.items()}

    results_tex = [
        "\\begin{figure}[t]\\centering\\includegraphics[width=\\linewidth]{figures/answers.pdf}",
        "\\caption{Every defensible answer to ``what was the 2024 loss ratio?'' on the synthetic book, valued at 31",
        "December 2025, as computed by the semantic layer. Ultimate figures include booked IBNR; the policy-year basis",
        "has no booked IBNR, so no policy-year ultimate exists.}\\label{fig:answers}\\end{figure}",
        "",
        "\\begin{figure}[t]\\centering\\includegraphics[width=\\linewidth]{figures/development.pdf}",
        "\\caption{The same accident year read at successive quarter-end valuation dates. Reported loss ratios rise as",
        "late claims are reported and reserves develop; adding booked IBNR anticipates much of that development.}",
        "\\label{fig:development}\\end{figure}",
        "",
        "\\begin{table}[t]\\centering\\small",
        "\\caption{ISL-Bench results. Accuracy over all questions with a 95\\% bootstrap interval over questions; "
        "accuracy on the answerable, unambiguous core; ambiguous questions answered with a defensible value "
        "(and, in brackets, how many answers named a basis or definition); unanswerable questions declined; wrong answers "
        "(in brackets, how many quoted the governed figure in their explanation); API cost.}\\label{tab:main}",
        "\\resizebox{\\linewidth}{!}{\\begin{tabular}{llrrrrrr}\\toprule",
        "Model & Condition & All & Core & Ambiguous (disclosed) & Unanswerable & Wrong (quoted) & Cost \\\\ \\midrule",
        *main_rows,
        "\\bottomrule\\end{tabular}}\\end{table}",
        "",
        "\\begin{table}[t]\\centering\\small",
        "\\caption{Correct answers by question category (number of questions in brackets).}\\label{tab:categories}",
        "\\resizebox{\\linewidth}{!}{\\begin{tabular}{l" + "r" * len(cat_cols) + "}\\toprule",
        "Category & " + header + " \\\\ \\midrule",
        *cat_rows,
        "\\midrule Mean tool calls & " + " & ".join(f"{calls.get(k, 0):.1f}" for k in cat_cols) + " \\\\",
        "\\bottomrule\\end{tabular}}\\end{table}",
        "",
        "\\begin{table}[t]\\centering\\small",
        "\\caption{Every incorrect answer, classified from the model's own explanation. The first three classes "
        "are house conventions that cannot be discovered from raw tables.}\\label{tab:errors}",
        "\\resizebox{\\linewidth}{!}{\\begin{tabular}{l" + "r" * len(cat_cols) + "}\\toprule",
        "Error class & " + header + " \\\\ \\midrule",
        *outcome_rows,
        "\\bottomrule\\end{tabular}}\\end{table}",
        "",
        "\\input{results_text.tex}",
    ]
    (GEN / "results.tex").write_text("\n".join(results_tex) + "\n", encoding="utf-8")

    import subprocess
    tests = subprocess.run(["uv", "run", "--no-sync", "pytest", "-q", "--collect-only"], cwd=ROOT,
                           capture_output=True, text=True).stdout
    m = re.search(r"(\d+) tests? collected", tests)
    nums.append(macro("TestCount", m.group(1) if m else "200+"))
    n_cross = len([ln for ln in tests.splitlines() if "test_compiler_matches_reference" in ln])
    nums.append(macro("CrossChecks", n_cross or "about 190"))
    (GEN / "numbers.tex").write_text("\n".join(nums) + "\n", encoding="utf-8")
    print("wrote", sorted(p.name for p in GEN.iterdir()), sorted(p.name for p in FIG.iterdir()))


if __name__ == "__main__":
    main()
