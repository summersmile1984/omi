# Phone / Twilio staging owner

Phone caller-ID verification and Voice webhook routes are implemented in the
Cloudflare Jobs Worker. The Edge authenticates Better Auth sessions, evaluates
the account cutover gate, applies the existing Redis rate-limit boundary, and
forwards an internal signed context to Jobs. The public Twilio TwiML route
forwards only the provider signature and request body; Jobs validates the
canonical URL and `X-Twilio-Signature` before emitting bounded XML.

## State authority

Migration `0108_phone_twilio.sql` adds the D1 authority:

- `cf_phone_numbers` stores only a uid-scoped encrypted E.164 value, its
  deterministic hash, Twilio caller-ID SID, primary flag, and account
  generation.
- `cf_phone_verifications` stores one globally unique pending number with a
  five-minute TTL, uid/generation ownership, attempts, and terminal provider
  failure state. A provider failure is retained as `failed`, so a later retry
  can create a fresh request rather than silently duplicating a pending call.
- `cf_phone_call_usage` reserves free-tier monthly calls with an atomic D1
  conditional update. `cf_phone_call_attempts` hashes `CallId`/`CallSid` and
  gives signed TwiML retries a short pending lease plus completed idempotency.
- All four tables have deletion-intent/tombstone triggers and are included in
  the account-deletion residual scan and purge order.

Twilio REST credentials, Voice API-key credentials, TwiML application SID, and
the phone encryption secret are Worker secrets. REST Basic authentication uses
standard Base64 (not Base64URL). Voice JWTs use HS256 and include `sub` equal
to the Twilio Account SID, `iss` equal to the API key SID, and the Voice grant.

## Routes and verification

The six legacy `/v1/phone/*` routes are now `staging-owned` by Jobs:

`GET /v1/phone/numbers`, `DELETE /v1/phone/numbers/{phone_number_id}`,
`POST /v1/phone/numbers/verify`, `POST /v1/phone/numbers/verify/check`,
`POST /v1/phone/token`, and `POST /v1/phone/twiml`.

Focused evidence:

```text
npm test -- --run tests/phone-twilio.test.ts tests/phone-edge.test.ts
npm run typecheck
npm run validate:manifest
```

The tests cover Better Auth-to-Jobs forwarding, public Twilio signature/body
preservation, standard REST auth, verification/check/list/delete, Voice JWT
claims, cross-uid pending claims, and deletion fencing. A live staging check
still requires configured Twilio test credentials and a disposable verified
number; no production Twilio or historical Firestore backfill is implied by
this code change.
