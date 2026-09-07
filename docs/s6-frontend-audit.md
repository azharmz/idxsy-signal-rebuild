# S6 hydrated sorting and canonical Profit audit

The former headers Outcome / Profit / Sumber had no data-sort handlers, and only
one server page was hydrated. The frontend now fetches all visible signals,
active outcomes and linked source labels in bounded batches/ranges, publishes
only the complete hydration, and filters/sorts before local pagination. Refresh
replaces the snapshot; detail events and raw provenance remain lazy-loaded.
Each range has stable ordering. Null values sort last in both directions; ties
use signal ID. Source sorting uses the same deduplicated, sorted label as display.
This is a paginated read cache, not a transactionally consistent database snapshot.

## Profit contract and data gap

The authoritative S4 field is signal_outcomes.supporting_evidence.profit_pct.
Both checked-in S4 RPC definitions copy signal_outcome_assertions.profit_pct
into this JSON field. The previous UI already read this path; changing to an
assumed top-level outcome profit_pct would be incorrect. List, detail and sorting
now share the same accessor. Zero, negatives and finite numeric strings remain
values. Missing/null, malformed and no-outcome states are distinct. There is no
price calculation or fallback to an unrelated/latest assertion in the browser.
The summary counts final outcomes without valid profit across the loaded snapshot.

The S4 resolver's existing early return compares status, resolver version,
authority and source_record_id, but does not compare profit_pct or assertion_id.
Consequently, an assertion profit correction with the same source/status can
leave an older/null active outcome evidence value. This is a possible data gap,
not a confirmed live diagnosis, and the FROZEN resolver is unchanged.
The source adapter also accepts raw profit_pct null without deriving a value.

Run supabase/verification/s6_profit_gaps.sql in an authorized DB session to list
actual signal/outcome IDs and distinguish null assertion profit from missing
outcome evidence where the referenced assertion has profit. If a referenced
assertion is absent/invisible, investigate its provenance/RLS separately.
Live DB counts have not been verified in this local checkout; no authenticated
DB credentials were provided. No data repair, migration or canonical backend
semantics are included in this change.

## Verification

- node --test tests/frontend.test.cjs runs the actual inline frontend script with
  a small DOM and Supabase test double: global sorting, zero/null/string profit,
  capped result pagination, source hydration, errors and cached interactions.
- python -m pytest -q includes the updated frontend architecture contracts.
- python scripts/build_cloudflare_pages.py validates the single-file deployment.
- rebuild-ci runs JavaScript regressions with Node 22 before Python tests/build.
