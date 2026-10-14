# Insurance Semantic Layer

**"What was the 2024 loss ratio?"** On the synthetic motor book in this repo, that question has ten defensible
answers, from 0.52 to 0.93. They depend on which date assigns a loss to the year (calendar, accident or policy
year), which loss measure is meant (paid, reported, ultimate, with or without ALAE), and the date the books are
read at.

This repo makes those choices explicit. It contains:

- **A semantic layer** (`semantic/motor.yml` → `src/isl/compiler.py`). Every measure is defined *per time basis*,
  and every query must name a basis and a valuation date. Four measure kinds cover P&C metrics: flow, earned,
  snapshot and valuation. Ratios are always ratios of sums. Undefined combinations are refused with a reason,
  for example a loss ratio by peril, or an ultimate loss ratio by policy year when IBNR is booked by accident year.
- **A synthetic UK personal-motor book** (`src/isl/generate.py`), generated forward in time from a seed:
  - price-comparison quoting, renewals capped at the equivalent new-business price;
  - mid-term changes and cancellations;
  - late-reported claims, case reserving, recoveries;
  - quarterly chain-ladder IBNR.

  Facts after the horizon are withheld. The true final cost of every accident is kept for checking estimates.
- **ISL-Bench** (`bench/questions.yml`, `src/isl/bench.py`): 62 questions comparing LLMs answering from raw
  tables, from tables plus a glossary, and through the semantic layer as a tool.
- **A paper** (`paper/`) and the data behind an interactive simulator on
  [hraza.online/labs](https://hraza.online/labs) (`exports/simulator.json`).

Everything is synthetic. No real policyholder, insurer or employer data is used.

## Quick start

```bash
uv sync
uv run isl generate                       # build data/book.duckdb from seed 7 (about 20 s)
uv run isl metrics                        # catalogue: metrics, bases, dimensions
uv run isl query loss_ratio_ultimate,loss_ratio_reported --basis accident --as-of 2025-12-31
uv run isl query loss_ratio_reported --basis accident --as-of 2023-12-31 --since 2021-01-01
uv run isl sql loss_ratio_ultimate --basis accident --as-of 2025-12-31     # the compiled SQL
uv run isl query loss_ratio_reported --basis accident --as-of 2025-12-31 --by peril   # refused, with a reason
```

## Tests

```bash
uv run pytest
```

There are 208 tests:
- **Accounting invariants of the book:** reports follow accidents within cover, case reserves never go negative, nothing is known after the horizon, the renewal price cap holds, expired cohorts earn their written premium, and earned exposure reconciles with covered days.
- **The specification's scenarios:** a mid-year policy earns 368 of 730 in its first year, valuation dates hide later facts, ratios are ratios of sums, and non-conformed dimensions are refused.
- **An exhaustive cross-check of the SQL compiler** against an independent pure-Python reference implementation (`src/isl/reference.py`), covering every measure and metric on every basis.

## Benchmark

```bash
uv run isl bench --models claude-haiku-5-5,claude-sonnet-5-5
```

Responses are cached in `results/cache/`, so this reproduces `results/scores.jsonl` without API calls. Delete a
cache file to re-ask that question; that needs `ANTHROPIC_API_KEY`. Every wrong answer is classified by hand
in `results/error_taxonomy.yml`.

Results (62 questions, accuracy against the governed definitions):

| | Raw tables + SQL | Tables + glossary + SQL | Semantic layer |
|---|---|---|---|
| Claude Haiku 5.5 | 77% | 98% | 100% |
| Claude Sonnet 5.5 | 84% | 98% | 100% |

Almost every raw-table error is a house convention a schema can't reveal, such as whether losses include
ALAE or whether paid is net of recoveries. With those counted as correct, raw accuracy is 98%. The paper
discusses what that means, and two defects the models found in our own artefacts.

## Paper

```bash
uv run python scripts/paper_assets.py      # figures, tables and every quoted number
cd paper && tectonic main.tex              # or latexmk -pdf main.tex
```

`uv run python scripts/arxiv_bundle.py` packs the sources and the compiled bibliography into
`dist/arxiv.tar.gz` for arXiv. CI compiles the paper and that bundle with pdfLaTeX on every push
(`.github/workflows/ci.yml`).

## Layout

```
semantic/motor.yml        the semantic model
src/isl/schema.sql        tables (business dates vs knowledge dates on every row)
src/isl/generate.py       synthetic book
src/isl/compiler.py       metric query -> DuckDB SQL
src/isl/reference.py      independent Python implementation (for tests)
src/isl/bench.py          benchmark harness and scoring
bench/questions.yml       ISL-Bench
results/                  cached responses, scores, error taxonomy
paper/                    LaTeX source; generated/ and figures/ come from scripts/paper_assets.py
openspec/                 the change specification this repo was built against
```

## Licence

Apache-2.0. Author: Hassan (Max) Raza, [hraza.online](https://hraza.online).
