"""ISL-Bench: ask each question under three conditions and score the answers.

Conditions
  raw       schema DDL (no comments) + a read-only SQL tool
  docs      commented DDL + a prose business glossary (the semantic model as text) + the SQL tool
  semantic  the semantic layer as a tool (`query_metrics`); no SQL

Every model response is cached under results/cache/, keyed by (prompt version, model,
condition, question), so a re-run reproduces the tables without API calls.
"""

from __future__ import annotations

import hashlib
import json
import re
import statistics
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

import duckdb
import yaml

from .compiler import Compiler, MetricQuery, SemanticError, run

ROOT = Path(__file__).resolve().parents[2]
QUESTIONS = ROOT / "bench" / "questions.yml"
CACHE = ROOT / "results" / "cache"
PROMPT_VERSION = "v1"
MAX_TURNS = 10
MAX_ROWS = 60
PUBLIC_TABLES = ["quote", "policy", "policy_term", "premium_txn", "renewal_offer", "claim", "claim_txn",
                 "ibnr_valuation", "expense"]
CONDITIONS = ("raw", "docs", "semantic")

INSTRUCTIONS = """You are an analyst at a UK personal motor insurer, answering a colleague's question from the \
company's data. Data is complete up to 31 December 2025; unless the question says otherwise, value figures as \
at 31 December 2025. Amounts are in GBP and gross of reinsurance.

Use the tools to get the numbers; don't estimate. If the question can't be answered from the data and \
definitions available, say why rather than guessing. Keep the explanation short.

End your reply with exactly one final line in this form:
ANSWER: <number>
Give ratios and rates as decimals (0.734, not 73.4%) and counts and amounts as plain numbers. If the question \
can't be answered, end with:
ANSWER: UNANSWERABLE"""


# ---------------------------------------------------------------------- gold answers

@dataclass
class Question:
    id: str
    category: str
    question: str
    answer: dict | None = None
    derived: dict | None = None
    accept: list[dict] = field(default_factory=list)
    accept_derived: list[dict] = field(default_factory=list)
    traps: list[dict] = field(default_factory=list)
    ambiguous: bool = False
    unanswerable: bool = False
    why: str | None = None


def load_questions(path: Path = QUESTIONS) -> tuple[list[Question], dict]:
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    return [Question(**q) for q in raw["questions"]], raw["defaults"]


class Gold:
    """Resolves answer cells with the compiler against the full (seed 7) book."""

    def __init__(self, con, defaults: dict) -> None:
        self.con, self.defaults, self.compiler = con, defaults, Compiler()

    def cell(self, c: dict) -> float | None:
        as_of = c.get("as_of", self.defaults["as_of"])
        as_of = as_of if isinstance(as_of, date) else date.fromisoformat(str(as_of))
        q = MetricQuery(metrics=[c["metric"]], basis=c["basis"], as_of=as_of,
                        grain=c.get("grain", self.defaults["grain"]), filters=c.get("filters") or {})
        _cols, rows = run(self.con, q, self.compiler)
        values = {r[0]: r[-1] for r in rows}
        if c.get("period") == "all":
            picked = list(values.values())
        elif "period_in_year" in c:
            picked = [v for p, v in values.items() if str(p).startswith(str(c["period_in_year"]))]
        else:
            picked = [values.get(str(c["period"]))]
        picked = [v for v in picked if v is not None]
        if not picked:
            return None
        reduce = c.get("reduce")
        if reduce == "sum":
            return float(sum(picked))
        if reduce == "mean":
            return float(statistics.fmean(picked))
        return float(picked[0])

    def derived(self, d: dict) -> float | None:
        env = {k: self.cell(v) for k, v in d["parts"].items()}
        if any(v is None for v in env.values()):
            return None
        return float(eval(d["expr"], {"__builtins__": {}}, env))  # expr comes from questions.yml

    def resolve(self, q: Question) -> dict:
        if q.unanswerable:
            return {"gold": None, "accept": [], "traps": []}
        gold = self.derived(q.derived) if q.derived else self.cell(q.answer)
        accept = [self.cell(c) for c in q.accept] + [self.derived(d) for d in q.accept_derived]
        traps = [{"cell": t, "value": self.cell(t)} for t in q.traps]
        return {"gold": gold, "accept": [a for a in accept if a is not None], "traps": traps}


# ---------------------------------------------------------------------- context per condition

