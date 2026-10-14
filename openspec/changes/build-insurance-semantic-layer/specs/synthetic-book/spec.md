## ADDED Requirements

### Requirement: Reproducible synthetic book
The generator SHALL produce identical data for the same seed and configuration, and SHALL contain no real policyholder, insurer or employer data.

#### Scenario: Same seed
- **WHEN** the generator runs twice with seed 7
- **THEN** every table hash matches

### Requirement: Accounting invariants hold
The generated data SHALL satisfy: claims are reported on or after the accident date and within a covered term; incurred equals paid plus case reserve at every valuation date; a term's earned premium at expiry equals its net written premium.

#### Scenario: Invariant test suite
- **WHEN** `pytest` runs on a generated book
- **THEN** all invariant tests pass
