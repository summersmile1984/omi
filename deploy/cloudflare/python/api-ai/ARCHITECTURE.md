# API AI Worker

`src/entry.py` composes the FastAPI application and verifies request-bound
internal assertions from Edge. Worker bindings supply inference, D1, R2 and
service RPC; the Worker does not load the Server OS backend or local AI models.
`chat_generation_routes.py` owns text generation and its shared
`chat_quota.py` D1 reservation/settlement boundary. Voice transcription, TTS,
app generation and provider proxy modules preserve their separate public wire
contracts. Shared Python transport/auth modules come from `python/shared` via
the normal source staging entry.

`realtime_routes.py` contains the legacy desktop session/usage contracts.
Edge currently denies direct OpenAI/Gemini session issuance for the pure
Cloudflare profile. A usable native interactive transport is still required;
the private mint implementation is not proof of that capability.

Client-reported realtime pricing follows the upstream
`utils.llm.realtime_usage` contract: rates belong to the fixed server-issued
model, cached tokens are a text-first subset of input, and micro-USD rounding
is integer half-up. Gemini Live uses its full published modality rates for
cached input. Native Workers AI reports do not invent an external token cost;
the Durable Object's speech-duration authority owns that meter. This
client-reported metadata is not an invoice reconciliation or hosted live-model
verification. Default prompts are not involved in this accounting path.

Migration 0164 makes one immutable `cf_realtime_usage_events` row the normalized
turn receipt. A provided `turn_id` is hashed and scoped to its authenticated UID
and meter. OpenAI/Gemini hub turns share an identity namespace; the two native
speech aliases share a separate namespace. The first report wins across retries
and UTC date changes. Older clients without a turn ID retain one event per
request. No raw turn identity, session credential or conversation content is
stored in this receipt.

`chat_quota.question_reservation_statement` shares the text-chat admission
policy while allowing realtime to commit the reservation and usage together.
An accepted managed event projects realtime counters, the `desktop_chat_realtime`
cost bucket and quota settlement inside that D1 transaction. Failed projections
roll back the reservation too. Managed reports never receive a BYOK exemption;
native speech metadata does not create an external-model charge or chat question.
The existing duration meter remains the native ASR usage authority. Historical
daily totals are retained without inventing old turn receipts or retroactive
quota events. Missing storage returns 502 instead of acknowledging lost usage.

Core export includes owned `realtime_usage` and `realtime_turns` sections. The
Jobs account-deletion registry purges the receipt table, and D1 fences reject
late writes under either an active deletion intent or a completed tombstone.
The existing recording product lane exercises concurrent public reports,
cost/quota reads, export, account isolation and actual queue-driven erasure.

Rate sources: [OpenAI GPT-Realtime-2](https://developers.openai.com/api/docs/models/gpt-realtime-2),
[Gemini Live pricing](https://ai.google.dev/gemini-api/docs/pricing).
Run `uvx uv==0.12.3 run pytest -q` in this package for the component suite.
