"""`isl` command line: generate the book, inspect the model, compile and run metric queries."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import duckdb
import typer
from rich.console import Console
from rich.table import Table

from .compiler import Compiler, MetricQuery, SemanticError, run
from .generate import Config, generate, write_duckdb

app = typer.Typer(add_completion=False, help="Insurance semantic layer.")
console = Console()
DB = Path("data/book.duckdb")


def _query(metrics: str, basis: str, as_of: str, grain: str, by: str | None, where: list[str] | None,
           since: str | None) -> MetricQuery:
    filters = {}
    for w in where or []:
        k, v = w.split("=", 1)
        filters[k] = v.split(",") if "," in v else v
    return MetricQuery(metrics=metrics.split(","), basis=basis, as_of=date.fromisoformat(as_of), grain=grain,
                       group_by=by.split(",") if by else [], filters=filters,
                       period_from=date.fromisoformat(since) if since else None)


@app.command("generate")
def generate_cmd(out: Path = DB, seed: int = 7, quotes_per_month: int = 9000) -> None:
    """Generate the synthetic book into a DuckDB file."""
    tables = generate(Config(seed=seed, quotes_per_month=quotes_per_month))
    write_duckdb(tables, out)
    for k, v in tables.items():
        console.print(f"{k:18} {len(v):>9,}")
    console.print(f"[green]wrote {out}")


@app.command()
def metrics() -> None:
    """List metrics and measures with their bases and dimensions."""
    model = Compiler().model
    t = Table("name", "kind", "bases", "dimensions", "description")
    for name in model.names():
        t.add_row(name, "metric" if name in model.metrics else "measure", ",".join(model.bases(name)),
                  ",".join(model.conformed_dimensions(name)), model.describe(name)[:80])
    console.print(t)


@app.command()
def sql(metrics: str, basis: str = typer.Option(...), as_of: str = typer.Option(...), grain: str = "year",
        by: str = None, where: list[str] = typer.Option(None), since: str = None) -> None:
    """Print the SQL for a metric query."""
    try:
        print(Compiler().compile(_query(metrics, basis, as_of, grain, by, where, since)))
    except SemanticError as e:
        console.print(f"[red]{e}")
        raise typer.Exit(2) from e


@app.command()
def query(metrics: str, basis: str = typer.Option(...), as_of: str = typer.Option(...), grain: str = "year",
          by: str = None, where: list[str] = typer.Option(None), since: str = None, db: Path = DB,
          as_json: bool = typer.Option(False, "--json")) -> None:
    """Run a metric query, e.g. `isl query loss_ratio_ultimate --basis accident --as-of 2025-12-31`."""
    try:
        cols, rows = run(duckdb.connect(str(db), read_only=True), _query(metrics, basis, as_of, grain, by, where, since))
    except SemanticError as e:
        console.print(f"[red]{e}")
        raise typer.Exit(2) from e
    if as_json:
        print(json.dumps([dict(zip(cols, r)) for r in rows], default=str))
        return
    t = Table(*cols)
    for r in rows:
        t.add_row(*[f"{v:,.4f}" if isinstance(v, float) and abs(v) < 10 else
                    f"{v:,.0f}" if isinstance(v, float) else str(v) for v in r])
    console.print(t)


@app.command()
def bench(models: str = "claude-haiku-5-5", conditions: str = "raw,docs,semantic", only: str = None,
          workers: int = 4, effort: str = "medium", db: Path = DB, env_file: Path = None) -> None:
    """Run ISL-Bench (cached) and print the score table."""
    import os
    from concurrent.futures import ThreadPoolExecutor

    import anthropic

    from . import bench as B

    if env_file and not os.environ.get("ANTHROPIC_API_KEY"):
        for line in env_file.read_text(encoding="utf-8").splitlines():
            if line.startswith("ANTHROPIC_API_KEY="):
                os.environ["ANTHROPIC_API_KEY"] = line.split("=", 1)[1].strip().strip('"')
    import functools

    @functools.cache
    def client():  # created on first cache miss, so a fully cached run needs no API key
        return anthropic.Anthropic(max_retries=6)
    qs, defaults = B.load_questions()
    if only:
        qs = [q for q in qs if q.id in only.split(",")]
    sem = duckdb.connect(str(db), read_only=True)
    pub = B.public_db(db, db.with_name("book_public.duckdb"))
    gold = {q.id: B.Gold(sem, defaults).resolve(q) for q in qs}
    jobs = [(m, c, q) for m in models.split(",") for c in conditions.split(",") for q in qs]

    def one(job):
        m, c, q = job
        sql = duckdb.connect(str(pub), read_only=True)  # one connection per thread
        sem_t = duckdb.connect(str(db), read_only=True)
        try:
            rec = B.ask(client, m, c, q, sql, sem_t, effort=effort)
        finally:
            sql.close()
            sem_t.close()
        return B.score(q, gold[q.id], rec), rec["usage"]

    with ThreadPoolExecutor(workers) as ex:
        results = list(ex.map(one, jobs))
    out = Path("results") / "scores.jsonl"
    out.parent.mkdir(exist_ok=True)
    with out.open("w", encoding="utf-8") as f:
        for s, u in results:
            f.write(json.dumps({**s, "usage": u}) + "\n")
    t = Table("model", "condition", "n", "correct", "declined", "trap", "wrong", "no answer", "disclosed (amb.)")
    keys = sorted({(s["model"], s["condition"]) for s, _ in results}, key=lambda k: (k[0], CONDS.index(k[1])))
    for k in keys:
        rs = [s for s, _ in results if (s["model"], s["condition"]) == k]
        amb = [s for s in rs if s["disclosed"] is not None]
        t.add_row(k[0], k[1], str(len(rs)), f"{sum(s['correct'] for s in rs) / len(rs):.1%}",
                  str(sum(s["outcome"] == "declined" for s in rs)),
                  str(sum(s["outcome"].startswith("trap") for s in rs)),
                  str(sum(s["outcome"] in ("wrong", "number") for s in rs)),
                  str(sum(s["outcome"] == "no_answer" for s in rs)),
                  f"{sum(s['disclosed'] for s in amb)}/{len(amb)}" if amb else "-")
    console.print(t)
    console.print(f"wrote {out}")


CONDS = ["raw", "docs", "semantic"]


if __name__ == "__main__":
    app()
