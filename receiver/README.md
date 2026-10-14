# KaiForward Convex receiver

A Convex HTTP action that implements the firmware's `/v1/messages` upload
protocol, replacing the PoC's `poc/server/receiver.py`. Messages are stored in
the `messages` table, keyed by message key.

```sh
npm install
npx convex dev --once          # deploy functions to the dev deployment
npx convex env set KAIFORWARD_TOKEN '<receiver token>'
```

The phone's receiver URL is `<CONVEX_SITE_URL>/v1/messages`, for example
`https://peaceful-cuttlefish-556.convex.site/v1/messages` in production. The action behaves like
the PoC receiver: bearer token check, the same message validation, and
`{"accepted":[...]}` on success. A batch is stored atomically. Replaying a
stored message is accepted; a key with different content returns 409 and
stores nothing. `GET /health` returns `{"ok":true}`.

Convex serves certificates chaining to GTS Root R4 (cross-signed by GlobalSign
Root CA). The phone's system bundle at `/system/kaiforward/ca.pem` already
verifies them.

