-- IDXSY Signal Rebuild — S3+S4 canonical augmentation bundle
-- Target: NEW canonical Supabase reconstructed by Importer.
-- This is NOT a bootstrap migration and must never be applied to legacy Signal Supabase.
--
-- Run once in Supabase SQL Editor after reviewing the preflight query below.

-- ==========================================================================
-- PRE-FLIGHT: MUST RETURN ZERO ROWS BEFORE CONTINUING
-- ==========================================================================
SELECT source_record_id, COUNT(*) AS duplicate_count
FROM public.signal_events
WHERE source_record_id IS NOT NULL
GROUP BY source_record_id
HAVING COUNT(*) > 1;

-- ==========================================================================
-- S3 GAP-001: one source event -> at most one canonical signal_event
-- ==========================================================================
CREATE UNIQUE INDEX IF NOT EXISTS signal_events_source_unique_idx
ON public.signal_events (source_record_id)
WHERE source_record_id IS NOT NULL;

-- ==========================================================================
-- S3 GAP-002: atomic Telegram publication + provenance link
-- ==========================================================================
CREATE OR REPLACE FUNCTION public.ensure_telegram_publication(
    p_user_id uuid,
    p_source_record_id uuid,
    p_signal jsonb
)
RETURNS uuid
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public
AS $$
DECLARE
    v_source public.source_records%ROWTYPE;
    v_signal_id uuid;
BEGIN
    SELECT *
      INTO v_source
      FROM public.source_records
     WHERE id = p_source_record_id
       AND user_id = p_user_id
       AND source_system = 'TELEGRAM'
     FOR UPDATE;

    IF NOT FOUND THEN
        RAISE EXCEPTION 'Telegram source_record % not found for user %', p_source_record_id, p_user_id;
    END IF;

    IF NULLIF(p_signal->>'ticker', '') IS NULL
       OR NULLIF(p_signal->>'signal_timestamp', '') IS NULL
       OR NULLIF(p_signal->>'market_date', '') IS NULL THEN
        RAISE EXCEPTION 'ticker, signal_timestamp, market_date are required for Telegram publication %', p_source_record_id;
    END IF;

    SELECT signal_id INTO v_signal_id
    FROM public.signal_source_links
    WHERE source_record_id = p_source_record_id;

    IF v_signal_id IS NOT NULL THEN
        UPDATE public.signals
           SET ticker = p_signal->>'ticker',
               signal_type = NULLIF(p_signal->>'signal_type', ''),
               signal_timestamp = (p_signal->>'signal_timestamp')::timestamptz,
               market_date = (p_signal->>'market_date')::date,
               entry_price = NULLIF(p_signal->>'entry_price', '')::numeric,
               tp1_price = NULLIF(p_signal->>'tp1_price', '')::numeric,
               tp1_pct = NULLIF(p_signal->>'tp1_pct', '')::numeric,
               tp2_price = NULLIF(p_signal->>'tp2_price', '')::numeric,
               tp2_pct = NULLIF(p_signal->>'tp2_pct', '')::numeric,
               sl_default_price = NULLIF(p_signal->>'sl_default_price', '')::numeric,
               sl_default_pct = NULLIF(p_signal->>'sl_default_pct', '')::numeric,
               sl_moderat_price = NULLIF(p_signal->>'sl_moderat_price', '')::numeric,
               sl_moderat_pct = NULLIF(p_signal->>'sl_moderat_pct', '')::numeric,
               sl_konservatif_price = NULLIF(p_signal->>'sl_konservatif_price', '')::numeric,
               sl_konservatif_pct = NULLIF(p_signal->>'sl_konservatif_pct', '')::numeric,
               confidence_score = NULLIF(p_signal->>'confidence_score', '')::numeric,
               confidence_label = NULLIF(p_signal->>'confidence_label', ''),
               detail = COALESCE(p_signal->'detail', '{}'::jsonb)
         WHERE id = v_signal_id
           AND user_id = p_user_id;

        IF NOT FOUND THEN
            RAISE EXCEPTION 'Linked signal % not found for Telegram source %', v_signal_id, p_source_record_id;
        END IF;
        RETURN v_signal_id;
    END IF;

    INSERT INTO public.signals (
        user_id,ticker,signal_type,signal_timestamp,market_date,entry_price,
        tp1_price,tp1_pct,tp2_price,tp2_pct,sl_default_price,sl_default_pct,
        sl_moderat_price,sl_moderat_pct,sl_konservatif_price,sl_konservatif_pct,
        confidence_score,confidence_label,detail
    ) VALUES (
        p_user_id,p_signal->>'ticker',NULLIF(p_signal->>'signal_type',''),
        (p_signal->>'signal_timestamp')::timestamptz,(p_signal->>'market_date')::date,
        NULLIF(p_signal->>'entry_price','')::numeric,NULLIF(p_signal->>'tp1_price','')::numeric,
        NULLIF(p_signal->>'tp1_pct','')::numeric,NULLIF(p_signal->>'tp2_price','')::numeric,
        NULLIF(p_signal->>'tp2_pct','')::numeric,NULLIF(p_signal->>'sl_default_price','')::numeric,
        NULLIF(p_signal->>'sl_default_pct','')::numeric,NULLIF(p_signal->>'sl_moderat_price','')::numeric,
        NULLIF(p_signal->>'sl_moderat_pct','')::numeric,NULLIF(p_signal->>'sl_konservatif_price','')::numeric,
        NULLIF(p_signal->>'sl_konservatif_pct','')::numeric,NULLIF(p_signal->>'confidence_score','')::numeric,
        NULLIF(p_signal->>'confidence_label',''),COALESCE(p_signal->'detail','{}'::jsonb)
    ) RETURNING id INTO v_signal_id;

    INSERT INTO public.signal_source_links (
        user_id,signal_id,source_record_id,match_method,match_confidence,evidence,review_status
    ) VALUES (
        p_user_id,v_signal_id,p_source_record_id,'NATIVE_TELEGRAM_PUBLICATION','STRONG',
        jsonb_build_object('source_system','TELEGRAM','native_source_id',v_source.native_source_id,'parser_version',v_source.parser_version),
        'AUTO_ACCEPTED'
    );
    RETURN v_signal_id;
