## Why

"What was our loss ratio last year?" has at least six defensible answers in a P&C insurer: calendar-year vs accident-year vs policy-year, paid vs incurred vs ultimate, with or without LAE, valued at which date. Humans resolve this with tribal knowledge; LLMs answering over raw tables resolve it silently and inconsistently. Generic semantic layers (MetricFlow, Cube, LookML) model a metric with one time dimension and no valuation date, so they can't express the distinction either.

This repo is a clean-room, open-source reference: an insurance semantic layer where **time basis** and **valuation (as-of) date** are first-class query parameters, a synthetic personal-motor book to run it on, and a benchmark measuring how LLMs answer insurance questions with and without it. It anchors an academic paper and an interactive simulator on hraza.online.

## What Changes

- `isl` Python package: semantic model (YAML), compiler from metric queries to DuckDB SQL, CLI.
- Synthetic personal-motor generator (policies, terms, premium transactions, quotes incl. price-comparison channel, claims, claim transactions, IBNR valuations, expenses). Seeded, reproducible, no real data.
- ISL-Bench: natural-language questions with gold metric queries and labelled ambiguity; harness comparing raw text-to-SQL, glossary-augmented text-to-SQL, and semantic-layer tool calls.
- Paper (`paper/`, LaTeX, built in CI) and an exported fixture for the web simulator.

## Capabilities

### New Capabilities
- `metric-semantics`: basis-aware, valuation-aware metric definitions and their compilation to SQL.
- `synthetic-book`: the reproducible synthetic motor book.
- `llm-eval`: the benchmark and evaluation harness.

## Impact

- New public repo (Apache-2.0) once Max approves; nothing in it derives from any employer's data, code, or documents.
- Uses Max's Anthropic key for the evaluation runs (small, capped).
