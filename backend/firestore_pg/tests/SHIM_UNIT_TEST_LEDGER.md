# Unit-test migration ledger: tests/unit Firestore semantics through the shim

Date: 2026-08-09 · Branch: `feature/cloud-neutral-shim` · Mode: `FIRESTORE_PG_DSN`
set (shim facade installed before import). Each file run standalone to avoid
pytest timeseries-fixture name collisions across files.

## Result

| File | Result (shim mode) |
|---|---|
| `test_action_item_idempotency` | 13 passed |
| `test_agent_proxy_async_boundaries` | 21 passed |
| `test_agent_proxy_history_seeding` | 5 passed |
| `test_agent_proxy_startup_client_gone` | 3 passed |
| `test_agent_vm_firebase_project_split` | **1 failed, 6 passed** (see note) |
| `test_agent_vm_reaped_record_recovery` | 6 passed |
| `test_api_key_listability_contract` | 25 passed |
| `test_check_firestore_model_read_boundary` | 4 passed |
| `test_conversation_finalization_jobs` | 45 passed |
| `test_conversations_count` | 32 passed |
| `test_desktop_migration` | 114 passed |
| `test_dev_key_memory_grant_seeding` | 8 passed |
| `test_firestore_cache` | 9 passed |
| `test_firestore_di_seam` | 5 passed |
| `test_firestore_query_contract` | 25 passed |
| `test_firestore_query_stream_retry` | 7 passed |
| `test_mcp_api_key_full_access` | 7 passed |
| `test_memory_ledger` | 29 passed |
| `test_review_queue_list_query` | 8 passed |
| `test_sync_two_lane` | 33 passed |

Shim's own suites: `firestore_pg/tests/` — 10 passed (transactions, composite
indexes); shadow diff real-vs-shim 16/16 match.

## The one shim-mode failure

`test_agent_vm_firebase_project_split::test_firestore_client_uses_dev_adc_when_firebase_auth_path_is_separate`
asserts `get_firestore_client() is firestore.Client()` (a MagicMock the test
installs). Under `FIRESTORE_PG_DSN`, `database._client._build_firestore_client`
deliberately takes the shim branch and returns a `firestore_pg.Client` instead
of calling `firestore.Client()`, so the identity assertion fails.

This is the **intended shim divergence**, not a regression: the test verifies the
real-client ADC factory path, which the shim bypasses by design. It passes in
normal mode (verified, 7/7). Documented here as the expected ledger entry.

## Gaps the shim surfaced and fixed this run

- `firestore.Query.ASCENDING` / `DESCENDING` class constants were missing
  (12+ `database/*` modules call them) — added to the shim `Query`.
- `@firestore.transactional` was stricter than the real SDK: it rejected
  duck-typed transactions (DI-injected test fakes with `_begin/_commit/
  _rollback/_max_attempts`). Now mirrors the real `_Transactional.__call__`
  for non-shim transactions.
