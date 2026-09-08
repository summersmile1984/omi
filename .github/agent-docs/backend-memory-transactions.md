# Backend Firestore and canonical mutation boundaries

**Firestore** (primary store): use `get_firestore_client()` from `database._client` at call time, and add optional keyword-only `firestore_client` parameters on converted database helpers so tests can inject fake clients. `db` remains a legacy lazy compatibility proxy only; do not use it in new code. Never construct Firestore clients at import time. Segments are encrypted at rest — direct Firestore reads return opaque blobs. Feature gating via user fields: e.g., translation requires `users/{uid}.language` non-empty — silently disabled if missing.

**Canonical mutation identity:** when a user patch changes `arguments`, the operation's logical payload must contain the same arguments before computing its digest. Verify the adapter-to-apply boundary, not only a hand-built operation: `test_canonical_user_mutation_hashes_feedback_arguments_before_apply` covers the valid feedback request that previously failed with `payload_mismatch`.