def public_db(src: Path, dst: Path) -> Path:
    """Copy of the book without the ground-truth table, for the SQL tool."""
    if dst.exists() and dst.stat().st_mtime >= src.stat().st_mtime:
        return dst
    dst.unlink(missing_ok=True)
    con = duckdb.connect(str(dst))
    con.execute(f"ATTACH '{src.as_posix()}' AS src (READ_ONLY)")
    for t in PUBLIC_TABLES:
        con.execute(f"CREATE TABLE {t} AS SELECT * FROM src.{t}")
    con.close()
    return dst


def schema_ddl(with_comments: bool) -> str:
    text = (ROOT / "src" / "isl" / "schema.sql").read_text(encoding="utf-8")
    if with_comments:
        return text
    lines = []
    for line in text.splitlines():
        line = re.sub(r"\s*--.*$", "", line)
        if line.strip():
            lines.append(line)
    return "\n".join(lines)


def glossary() -> str:
    """The semantic model rendered as the prose documentation a data team would write."""
    m = Compiler().model
    out = ["Business glossary (how the finance and actuarial teams define each figure):", ""]
    out.append("Time bases. Calendar basis assigns transactions to the period in which they were booked (premium "
               "is earned day by day over each transaction's cover period). Accident basis assigns losses by the "
               "claim's accident date and pairs them with premium earned in the same period. Policy (underwriting) "
               "basis assigns both premium and losses to the period in which the policy term incepted.")
    out.append("Valuation. A figure 'as at' a date uses only rows whose knowledge date (booked_date, report_date, "
               "txn_date, valuation_date, offer_date) is on or before it. Ratios are computed as ratios of totals "
               "at the requested grain, never as averages of ratios. Balances (in force, unearned premium, case "
               "reserves, open claims) are taken at the end of the period and are not summed across periods.")
    out.append("")
    for name in sorted(m.measures):
        meas = m.measures[name]
        out.append(f"- {name}: {m.describe(name)} Bases: {', '.join(meas['bases'])}. "
                   f"Can be split by: {', '.join(m.dimensions(name)) or 'nothing'}.")
    out.append("")
    for name in sorted(m.metrics):
        out.append(f"- {name} = {m.metrics[name]['expr']}: {m.describe(name)}")
    return "\n".join(out)


SQL_TOOL = {
    "name": "run_sql",
    "description": "Run one read-only DuckDB SQL query against the company database and get the result rows "
                   f"(at most {MAX_ROWS} rows are returned).",
    "input_schema": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"],
                     "additionalProperties": False},
}


def semantic_tool() -> dict:
    m = Compiler().model
    catalogue = []
    for name in m.names():
        catalogue.append(f"{name} [{'/'.join(m.bases(name))}; by: {', '.join(m.conformed_dimensions(name)) or '-'}]"
                         f": {m.describe(name)}")
    dims = sorted({d for n in m.names() for d in m.conformed_dimensions(n)})
    return {
        "name": "query_metrics",
        "description": (
            "Query the governed insurance semantic layer. Every query needs a time basis and a valuation date. "
            "calendar = accounting period (premium earned day by day); accident = loss assigned by accident "
            "date, paired with premium earned in the same period; policy = assigned by policy inception date. "
            "Only facts known on or before as_of are used. Ratios are computed as ratios of totals. The tool "
            "returns an error when a metric isn't defined on the requested basis or can't be split by a "
            "dimension.\n\nCatalogue (name [bases; dimensions]: description):\n" + "\n".join(catalogue)),
        "input_schema": {
            "type": "object",
            "properties": {
                "metrics": {"type": "array", "items": {"type": "string", "enum": m.names()}},
                "basis": {"type": "string", "enum": ["calendar", "accident", "policy"]},
                "as_of": {"type": "string", "description": "valuation date, YYYY-MM-DD"},
                "grain": {"type": "string", "enum": ["year", "quarter", "month"]},
                "group_by": {"type": "array", "items": {"type": "string", "enum": dims}},
                "filters": {"type": "object", "description": "dimension -> value or list of values, e.g. "
                            "{\"channel\": \"PCW\"}. Values: channel PCW|DIRECT|BROKER; region NORTH|MIDLANDS|"
                            "LONDON|SOUTH|SCOTLAND; vehicle_group A-E; driver_age_band 17-24|25-39|40-59|60+; "
                            "business_type NEW|RENEWAL; peril OWN_DAMAGE|THIRD_PARTY_PROPERTY|BODILY_INJURY|"
                            "WINDSCREEN|THEFT; expense_type ACQUISITION|COMMISSION|ADMIN"},
                "period_from": {"type": "string", "description": "first period start, YYYY-MM-DD (optional)"},
                "period_to": {"type": "string", "description": "last period, YYYY-MM-DD (optional)"},
            },
            "required": ["metrics", "basis", "as_of"],
            "additionalProperties": False,
        },
    }