END;
$$;

REVOKE ALL ON FUNCTION public.ensure_telegram_publication(uuid, uuid, jsonb) FROM PUBLIC;
REVOKE ALL ON FUNCTION public.ensure_telegram_publication(uuid, uuid, jsonb) FROM anon;
REVOKE ALL ON FUNCTION public.ensure_telegram_publication(uuid, uuid, jsonb) FROM authenticated;
GRANT EXECUTE ON FUNCTION public.ensure_telegram_publication(uuid, uuid, jsonb) TO service_role;

-- ==========================================================================
-- S4: atomic ZETA_ONLY publication
-- ==========================================================================
CREATE OR REPLACE FUNCTION public.ensure_zeta_publication(
    p_user_id uuid,
    p_source_record_id uuid,
    p_signal jsonb
)
RETURNS uuid
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public
AS $$
DECLARE
    v_source public.source_records%ROWTYPE;
    v_signal_id uuid;
    v_has_telegram_provenance boolean := false;
BEGIN
    SELECT * INTO v_source
    FROM public.source_records
    WHERE id = p_source_record_id AND user_id = p_user_id AND source_system = 'ZETA'
    FOR UPDATE;
    IF NOT FOUND THEN RAISE EXCEPTION 'Zeta source_record not found or wrong owner/source: %', p_source_record_id; END IF;

    SELECT signal_id INTO v_signal_id
    FROM public.signal_source_links
    WHERE user_id = p_user_id AND source_record_id = p_source_record_id
    LIMIT 1;

    IF v_signal_id IS NOT NULL THEN
        SELECT EXISTS (
            SELECT 1 FROM public.signal_source_links ssl
            JOIN public.source_records sr ON sr.id = ssl.source_record_id
            WHERE ssl.user_id=p_user_id AND ssl.signal_id=v_signal_id
              AND sr.user_id=p_user_id AND sr.source_system='TELEGRAM' AND sr.source_record_type='SIGNAL'
        ) INTO v_has_telegram_provenance;
        IF v_has_telegram_provenance THEN RETURN v_signal_id; END IF;

        UPDATE public.signals SET
            ticker=p_signal->>'ticker', signal_type=NULLIF(p_signal->>'signal_type',''),
            signal_timestamp=(p_signal->>'signal_timestamp')::timestamptz,
            market_date=(p_signal->>'market_date')::date,
            entry_price=NULLIF(p_signal->>'entry_price','')::numeric,
            tp1_price=NULLIF(p_signal->>'tp1_price','')::numeric,
            tp1_pct=NULLIF(p_signal->>'tp1_pct','')::numeric,
            tp2_price=NULLIF(p_signal->>'tp2_price','')::numeric,
            tp2_pct=NULLIF(p_signal->>'tp2_pct','')::numeric,
            sl_default_price=NULLIF(p_signal->>'sl_default_price','')::numeric,
            sl_default_pct=NULLIF(p_signal->>'sl_default_pct','')::numeric,
            sl_moderat_price=NULLIF(p_signal->>'sl_moderat_price','')::numeric,
            sl_moderat_pct=NULLIF(p_signal->>'sl_moderat_pct','')::numeric,
            sl_konservatif_price=NULLIF(p_signal->>'sl_konservatif_price','')::numeric,
            sl_konservatif_pct=NULLIF(p_signal->>'sl_konservatif_pct','')::numeric,
            confidence_score=NULLIF(p_signal->>'confidence_score','')::numeric,
            confidence_label=NULLIF(p_signal->>'confidence_label',''),
            detail=COALESCE(p_signal->'detail','{}'::jsonb)
        WHERE id=v_signal_id AND user_id=p_user_id;
        RETURN v_signal_id;
    END IF;

    INSERT INTO public.signals (
        user_id,ticker,signal_type,signal_timestamp,market_date,entry_price,tp1_price,tp1_pct,
        tp2_price,tp2_pct,sl_default_price,sl_default_pct,sl_moderat_price,sl_moderat_pct,
        sl_konservatif_price,sl_konservatif_pct,confidence_score,confidence_label,detail
    ) VALUES (
        p_user_id,p_signal->>'ticker',NULLIF(p_signal->>'signal_type',''),
        (p_signal->>'signal_timestamp')::timestamptz,(p_signal->>'market_date')::date,
        NULLIF(p_signal->>'entry_price','')::numeric,NULLIF(p_signal->>'tp1_price','')::numeric,
        NULLIF(p_signal->>'tp1_pct','')::numeric,NULLIF(p_signal->>'tp2_price','')::numeric,
        NULLIF(p_signal->>'tp2_pct','')::numeric,NULLIF(p_signal->>'sl_default_price','')::numeric,
        NULLIF(p_signal->>'sl_default_pct','')::numeric,NULLIF(p_signal->>'sl_moderat_price','')::numeric,
        NULLIF(p_signal->>'sl_moderat_pct','')::numeric,NULLIF(p_signal->>'sl_konservatif_price','')::numeric,
        NULLIF(p_signal->>'sl_konservatif_pct','')::numeric,NULLIF(p_signal->>'confidence_score','')::numeric,
        NULLIF(p_signal->>'confidence_label',''),COALESCE(p_signal->'detail','{}'::jsonb)
    ) RETURNING id INTO v_signal_id;

    INSERT INTO public.signal_source_links (
        user_id,signal_id,source_record_id,match_method,match_confidence,evidence,review_status
    ) VALUES (
        p_user_id,v_signal_id,p_source_record_id,'NATIVE_ZETA_PUBLICATION','STRONG',
        jsonb_build_object('source_system','ZETA','native_source_id',v_source.native_source_id,'parser_version',v_source.parser_version),
        'AUTO_ACCEPTED'
    );
    RETURN v_signal_id;
