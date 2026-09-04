# Web session owner

`AuthSession` owns the browser identity and opaque session for one resolved
deployment profile. Sign-in, sign-up and sign-out each claim a new operation
epoch. Only the latest operation can publish an identity or finish a logout;
superseded operations reject with `AuthRequestError(409)` so UI callers cannot
treat a cancelled action as successful. Restoration also checks its captured
epoch before publishing, and an old restoration cannot invalidate a newer
successful authentication's cache.

The controller accepts a transport, storage and clock for behavioral fault
tests. `web/app/fork/test.sh` runs the deferred-response regressions through this
production controller in the existing fork contract lane. These regressions
cover the sign-in/sign-out publication races identified during independent
review of commit `7263d03f4f498fa40dadcf0065fb2279be5df417`.

This client-side ownership rule does not replace server session revocation.
Session storage and access-token rules are documented in `web/app/fork/README.md`.