def system_prompt(condition: str) -> str:
    if condition == "raw":
        return f"{INSTRUCTIONS}\n\nDatabase schema (DuckDB):\n\n{schema_ddl(False)}"
    if condition == "docs":
        return f"{INSTRUCTIONS}\n\nDatabase schema (DuckDB):\n\n{schema_ddl(True)}\n\n{glossary()}"
    return INSTRUCTIONS + "\n\nUse the query_metrics tool; it is the only data access available."


# ---------------------------------------------------------------------- tools

def _rows_text(cols: list[str], rows: list[tuple]) -> str:
    def fmt(v):
        if isinstance(v, float):
            return f"{v:.6g}" if abs(v) < 100 else f"{v:.2f}"
        return str(v)

    head = ",".join(cols)
    body = "\n".join(",".join(fmt(v) for v in r) for r in rows[:MAX_ROWS])
    more = f"\n... {len(rows) - MAX_ROWS} more rows" if len(rows) > MAX_ROWS else ""
    return f"{head}\n{body}{more}" if rows else f"{head}\n(no rows)"


def execute_tool(name: str, args: dict, sql_con, sem_con) -> tuple[str, bool]:
    try:
        if name == "run_sql":
            rel = sql_con.execute(args["query"])
            return _rows_text([d[0] for d in rel.description], rel.fetchall()), False
        if name == "query_metrics":
            cols, rows = run(sem_con, MetricQuery.from_dict(args))
            return _rows_text(cols, rows), False
        return f"unknown tool {name}", True
    except (SemanticError, duckdb.Error, KeyError, ValueError, TypeError) as e:
        return f"error: {e}", True


# ---------------------------------------------------------------------- running

def _cache_key(model: str, condition: str, qid: str) -> str:
    h = hashlib.sha256(f"{PROMPT_VERSION}|{model}|{condition}|{qid}".encode()).hexdigest()[:16]
    return f"{model}__{condition}__{qid}__{h}.json"


def ask(client, model: str, condition: str, q: Question, sql_con, sem_con, effort: str = "medium") -> dict:
    """One question under one condition: an agentic tool loop, cached to disk.

    `client` may be a zero-argument factory; it is only called on a cache miss.
    """
    path = CACHE / _cache_key(model, condition, q.id)
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    if callable(client):
        client = client()
    tools = [semantic_tool()] if condition == "semantic" else [SQL_TOOL]
    system = [{"type": "text", "text": system_prompt(condition), "cache_control": {"type": "ephemeral"}}]
    messages: list = [{"role": "user", "content": q.question}]
    trace, usage = [], {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0}
    text, stop = "", None
    for _turn in range(MAX_TURNS):
        resp = client.messages.create(model=model, max_tokens=16000, system=system, tools=tools,
                                      messages=messages, output_config={"effort": effort})
        u = resp.usage
        usage["input"] += u.input_tokens
        usage["output"] += u.output_tokens
        usage["cache_read"] += u.cache_read_input_tokens or 0
        usage["cache_write"] += u.cache_creation_input_tokens or 0
        stop = resp.stop_reason
        messages.append({"role": "assistant", "content": resp.content})  # thinking blocks passed back intact
        calls = [b for b in resp.content if b.type == "tool_use"]
        texts = [b.text for b in resp.content if b.type == "text"]
        if texts:
            text = "\n".join(texts)
        if stop != "tool_use" or not calls:
            break
        results = []
        for b in calls:
            out, err = execute_tool(b.name, b.input, sql_con, sem_con)
            trace.append({"tool": b.name, "input": b.input, "output": out[:4000], "error": err})
            results.append({"type": "tool_result", "tool_use_id": b.id, "content": out, "is_error": err})
        messages.append({"role": "user", "content": results})
    record = {"model": model, "condition": condition, "qid": q.id, "question": q.question, "final_text": text,
              "stop_reason": stop, "tool_calls": trace, "usage": usage, "effort": effort,
              "prompt_version": PROMPT_VERSION}
    CACHE.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record, indent=1, default=str), encoding="utf-8")
    return record