END;
$$;

REVOKE ALL ON FUNCTION public.ensure_zeta_publication(uuid, uuid, jsonb) FROM PUBLIC;
REVOKE ALL ON FUNCTION public.ensure_zeta_publication(uuid, uuid, jsonb) FROM anon;
REVOKE ALL ON FUNCTION public.ensure_zeta_publication(uuid, uuid, jsonb) FROM authenticated;
GRANT EXECUTE ON FUNCTION public.ensure_zeta_publication(uuid, uuid, jsonb) TO service_role;

-- ==========================================================================
-- S4: deterministic, versioned Zeta-authority canonical outcome resolver
-- ==========================================================================
CREATE OR REPLACE FUNCTION public.resolve_zeta_outcome(p_user_id uuid,p_signal_id uuid)
RETURNS uuid
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public
AS $$
DECLARE
    v_assertion public.signal_outcome_assertions%ROWTYPE;
    v_source public.source_records%ROWTYPE;
    v_active public.signal_outcomes%ROWTYPE;
    v_outcome_id uuid;
BEGIN
    PERFORM pg_advisory_xact_lock(hashtextextended(p_signal_id::text,0));

    SELECT a.* INTO v_assertion
    FROM public.signal_outcome_assertions a
    JOIN public.source_records sr ON sr.id=a.source_record_id
    WHERE a.user_id=p_user_id AND a.signal_id=p_signal_id
      AND sr.user_id=p_user_id AND sr.source_system='ZETA'
    ORDER BY COALESCE(a.source_resolved_at,sr.source_timestamp_utc,a.created_at) DESC,
             a.created_at DESC,a.id DESC
    LIMIT 1;
    IF NOT FOUND THEN RAISE EXCEPTION 'No Zeta outcome assertion for signal %',p_signal_id; END IF;

    SELECT * INTO v_source FROM public.source_records
    WHERE id=v_assertion.source_record_id AND user_id=p_user_id;

    SELECT * INTO v_active FROM public.signal_outcomes
    WHERE user_id=p_user_id AND signal_id=p_signal_id AND superseded_at IS NULL
    LIMIT 1 FOR UPDATE;

    IF v_active.id IS NOT NULL
       AND v_active.outcome_status=v_assertion.outcome_status
       AND v_active.resolver_version='zeta-authority-v1'
       AND v_active.authority='ZETA'
       AND v_active.supporting_evidence->>'source_record_id'=v_assertion.source_record_id::text
    THEN RETURN v_active.id; END IF;

    IF v_active.id IS NOT NULL THEN
        UPDATE public.signal_outcomes SET superseded_at=now() WHERE id=v_active.id;
    END IF;

    INSERT INTO public.signal_outcomes (
        user_id,signal_id,outcome_status,resolved_at,resolver_version,authority,supporting_evidence
    ) VALUES (
        p_user_id,p_signal_id,v_assertion.outcome_status,v_assertion.source_resolved_at,
        'zeta-authority-v1','ZETA',
        jsonb_build_object('source_record_id',v_assertion.source_record_id,'assertion_id',v_assertion.id,
                           'zeta_id',v_source.native_source_id,'profit_pct',v_assertion.profit_pct)
    ) RETURNING id INTO v_outcome_id;
    RETURN v_outcome_id;
