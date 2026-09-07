-- S6 diagnostic only. SELECT-only; does not repair or recompute S3/S4.
-- Run with an authorized database session. Results are limited by its RLS visibility.
WITH audit AS (
  SELECT s.id AS signal_id, s.ticker, o.id AS outcome_id, o.outcome_status,
         o.supporting_evidence->>'profit_pct' AS canonical_profit_pct,
         o.supporting_evidence->>'assertion_id' AS referenced_assertion_id,
         a.id AS assertion_id, a.profit_pct AS assertion_profit_pct,
         CASE
           WHEN o.supporting_evidence->>'profit_pct' IS NOT NULL THEN 'CANONICAL_VALUE_PRESENT'
           WHEN a.id IS NULL THEN 'ASSERTION_NOT_FOUND_OR_NOT_VISIBLE'
           WHEN a.profit_pct IS NULL THEN 'ASSERTION_PROFIT_NULL'
           ELSE 'OUTCOME_EVIDENCE_NULL_ASSERTION_HAS_PROFIT'
         END AS gap
  FROM public.signal_outcomes o
  JOIN public.signals s ON s.id = o.signal_id
  LEFT JOIN public.signal_outcome_assertions a
    ON a.id::text = o.supporting_evidence->>'assertion_id'
   AND a.signal_id = o.signal_id AND a.user_id = o.user_id
  WHERE o.superseded_at IS NULL
    AND o.outcome_status IN ('TP_HIT', 'SL_HIT', 'EXPIRED')
)
SELECT *, count(*) OVER (PARTITION BY gap) AS gap_count
FROM audit
ORDER BY gap, ticker, signal_id;
