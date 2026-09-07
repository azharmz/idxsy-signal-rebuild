from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
WEB = (ROOT / 'web' / 'index.html').read_text(encoding='utf-8')


def test_frontend_has_no_legacy_canonical_dependencies():
    for legacy in ('trades_data', 'lastXlsxRows', 'matchTrades(', 'mergeWithXlsxData(', 'recompute_trades'):
        assert legacy not in WEB


def test_frontend_has_no_manual_import_pipeline():
    assert 'type="file"' not in WEB
    assert 'FileReader' not in WEB
    assert 'XLSX' not in WEB
    assert 'parseTelegramExport' not in WEB


def test_frontend_is_read_only_against_canonical_tables():
    for mutation in ('.insert(', '.update(', '.upsert(', '.delete('):
        assert mutation not in WEB
    assert "from('signals').select" in WEB
    assert "from('signal_events').select" in WEB
    assert "from('signal_outcomes').select" in WEB
    assert "from('source_records').select" in WEB


def test_snapshot_is_hydrated_before_local_sort_and_pagination():
    assert 'async function readAll(makeQuery)' in WEB
    assert '.range(from,from+size-1)' in WEB
    assert 'const hydrated=await hydratePage' in WEB
    assert 'filtered.sort(compareSignals)' in WEB
    assert 'state.items=filtered.slice(' in WEB
    assert 'supporting_evidence?.profit_pct' in WEB
    assert '${esc(profitText(o))}' in WEB
    assert "['Profit',profitText(outcome)]" in WEB


def test_hydrated_columns_are_sortable():
    for key in ('outcome_status', 'profit_pct', 'source'):
        assert f'data-sort="{key}"' in WEB
        assert f'data-arrow="{key}"' in WEB


def test_empty_outcome_and_profit_labels_are_consistent():
    assert "o?.outcome_status||'OPEN'" in WEB
    assert "outcome?.outcome_status||'OPEN'" in WEB
    assert "return '-'" in WEB
    assert 'Belum ada outcome' not in WEB
    assert 'Belum tersedia' not in WEB


def test_frontend_uses_user_scoped_indexeddb_stale_while_revalidate_cache():
    assert "CACHE_DB='idxsy-signal-cache'" in WEB
    assert "createObjectStore(CACHE_STORE,{keyPath:'userId'})" in WEB
    assert 'readCachedSnapshot(userId)' in WEB
    assert 'void refreshSnapshot(true)' in WEB
    assert 'deleteCachedSnapshot(userId)' in WEB
    assert 'outcomes:[...outcomes]' in WEB
    assert 'sources:[...sources]' in WEB


def test_hydration_batches_have_bounded_parallelism():
    assert 'BATCH_CONCURRENCY=4' in WEB
    assert 'Math.min(BATCH_CONCURRENCY,batches.length)' in WEB
    assert 'await Promise.all(' in WEB
    assert 'return results.flat()' in WEB


def test_detail_is_lazy_loaded_and_includes_events_outcome_provenance():
    assert 'async function openDetail(id)' in WEB
    assert "from('signal_events')" in WEB
    assert "from('signal_source_links')" in WEB
    assert 'Event history' in WEB
    assert 'Provenance' in WEB


def test_canonical_frontend_config_is_explicit():
    assert 'https://wshtfqqusiifdkqqoejs.supabase.co' in WEB
    assert 'sb_publishable_45BjutlMC-tHSoS-lGXIUQ_AOIuqF1y' in WEB
    assert 'gfxhtpqzebxycijdnpjp.supabase.co' not in WEB


def test_no_backend_secret_in_frontend():
    assert 'SERVICE_ROLE' not in WEB
    assert 'SUPABASE_SERVICE_KEY' not in WEB
    assert 'ZETA_MEMBER_COOKIE' not in WEB
    assert 'TG_SESSION_STRING' not in WEB
