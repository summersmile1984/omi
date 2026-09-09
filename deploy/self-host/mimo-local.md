# Local Server OS with Xiaomi MiMo

The opt-in `mimo-cn` profile uses China Token Plan for `mimo-v2.5` chat and
business extraction, `mimo-v2.5-asr` transcription, and `mimo-v2.5-tts` speech.
Embedding remains the pinned local BGE-M3/Ollama model. Default prompts, tool
descriptions, PostgreSQL owners, memory extraction and tool execution are unchanged.
The local command remains an explicit opt-in. A brand can also freeze
`self_hosted_inference.beta: mimo-cn` and/or the production selection in its
manifest; Eddy selects both. That single policy is consumed by every profile
renderer and the production Compose model-service selector. The normal native
profile and hermetic core fixture retain their existing model contracts.

For an accepted beta/production image, store `MIMO_API_KEY` in the private host
`runtime.env` and provision only `EMBEDDING_MODEL_STORE`. The canonical Compose
wrapper removes the unused LLM services and speech mount before interpolation;
the startup checker therefore requires the selected provider's actual inputs.
MiMo credentials reach only the API service, never images or client profiles.
The historical zero-vendor/cutover attestation intentionally does not certify
a hosted MiMo deployment; CD runs its separately documented public acceptance.

Render public configuration without credentials:

```bash
python3 scripts/profiles/render.py --target self_hosted --stage local \
  --brand eddy --operator-ai mimo-cn --emit-json
```

Prepare a private mode-0600 JSON file outside the repository with
`MIMO_API_KEY` and `MIMO_BASE_URL`. The endpoint must be exactly
`https://token-plan-cn.xiaomimimo.com/v1`. Never put credentials in a brand
manifest, generated client table or image. Start an isolated local environment:

```bash
python3 deploy/self-host/ci/product.py --output /absolute/private/new-local-run \
  --brand-id eddy --port 34880 \
  --embedding-store /absolute/path/to/admitted-bge-m3-store \
  --mimo-secret-file /absolute/private/mimo.json
```

Do not pass `--llm-store` or `--speech-store` in this mode. The fixture builds
the ordinary backend image, renders the selected public profile, runs the normal
database migrations and starts the real product services. Only the API receives
the MiMo credential and an outbound network; storage and embedding stay internal.
The private generated Compose file contains credentials and has mode 0600.
The Python HTTP guard grants only the exact selected HTTPS Chat Completions
endpoint; it does not grant every Xiaomi hostname or a different provider.

Both audio models use `/chat/completions`. ASR sends base64 WAV/MP3 input;
TTS sends the requested text as an assistant message and receives base64 WAV.
The public TTS routes retain authentication, rate limits and MP3 conversion.
ASR supports the documented `auto`, `zh` and `en` language options. Live listen
and PTT buffer bounded PCM windows and flush at finalization through the existing
socket ownership contract; this is chunked HTTP recognition, not a native MiMo
WebSocket stream. ASR reports one anonymous speaker; it does not identify people.
Normal disconnect waits for outstanding ASR windows and the transcript persistence
owner before finalizing. Deferred desktop conversations use the existing first-open
enrichment flow; opening their detail view triggers summary and memory extraction.

MiMo documents automatic tool selection. Structured outputs use the existing
schema as a function tool and validate the returned object. A missing result
fails rather than becoming a successful extraction. The adapter retains the
prior local adapter's zero temperature and disabled thinking; it does not rewrite
application prompts.

Provider failures do not select another model. The local mode is not evidence
of Cloudflare Workers AI or a completed Cloudflare production deployment.
Measured business results and unresolved ASR quality issues are recorded in the
[local verification journal](../../dev/unified-main/implementation-2026-09-05/server-mimo-verification-2026-09-06.md).

Official protocol sources:

- [Token Plan access](https://mimo.mi.com/docs/zh-CN/tokenplan/Token%20Plan/quick-access)
- [Chat Completions](https://mimo.mi.com/docs/en-US/api/chat/openai-api)
- [Speech recognition](https://mimo.mi.com/docs/en-US/api/audio/Speech-Recognition)
- [Speech synthesis](https://mimo.mi.com/docs/usage-guide/speech-synthesis-v2.5)
