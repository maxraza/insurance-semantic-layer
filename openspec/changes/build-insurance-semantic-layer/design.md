## Context

Insurance numbers are bitemporal. Each record has *business* dates (accident date, policy inception, cover period) and a *knowledge* date (booking date, report date, transaction date, valuation date). A metric is only defined once you fix:

1. **Time basis** — which business date assigns a record to a period:
   - calendar (CY): accounting/transaction date (and earning date for premium),
   - accident (AY): claim accident date (premium side: earned in the period),
   - policy / underwriting (PY): term inception date, both sides.
2. **Valuation date** (`as_of`) — only facts known on or before it count.
3. **Measure variant** — paid / incurred / ultimate; with or without ALAE; written / earned.

## Goals / Non-Goals

**Goals:** a small, readable reference implementation; correct actuarial semantics; a reproducible benchmark; outputs the web simulator can reuse.
**Non-Goals:** a production BI tool, reinsurance (gross only), multi-currency, IFRS 17 measurement, competing with MetricFlow/Cube.

## Decisions

1. **Query shape.** `MetricQuery(metrics, basis, grain, as_of, group_by, filters, period_range)`. `basis` and `as_of` are required: the API forces the choice that raw SQL lets you skip.
2. **Measure kinds** (the only things the compiler knows):
   - `flow`: sum of transaction amounts; period by a basis-specific date; filtered by knowledge date ≤ as_of.
   - `earned`: pro-rata earning of each premium/exposure transaction over its cover interval, intersected with the period and with (−∞, as_of].
   - `snapshot`: balance at min(period end, as_of) (case reserve, unearned premium, open claims, in-force) — semi-additive over time.
   - `valuation`: latest booked actuarial estimate (IBNR) with valuation date ≤ as_of.
   - `count`: distinct entities whose basis date is in the period and knowledge date ≤ as_of.
3. **Ratios are ratios of sums.** Numerator and denominator are computed at the requested grain and divided last. Averaging ratios is never generated.
4. **Conformed dimensions.** Each measure declares the dimensions it can be cut by. Asking for `loss_ratio by peril` is an error (premium isn't split by peril), not a silent wrong join.
5. **Basis-specific pairing.** AY loss ratio = AY losses / CY earned premium of the same period (standard actuarial pairing); PY = PY losses / PY earned premium. Encoded per metric, not left to the caller.
6. **Compiler emits one CTE per measure family** over a generated period grid, joined on period + group-by keys. Output is plain DuckDB SQL that a reviewer can read.
7. **Synthetic data is generated forward in time** (quotes → binds → terms → claims → development → IBNR at quarter-ends) so as-of slicing is genuine, not simulated by filtering a static table.
8. **LLM conditions:** (A) raw schema → SQL, (B) schema + prose glossary → SQL, (C) semantic-layer tool (`query_metrics` JSON schema generated from the YAML). Scoring is numeric (relative tolerance 0.5%) against the gold interpretation(s); ambiguous questions accept any labelled interpretation but also score whether the answer states its basis and valuation date.

## Risks / Trade-offs

- [Synthetic data is too clean] → inject real-world mess: mid-term cancellations, MTAs, late-reported claims, reserve strengthening, nil claims, recoveries, PCW-driven quote volume.
- [Benchmark built by the author of the method] → publish all questions, gold queries, prompts and raw model outputs; include questions where the semantic layer is *not* needed.
- [LLM cost] → cap runs (questions × conditions × models ≤ ~600 calls), cache responses on disk.
