from pathlib import Path

from backend.reconciliation import match_zeta_to_telegram


ROOT = Path(__file__).resolve().parents[1]
MIGRATION = (ROOT / 'supabase' / 'migrations' / '20260907_reconcile_zeta_only_to_telegram.sql').read_text(encoding='utf-8')
REPAIR = (ROOT / 'backend' / 'zeta_late_bind_reconcile.py').read_text(encoding='utf-8')


def _candidate(signal_id='tg-icon'):
    return {
        'id': signal_id,
        'signal_timestamp': '2026-09-07T05:19:00+00:00',
        'entry_price': 139,
        'tp1_price': 147,
        'sl_default_price': 131,
        'telegram_source_record_id': 'tg-src',
        'telegram_msg_id': '12345',
    }


def test_recent_two_minute_counterpart_is_defensible_match():
    zeta = {
        'source_timestamp_utc': '2026-09-07T05:17:00+00:00',
        'entry_price': 139,
        'tp1_price': 147,
        'sl_default_price': 131,
    }
    result = match_zeta_to_telegram(zeta, [_candidate()])
    assert result.result == 'MATCHED'
    assert result.signal_id == 'tg-icon'
    assert result.evidence['age_seconds'] == 120


def test_repair_uses_frozen_matcher_and_atomic_rpc_only():
    assert 'match_zeta_to_telegram' in REPAIR
    assert 'merge_zeta_only_into_telegram' in REPAIR
    assert 'delete().eq(' not in REPAIR
    assert 'table("signals").delete' not in REPAIR


def test_merge_rpc_requires_telegram_target_and_isolated_zeta_only_source():
    assert "sr.source_system = 'TELEGRAM'" in MIGRATION
    assert "sr.source_record_type = 'SIGNAL'" in MIGRATION
    assert 'v_old_link_count <> 1' in MIGRATION
    assert 'v_old_has_telegram' in MIGRATION
    assert 'v_old_event_count <> 0' in MIGRATION
    assert 'v_foreign_assertion_count <> 0' in MIGRATION


def test_merge_preserves_source_and_outcome_history_before_duplicate_signal_delete():
    assert 'UPDATE public.signal_outcomes' in MIGRATION
    assert 'SET signal_id = p_target_signal_id' in MIGRATION
    assert 'UPDATE public.signal_outcome_assertions' in MIGRATION
    assert "match_method = 'TELEGRAM_ZETA_RECONCILIATION'" in MIGRATION
    assert "'reconciled_from_signal_id'" in MIGRATION
    assert 'DELETE FROM public.signals' in MIGRATION
    assert 'DELETE FROM public.source_records' not in MIGRATION
    assert 'PERFORM public.resolve_zeta_outcome' in MIGRATION


def test_merge_rpc_is_backend_only():
    signature = 'merge_zeta_only_into_telegram(uuid, uuid, uuid, jsonb)'
    assert f'REVOKE ALL ON FUNCTION public.{signature} FROM PUBLIC;' in MIGRATION
    assert f'REVOKE ALL ON FUNCTION public.{signature} FROM anon;' in MIGRATION
    assert f'REVOKE ALL ON FUNCTION public.{signature} FROM authenticated;' in MIGRATION
    assert f'GRANT EXECUTE ON FUNCTION public.{signature} TO service_role;' in MIGRATION