END;
$$;

REVOKE ALL ON FUNCTION public.resolve_zeta_outcome(uuid, uuid) FROM PUBLIC;
REVOKE ALL ON FUNCTION public.resolve_zeta_outcome(uuid, uuid) FROM anon;
REVOKE ALL ON FUNCTION public.resolve_zeta_outcome(uuid, uuid) FROM authenticated;
GRANT EXECUTE ON FUNCTION public.resolve_zeta_outcome(uuid, uuid) TO service_role;

-- ==========================================================================
-- POST-FLIGHT physical objects
-- ==========================================================================
SELECT 'signal_events_source_unique_idx' AS object_name,
       CASE WHEN to_regclass('public.signal_events_source_unique_idx') IS NOT NULL THEN 'PASS' ELSE 'FAIL' END AS result
UNION ALL
SELECT 'ensure_telegram_publication',
       CASE WHEN to_regprocedure('public.ensure_telegram_publication(uuid,uuid,jsonb)') IS NOT NULL THEN 'PASS' ELSE 'FAIL' END
UNION ALL
SELECT 'ensure_zeta_publication',
       CASE WHEN to_regprocedure('public.ensure_zeta_publication(uuid,uuid,jsonb)') IS NOT NULL THEN 'PASS' ELSE 'FAIL' END
UNION ALL
SELECT 'resolve_zeta_outcome',
       CASE WHEN to_regprocedure('public.resolve_zeta_outcome(uuid,uuid)') IS NOT NULL THEN 'PASS' ELSE 'FAIL' END;
