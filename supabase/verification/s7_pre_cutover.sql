-- IDXSY Signal S7 — PRE-CUTOVER VERIFICATION (READ ONLY)
-- Run after approved S3/S4 migrations are applied, before enabling live workflows.
-- This script performs SELECTs only and does not mutate canonical data.

WITH metrics AS (
    SELECT 'canonical signals' AS check_name, COUNT(*)::bigint AS actual, 1138::bigint AS baseline FROM public.signals
    UNION ALL SELECT 'source records', COUNT(*)::bigint, 3023::bigint FROM public.source_records
    UNION ALL SELECT 'source links', COUNT(*)::bigint, 2025::bigint FROM public.signal_source_links
    UNION ALL SELECT 'Telegram event sources', COUNT(*)::bigint, 480::bigint FROM public.source_records WHERE source_system='TELEGRAM' AND source_record_type='EVENT'
    UNION ALL SELECT 'matched signal events', COUNT(*)::bigint, 460::bigint FROM public.signal_events
    UNION ALL SELECT 'Zeta outcome assertions', COUNT(*)::bigint, 1000::bigint FROM public.signal_outcome_assertions
    UNION ALL SELECT 'active canonical outcomes', COUNT(*)::bigint, 1000::bigint FROM public.signal_outcomes WHERE superseded_at IS NULL
)
SELECT check_name, actual, baseline, CASE WHEN actual >= baseline THEN 'PASS' ELSE 'FAIL' END AS result FROM metrics ORDER BY check_name;

SELECT 'unresolved Telegram events' AS check_name, COUNT(*)::bigint AS actual, 20::bigint AS baseline,
       CASE WHEN COUNT(*) >= 20 THEN 'PASS' ELSE 'FAIL' END AS result
FROM public.reconciliation_items ri
JOIN public.source_records sr ON sr.id = ri.source_record_id
WHERE sr.source_system='TELEGRAM' AND ri.item_type='UNMATCHED_EVENT' AND ri.status <> 'ACCEPTED';

SELECT user_id, source_system, native_source_id, COUNT(*) AS duplicate_count FROM public.source_records GROUP BY user_id,source_system,native_source_id HAVING COUNT(*)>1;
SELECT source_record_id, COUNT(*) AS duplicate_count FROM public.signal_source_links GROUP BY source_record_id HAVING COUNT(*)>1;
SELECT source_record_id, COUNT(*) AS duplicate_count FROM public.signal_events WHERE source_record_id IS NOT NULL GROUP BY source_record_id HAVING COUNT(*)>1;
SELECT source_record_id, COUNT(*) AS duplicate_count FROM public.signal_outcome_assertions GROUP BY source_record_id HAVING COUNT(*)>1;
SELECT signal_id, COUNT(*) AS active_count FROM public.signal_outcomes WHERE superseded_at IS NULL GROUP BY signal_id HAVING COUNT(*)>1;

SELECT 'orphan source link -> signal' AS check_name, COUNT(*) AS actual, CASE WHEN COUNT(*)=0 THEN 'PASS' ELSE 'FAIL' END AS result
FROM public.signal_source_links l LEFT JOIN public.signals s ON s.id=l.signal_id WHERE s.id IS NULL
UNION ALL SELECT 'orphan source link -> source', COUNT(*), CASE WHEN COUNT(*)=0 THEN 'PASS' ELSE 'FAIL' END
FROM public.signal_source_links l LEFT JOIN public.source_records sr ON sr.id=l.source_record_id WHERE sr.id IS NULL
UNION ALL SELECT 'orphan event -> signal', COUNT(*), CASE WHEN COUNT(*)=0 THEN 'PASS' ELSE 'FAIL' END
FROM public.signal_events e LEFT JOIN public.signals s ON s.id=e.signal_id WHERE s.id IS NULL
UNION ALL SELECT 'orphan assertion -> signal', COUNT(*), CASE WHEN COUNT(*)=0 THEN 'PASS' ELSE 'FAIL' END
FROM public.signal_outcome_assertions a LEFT JOIN public.signals s ON s.id=a.signal_id WHERE s.id IS NULL
UNION ALL SELECT 'orphan assertion -> source', COUNT(*), CASE WHEN COUNT(*)=0 THEN 'PASS' ELSE 'FAIL' END
FROM public.signal_outcome_assertions a LEFT JOIN public.source_records sr ON sr.id=a.source_record_id WHERE sr.id IS NULL
UNION ALL SELECT 'orphan outcome -> signal', COUNT(*), CASE WHEN COUNT(*)=0 THEN 'PASS' ELSE 'FAIL' END
FROM public.signal_outcomes o LEFT JOIN public.signals s ON s.id=o.signal_id WHERE s.id IS NULL;

SELECT a.id,a.signal_id,sr.source_system,a.outcome_status FROM public.signal_outcome_assertions a JOIN public.source_records sr ON sr.id=a.source_record_id WHERE sr.source_system <> 'ZETA';
SELECT id,signal_id,outcome_status,resolver_version,authority FROM public.signal_outcomes WHERE superseded_at IS NULL AND resolver_version='zeta-authority-v1' AND authority <> 'ZETA';
SELECT id,signal_id,outcome_status FROM public.signal_outcome_assertions WHERE outcome_status NOT IN ('TP_HIT','SL_HIT','EXPIRED');

SELECT 'signal_events_source_unique_idx' AS object_name,
       CASE WHEN to_regclass('public.signal_events_source_unique_idx') IS NOT NULL THEN 'PASS' ELSE 'FAIL' END AS result
UNION ALL SELECT 'ensure_telegram_publication RPC', CASE WHEN to_regprocedure('public.ensure_telegram_publication(uuid,uuid,jsonb)') IS NOT NULL THEN 'PASS' ELSE 'FAIL' END
UNION ALL SELECT 'ensure_zeta_publication RPC', CASE WHEN to_regprocedure('public.ensure_zeta_publication(uuid,uuid,jsonb)') IS NOT NULL THEN 'PASS' ELSE 'FAIL' END
UNION ALL SELECT 'resolve_zeta_outcome RPC', CASE WHEN to_regprocedure('public.resolve_zeta_outcome(uuid,uuid)') IS NOT NULL THEN 'PASS' ELSE 'FAIL' END;

SELECT CASE WHEN to_regclass('public.trades_data') IS NOT NULL THEN 'PRESENT (expected during transition)' ELSE 'ABSENT' END AS trades_data_state;
