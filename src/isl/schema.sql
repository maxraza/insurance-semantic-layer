-- Synthetic personal-motor book. Entity names follow common P&C data-model usage
-- (policy, policy term, premium transaction, claim, claim transaction, valuation).
--
-- Every table carries two kinds of dates:
--   business dates  (inception, effective, accident) decide which period a fact belongs to;
--   knowledge dates (booked, reported, transaction, valuation) decide when the insurer knew it.
-- A metric is only well defined once both a time basis and a valuation date are fixed.

CREATE TABLE quote (
    quote_id          BIGINT PRIMARY KEY,
    quote_date        DATE    NOT NULL,      -- knowledge + business date
    channel           VARCHAR NOT NULL,      -- PCW | DIRECT | BROKER
    region            VARCHAR NOT NULL,
    vehicle_group     VARCHAR NOT NULL,      -- A (low risk) .. E (high risk)
    driver_age_band   VARCHAR NOT NULL,      -- 17-24 | 25-39 | 40-59 | 60+
    quoted_premium    DECIMAL(12,2) NOT NULL,
    pcw_rank          INTEGER,               -- position on the comparison site; NULL off-PCW
    bound_term_id     BIGINT                 -- NULL when the quote did not convert
);

CREATE TABLE policy (
    policy_id         BIGINT PRIMARY KEY,
    first_inception   DATE    NOT NULL,
    channel           VARCHAR NOT NULL,      -- acquisition channel
    region            VARCHAR NOT NULL
);

CREATE TABLE policy_term (
    term_id           BIGINT PRIMARY KEY,
    policy_id         BIGINT  NOT NULL REFERENCES policy(policy_id),
    term_number       INTEGER NOT NULL,      -- 1 = new business, >1 = renewal
    bound_date        DATE    NOT NULL,      -- knowledge date of the term
    inception_date    DATE    NOT NULL,      -- policy-year basis date
    expiry_date       DATE    NOT NULL,      -- exclusive: cover is [inception, expiry)
    cancel_date       DATE,                  -- effective date of a mid-term cancellation
    channel           VARCHAR NOT NULL,
    region            VARCHAR NOT NULL,
    vehicle_group     VARCHAR NOT NULL,
    driver_age_band   VARCHAR NOT NULL,
    ncd_years         INTEGER NOT NULL       -- no-claims discount years at inception
);

-- Written premium and exposure, one row per accounting event.
-- Each row earns pro-rata over [effective_date, cover_end_date).
CREATE TABLE premium_txn (
    premium_txn_id    BIGINT PRIMARY KEY,
    term_id           BIGINT  NOT NULL REFERENCES policy_term(term_id),
    txn_type          VARCHAR NOT NULL,      -- NEW | RENEWAL | MTA | CANCEL
    booked_date       DATE    NOT NULL,      -- calendar-basis / knowledge date
    effective_date    DATE    NOT NULL,
    cover_end_date    DATE    NOT NULL,      -- = term expiry
    written_premium   DECIMAL(12,2) NOT NULL,-- negative for return premium
    exposure_units    DECIMAL(9,6)  NOT NULL,-- car-years written: 1 on NEW/RENEWAL, minus the unexpired
                                             -- fraction on CANCEL (a cancellation 3 months early is -0.25)
    vehicle_delta     INTEGER       NOT NULL -- vehicles on cover: +1 NEW/RENEWAL, -1 CANCEL, 0 MTA
);

CREATE TABLE renewal_offer (
    offer_id          BIGINT PRIMARY KEY,
    expiring_term_id  BIGINT  NOT NULL REFERENCES policy_term(term_id),
    offer_date        DATE    NOT NULL,
    due_date          DATE    NOT NULL,      -- = expiring term's expiry date
    prior_premium     DECIMAL(12,2) NOT NULL,
    offered_premium   DECIMAL(12,2) NOT NULL,
    enbp              DECIMAL(12,2) NOT NULL,-- equivalent new-business price (renewals capped at it from 2022)
    renewed_term_id   BIGINT                 -- NULL = lapsed (or not yet decided)
);

CREATE TABLE claim (
    claim_id          BIGINT PRIMARY KEY,
    term_id           BIGINT  NOT NULL REFERENCES policy_term(term_id),
    peril             VARCHAR NOT NULL,      -- OWN_DAMAGE | THIRD_PARTY_PROPERTY | BODILY_INJURY | WINDSCREEN | THEFT
    accident_date     DATE    NOT NULL,      -- accident-year basis date
    report_date       DATE    NOT NULL,      -- knowledge date of the claim
    closed_date       DATE                   -- NULL while open
);

-- Movements on a claim. Paid = PAYMENT + RECOVERY; case reserve = running sum of RESERVE;
-- incurred = paid + case reserve.
CREATE TABLE claim_txn (
    claim_txn_id      BIGINT PRIMARY KEY,
    claim_id          BIGINT  NOT NULL REFERENCES claim(claim_id),
    txn_date          DATE    NOT NULL,      -- calendar-basis / knowledge date
    txn_type          VARCHAR NOT NULL,      -- PAYMENT | RECOVERY | RESERVE
    cost_type         VARCHAR NOT NULL,      -- INDEMNITY | ALAE
    amount            DECIMAL(14,2) NOT NULL -- RESERVE rows are changes in case reserve
);

-- Actuarial IBNR booked at each quarter-end (chain ladder on data known at that date).
CREATE TABLE ibnr_valuation (
    valuation_date    DATE    NOT NULL,      -- knowledge date
    accident_year     INTEGER NOT NULL,
    ibnr              DECIMAL(14,2) NOT NULL,
    PRIMARY KEY (valuation_date, accident_year)
);

CREATE TABLE expense (
    expense_id        BIGINT PRIMARY KEY,
    expense_month     DATE    NOT NULL,      -- first day of the month incurred
    booked_date       DATE    NOT NULL,
    expense_type      VARCHAR NOT NULL,      -- ACQUISITION | COMMISSION | ADMIN
    channel           VARCHAR,               -- NULL for overheads
    amount            DECIMAL(12,2) NOT NULL
);
