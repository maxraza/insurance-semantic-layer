## ADDED Requirements

### Requirement: Basis and valuation date are explicit
Every metric query SHALL specify a time basis (calendar, accident, policy) and a valuation date; the compiler SHALL reject queries missing either.

#### Scenario: Missing basis
- **WHEN** a query asks for `loss_ratio_incurred` by year without a basis
- **THEN** the compiler raises an error naming the allowed bases

#### Scenario: Valuation date hides later facts
- **WHEN** accident-year 2023 incurred loss is queried as of 2023-12-31 and again as of 2025-12-31
- **THEN** the second value includes claim transactions dated 2024–2025 and the first does not

### Requirement: Earned premium is computed by earning, not by row filtering
The system SHALL earn each premium transaction pro-rata over its cover interval and count only the portion inside the period and on or before the valuation date.

#### Scenario: Annual policy written mid-year
- **WHEN** a 365-day term with written premium 730 incepts on 2024-07-01
- **THEN** calendar-2024 earned premium from it is 368 (184 days) and calendar-2025 earned is 362

### Requirement: Ratios are ratios of sums
The system SHALL compute ratio metrics by dividing aggregated numerators by aggregated denominators at the requested grain.

#### Scenario: Yearly loss ratio from quarters
- **WHEN** a yearly loss ratio is requested
- **THEN** it equals yearly losses / yearly earned premium, not the mean of the quarterly ratios

### Requirement: Dimensions are conformed
The system SHALL reject group-by dimensions that are not available on every measure the metric needs.

#### Scenario: Loss ratio by peril
- **WHEN** `loss_ratio_incurred` is grouped by `peril`
- **THEN** the compiler raises a non-conformed-dimension error, while `incurred_loss` by `peril` succeeds
