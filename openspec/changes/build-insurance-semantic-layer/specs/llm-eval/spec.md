## ADDED Requirements

### Requirement: Comparable conditions
The harness SHALL ask every question under each condition (raw SQL, glossary SQL, semantic tool) with the same model, temperature 0 and the same data, and SHALL cache raw responses to disk.

#### Scenario: Re-run without network
- **WHEN** the harness re-runs with a warm cache
- **THEN** it reproduces the results table without API calls

### Requirement: Scoring handles ambiguity
For ambiguous questions the harness SHALL accept any labelled interpretation and SHALL separately record whether the answer states its time basis and valuation date.

#### Scenario: Silent basis choice
- **WHEN** a model answers an ambiguous loss-ratio question with the accident-year value and no stated basis
- **THEN** it is scored correct on value and not-disclosed on assumptions
