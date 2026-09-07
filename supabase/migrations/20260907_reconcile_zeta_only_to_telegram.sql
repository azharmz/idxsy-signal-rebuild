-- IDXSY Signal Rebuild — cross-source canonical repair
-- Purpose: merge a defensibly matched ZETA_ONLY canonical signal into an
-- existing Telegram-provenance canonical signal without losing source facts.
-- Apply to the NEW canonical Supabase only.

CREATE OR REPLACE FUNCTION public.merge_zeta_only_into_telegram(
    p_user_id uuid,
    p_zeta_source_record_id uuid,
    p_target_signal_id uuid,
    p_evidence jsonb
)
RETURNS uuid
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public
AS $$
DECLARE
    v_source public.source_records%ROWTYPE;
    v_link public.signal_source_links%ROWTYPE;
    v_old_signal_id uuid;
    v_old_link_count integer;
    v_old_has_telegram boolean;
    v_target_has_telegram boolean;
    v_old_event_count integer;
    v_foreign_assertion_count integer;
    v_has_assertion boolean;
BEGIN
    -- Serialize repair per Zeta source and target signal.
    PERFORM pg_advisory_xact_lock(hashtextextended(p_zeta_source_record_id::text, 0));
    PERFORM pg_advisory_xact_lock(hashtextextended(p_target_signal_id::text, 0));

    SELECT * INTO v_source
    FROM public.source_records
    WHERE id = p_zeta_source_record_id
      AND user_id = p_user_id
      AND source_system = 'ZETA'
    FOR UPDATE;

    IF NOT FOUND THEN
        RAISE EXCEPTION 'Zeta source_record % not found for user %', p_zeta_source_record_id, p_user_id;
    END IF;

    SELECT * INTO v_link
    FROM public.signal_source_links
    WHERE user_id = p_user_id
      AND source_record_id = p_zeta_source_record_id
    LIMIT 1
    FOR UPDATE;

    IF NOT FOUND THEN
        RAISE EXCEPTION 'Zeta source_record % has no canonical source link', p_zeta_source_record_id;
    END IF;

    v_old_signal_id := v_link.signal_id;

    IF v_old_signal_id = p_target_signal_id THEN
        RETURN p_target_signal_id;
    END IF;

    PERFORM 1
    FROM public.signals
    WHERE id = p_target_signal_id AND user_id = p_user_id
    FOR UPDATE;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'Target signal % not found for user %', p_target_signal_id, p_user_id;
    END IF;

    SELECT EXISTS (
        SELECT 1
        FROM public.signal_source_links ssl
        JOIN public.source_records sr ON sr.id = ssl.source_record_id
        WHERE ssl.user_id = p_user_id
          AND ssl.signal_id = p_target_signal_id
          AND sr.user_id = p_user_id
          AND sr.source_system = 'TELEGRAM'
          AND sr.source_record_type = 'SIGNAL'
    ) INTO v_target_has_telegram;

    IF NOT v_target_has_telegram THEN
        RAISE EXCEPTION 'Target signal % lacks Telegram publication provenance', p_target_signal_id;
    END IF;

    SELECT EXISTS (
        SELECT 1
        FROM public.signal_source_links ssl
        JOIN public.source_records sr ON sr.id = ssl.source_record_id
        WHERE ssl.user_id = p_user_id
          AND ssl.signal_id = v_old_signal_id
          AND sr.user_id = p_user_id
          AND sr.source_system = 'TELEGRAM'
          AND sr.source_record_type = 'SIGNAL'
    ) INTO v_old_has_telegram;

    IF v_old_has_telegram THEN
        RAISE EXCEPTION 'Old signal % already has Telegram provenance; refusing merge', v_old_signal_id;
    END IF;

    SELECT COUNT(*) INTO v_old_link_count
    FROM public.signal_source_links
    WHERE user_id = p_user_id AND signal_id = v_old_signal_id;

    IF v_old_link_count <> 1 THEN
        RAISE EXCEPTION 'Old signal % has % source links; expected isolated ZETA_ONLY signal', v_old_signal_id, v_old_link_count;
    END IF;

    SELECT COUNT(*) INTO v_old_event_count
    FROM public.signal_events
    WHERE user_id = p_user_id AND signal_id = v_old_signal_id;

    IF v_old_event_count <> 0 THEN
        RAISE EXCEPTION 'Old signal % has % signal_events; refusing destructive merge', v_old_signal_id, v_old_event_count;
    END IF;

    SELECT COUNT(*) INTO v_foreign_assertion_count
    FROM public.signal_outcome_assertions
    WHERE user_id = p_user_id
      AND signal_id = v_old_signal_id
      AND source_record_id <> p_zeta_source_record_id;

    IF v_foreign_assertion_count <> 0 THEN
        RAISE EXCEPTION 'Old signal % has assertions from other sources; refusing merge', v_old_signal_id;
    END IF;

    -- Make both sides temporarily outcome-inactive before moving history.
    UPDATE public.signal_outcomes
       SET superseded_at = COALESCE(superseded_at, now())
     WHERE user_id = p_user_id
       AND signal_id IN (v_old_signal_id, p_target_signal_id)
       AND superseded_at IS NULL;

    -- Preserve interpretation history by moving old outcome versions to target.
    UPDATE public.signal_outcomes
       SET signal_id = p_target_signal_id
     WHERE user_id = p_user_id
       AND signal_id = v_old_signal_id;

    -- Re-home the Zeta assertion to the canonical Telegram-backed signal.
    UPDATE public.signal_outcome_assertions
       SET signal_id = p_target_signal_id
     WHERE user_id = p_user_id
       AND signal_id = v_old_signal_id
       AND source_record_id = p_zeta_source_record_id;

    -- Move provenance link and retain explicit repair evidence.
    UPDATE public.signal_source_links
       SET signal_id = p_target_signal_id,
           match_method = 'TELEGRAM_ZETA_RECONCILIATION',
           match_confidence = 'HIGH_CONFIDENCE',
           review_status = 'AUTO_ACCEPTED',
           evidence = COALESCE(evidence, '{}'::jsonb)
                      || COALESCE(p_evidence, '{}'::jsonb)
                      || jsonb_build_object(
                          'reconciled_from_signal_id', v_old_signal_id,
                          'reconciled_at', now(),
                          'repair_version', 'zeta-telegram-late-bind-v1'
                      )
     WHERE id = v_link.id;

    -- At this point every preserved fact/history reference has moved away from
    -- the erroneous duplicate interpretation. Removing this canonical signal
    -- does not remove source_records or versioned outcome history.
    DELETE FROM public.signals
    WHERE id = v_old_signal_id AND user_id = p_user_id;

    SELECT EXISTS (
        SELECT 1
        FROM public.signal_outcome_assertions
        WHERE user_id = p_user_id AND signal_id = p_target_signal_id
    ) INTO v_has_assertion;

    IF v_has_assertion THEN
        PERFORM public.resolve_zeta_outcome(p_user_id, p_target_signal_id);
    END IF;

    RETURN p_target_signal_id;
END;
$$;

REVOKE ALL ON FUNCTION public.merge_zeta_only_into_telegram(uuid, uuid, uuid, jsonb) FROM PUBLIC;
REVOKE ALL ON FUNCTION public.merge_zeta_only_into_telegram(uuid, uuid, uuid, jsonb) FROM anon;
REVOKE ALL ON FUNCTION public.merge_zeta_only_into_telegram(uuid, uuid, uuid, jsonb) FROM authenticated;
GRANT EXECUTE ON FUNCTION public.merge_zeta_only_into_telegram(uuid, uuid, uuid, jsonb) TO service_role;

SELECT 'merge_zeta_only_into_telegram' AS object_name,
       CASE WHEN to_regprocedure('public.merge_zeta_only_into_telegram(uuid,uuid,uuid,jsonb)') IS NOT NULL
            THEN 'PASS' ELSE 'FAIL' END AS result;
