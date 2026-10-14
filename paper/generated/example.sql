WITH grid AS (
  SELECT p_start::DATE AS period, (p_start + INTERVAL 1 YEAR)::DATE AS p_end
  FROM range(date_trunc('year', DATE '2019-01-01'), DATE '2025-12-31' + INTERVAL 1 DAY, INTERVAL 1 YEAR) t(p_start)
),
earned_premium AS (
  SELECT g.period, SUM(p.written_premium * GREATEST(date_diff('day', GREATEST(p.effective_date, g.period), LEAST(p.cover_end_date, g.p_end, DATE '2025-12-31' + 1)), 0) / date_diff('day', p.effective_date, p.cover_end_date)) AS value
  FROM grid g JOIN premium_txn p JOIN policy_term t ON t.term_id = p.term_id ON p.booked_date <= DATE '2025-12-31' AND p.effective_date < g.p_end AND p.cover_end_date > g.period AND g.period <= DATE '2025-12-31'
  GROUP BY ALL
),
ibnr AS (
  SELECT make_date(v.accident_year, 1, 1) AS period, SUM(v.ibnr) AS value
  FROM ibnr_valuation v
  WHERE v.valuation_date = (SELECT max(v.valuation_date) FROM ibnr_valuation v WHERE v.valuation_date <= DATE '2025-12-31')
  GROUP BY ALL
),
reported_loss AS (
  SELECT date_trunc('year', c.accident_date)::DATE AS period, SUM(x.amount) AS value
  FROM claim_txn x JOIN claim c ON c.claim_id = x.claim_id JOIN policy_term t ON t.term_id = c.term_id
  WHERE x.txn_date <= DATE '2025-12-31' AND x.cost_type = 'INDEMNITY'
  GROUP BY ALL
),
keys AS (
  SELECT period FROM earned_premium
  UNION
  SELECT period FROM ibnr
  UNION
  SELECT period FROM reported_loss
),
m AS (
  SELECT keys.*,
    earned_premium.value::DOUBLE AS earned_premium,
    ibnr.value::DOUBLE AS ibnr,
    reported_loss.value::DOUBLE AS reported_loss
  FROM keys
LEFT JOIN earned_premium USING (period)
LEFT JOIN ibnr USING (period)
LEFT JOIN reported_loss USING (period)
)
SELECT
  CAST(year(m.period) AS VARCHAR) AS period,
  ((COALESCE(m.reported_loss, 0) + COALESCE(m.ibnr, 0))) / NULLIF(COALESCE(m.earned_premium, 0), 0) AS loss_ratio_ultimate
FROM m
WHERE period <= DATE '2025-12-31'
ORDER BY period