# ---------------------------------------------------------------------- scoring

ANSWER_RE = re.compile(r"ANSWER:\s*(.+?)\s*$", re.IGNORECASE | re.MULTILINE)
BASIS_RE = re.compile(r"\b(accident[- ]year|calendar[- ]year|policy[- ]year|underwriting[- ]year|AY|CY|PY|UY|"
                      r"accident date|report(?:ed)? date|inception|calendar basis|accident basis|policy basis)\b",
                      re.IGNORECASE)
DEFINITION_RE = re.compile(r"\b(paid|reported|case reserves?|incurred|ultimate|IBNR|earned|written|"
                           r"booked)\b", re.IGNORECASE)


def parse_answer(text: str) -> float | str | None:
    found = ANSWER_RE.findall(text or "")
    if not found:
        return None
    a = found[-1].strip().strip("*` ")
    if "UNANSWERABLE" in a.upper():
        return "UNANSWERABLE"
    pct = "%" in a
    m = re.search(r"-?[\d,]*\.?\d+(?:[eE][-+]?\d+)?", a.replace("£", ""))
    if not m:
        return None
    v = float(m.group(0).replace(",", ""))
    return v / 100 if pct else v


REL_TOL = 0.002     # amounts and counts
RATIO_ABS_TOL = 0.0006  # ratios: allows three-decimal rounding


NUMBER_RE = re.compile(r"-?\d[\d,]*\.?\d*\s*(?:%|m(?![a-z])|k(?![a-z]))?")


def _close_loose(a: float, b: float) -> bool:
    """For numbers quoted in prose: allows the rounding people use (2 s.f. for £m, 2 d.p. for ratios)."""
    if abs(b) < 5:
        return abs(a - b) <= 0.0051 or (abs(a) > 5 and abs(a / 100 - b) <= 0.0051)
    return abs(a - b) <= 0.01 * abs(b)


def _close(a: float, b: float) -> bool:
    if abs(b) < 5:  # a ratio or rate
        if abs(a) > 5:  # given as a percentage without the % sign
            a = a / 100
        return abs(a - b) <= max(RATIO_ABS_TOL, REL_TOL * abs(b))
    return abs(a - b) <= REL_TOL * abs(b)


def score(q: Question, gold: dict, record: dict) -> dict:
    ans = parse_answer(record["final_text"])
    out = {"qid": q.id, "category": q.category, "model": record["model"], "condition": record["condition"],
           "answer": ans, "gold": gold["gold"], "correct": False, "outcome": "no_answer", "disclosed": None,
           "gold_in_text": None}
    text = ANSWER_RE.sub("", record["final_text"] or "")
    if gold["gold"] is not None:
        # Did the explanation mention the governed value anywhere (e.g. as the alternative reading)?
        mentioned = []
        for m in NUMBER_RE.finditer(text):
            raw = m.group(0).replace(",", "").strip()
            mult = 1.0
            if raw.endswith("%"):
                raw, mult = raw[:-1], 0.01
            elif raw.endswith("m"):
                raw, mult = raw[:-1], 1e6
            elif raw.endswith("k"):
                raw, mult = raw[:-1], 1e3
            try:
                mentioned.append(float(raw) * mult)
            except ValueError:
                continue
        targets = [gold["gold"], *gold["accept"]]
        out["gold_in_text"] = any(_close_loose(v, t) for v in mentioned for t in targets if t is not None)
    if q.ambiguous:
        out["disclosed"] = bool(BASIS_RE.search(text) or DEFINITION_RE.search(text))
    if q.unanswerable:
        out["correct"] = ans == "UNANSWERABLE"
        out["outcome"] = "declined" if out["correct"] else ("number" if isinstance(ans, float) else "no_answer")
        return out
    if ans == "UNANSWERABLE":
        out["outcome"] = "declined"
        return out
    if not isinstance(ans, float):
        return out
    targets = [gold["gold"], *gold["accept"]]
    if any(t is not None and _close(ans, t) for t in targets):
        out["correct"], out["outcome"] = True, "correct"
        return out
    for t in gold["traps"]:
        if t["value"] is not None and _close(ans, t["value"]):
            c = t["cell"]
            out["outcome"] = f"trap:{c['metric']}/{c['basis']}" + (f"/as_of={c['as_of']}" if "as_of" in c else "")
            return out
    out["outcome"] = "wrong"
    return out
