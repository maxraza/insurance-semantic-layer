## 1. Foundations

- [x] 1.1 Schema DDL for the synthetic book (grounded in OMG P&C / ACORD entity names)
- [x] 1.2 Generator: quotes, terms, premium transactions, claims, claim transactions, IBNR valuations, expenses; seeded; Parquet export
- [x] 1.3 Generator invariants as tests (premium earns to written at expiry, incurred = paid + case, reports after accidents, …)

## 2. Semantic layer

- [x] 2.1 YAML semantic model: entities, dimensions, measures (kinds), metrics (simple, ratio, derived)
- [x] 2.2 Compiler → DuckDB SQL; period grid; basis + as_of; conformed-dimension checks
- [x] 2.3 Reference implementation in plain Python (no SQL) to cross-check the compiler on random queries
- [x] 2.4 CLI: `isl generate`, `isl query`, `isl sql`, `isl explain`

## 3. Benchmark

- [x] 3.1 ISL-Bench questions (≥60) with categories, gold queries, ambiguity labels and alternative interpretations
- [x] 3.2 Harness: conditions A/B/C, response cache, scoring, error taxonomy
- [x] 3.3 Run on two models (Haiku 5.5, Sonnet 5.5); store raw outputs and results tables
- [ ] 3.4 Optional: add Opus 5.5 (about $5 with caching), Max to approve

## 4. Paper & outputs

- [x] 4.1 LaTeX paper: formal model, implementation, benchmark, results, limitations
- [x] 4.2 CI builds the PDF; README with reproduction steps (CI unverified until the repo is on GitHub)
- [x] 4.3 Export simulator fixture (semantic model JSON + compiled SQL samples) for hraza.online
- [ ] 4.4 Max approves; publish the repo (public) and the paper
