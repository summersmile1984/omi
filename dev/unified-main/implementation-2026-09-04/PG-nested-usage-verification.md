# Nested PostgreSQL usage mutation verification

The real self-host Qwen greeting reached the existing usage callback, but
`database.llm_usage.record_llm_usage` failed with `unsupported Firestore value
type: Increment`. Upstream #12065 (`0939cf2530`) correctly changed usage writes
to nested maps for `set(merge=True)`; the PG adapter normalized only top-level
sentinels and sent the nested objects into its ordinary JSONB codec. The
synthetic direct-owner traceback is retained at
`/tmp/memweft-implementation/server/llm/pg-increment-trace.log`.

The same document owner now recursively normalizes and materializes transforms.
Merge preserves map siblings; an explicitly empty map replaces its value.
Nested literal field names do not become dotted paths. The original SQL,
transaction and self-host write-policy authority remain in charge, with no
second lock, schema change, business-caller patch or inferred default model.

The expected merge/replacement behavior follows
[Firestore write documentation](https://docs.cloud.google.com/firestore/native/docs/manage-data/add-data)
and the installed google-cloud-firestore SDK's `DocumentExtractor`,
`DocumentExtractorForMerge` and `DocumentExtractorForUpdate`. Independent review
found that newly accepted nested DELETE_FIELD could otherwise overwrite a map
in forbidden update/create/non-merge-set positions. The regression executes the
installed SDK's protobuf constructors and the actual PG document owner: both
reject, and the SQL seam records zero writes. Valid nested merge deletes remain
supported. Source SDK tests and application callers are unchanged.

Evidence under `/tmp/memweft-implementation/pg-transforms/`:

- `before.log`: seven failures and one pass through the actual original write
  owner. Both SDK and facade nested Increment failed; plain merge lost siblings.
- `unit-reviewed.log`: nested write and existing account write-policy suites
  pass, including the added nested terminal-account rejection. These are
  hermetic production-behavior tests with the SQL connection as the seam.
- `live.log`: the real disposable PostgreSQL fence suite passes. New cases
  execute actual LLM usage and visible-question owners for a principal without
  state documents, persist both model counters without overwriting siblings,
  and deduplicate a repeated question. A competing first-use write receives
  the existing explicit `ProviderOperationBusy`; after the first commit, an
  explicit caller retry yields 7 input/7 output tokens and 2 calls. This does
  not prove that existing callbacks automatically retry contention.
- `electron/review-pg-delete-placement.log` under the parent implementation
  directory retains the independent review's failing placement reproduction.

The existing startup manifest discovers the new hermetic suite locally and in
CI; the live suite requires a disposable DSN and is never reported as hermetic.
This guard extends the existing storage primitive. The real greeting failure
and upstream #12065 are the concrete instances establishing its value.
Fresh standard-image/core verification and the actual native-model usage retry
remain separately recorded acceptance surfaces, not consequences of unit tests.
