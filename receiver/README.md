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

## Android viewer read API

The Android app uses `GET /v1/messages` with
`Authorization: Bearer <KAIFORWARD_READ_TOKEN>`. Configure this separate read
credential with `npx convex env set KAIFORWARD_READ_TOKEN '<random read token>'`
(add `--prod` for production). Reads return 503 until it is configured; the
firmware's upload token does not grant viewer access.

The response is `{ "messages": [...], "cursor": "..." }`, newest first, in pages
of 25. Pass the opaque cursor as a URL-encoded `cursor` query parameter for the
next page. The final page has `cursor: null`. Fields follow the stored schema:
`key`, `id`, `timestamp` (milliseconds since epoch), `sender`, `recipient`,
`direction`, optional `simSlot`, and `receivedAt`. Database metadata is omitted.
All read responses use `Cache-Control: no-store`.

## Paginated server search

`POST /v1/messages/search` uses the same read credential and accepts JSON:

```json
{"query":"payment","cursor":null}
```

The response has the same `{ "messages": [...], "cursor": "..." }` shape, with
up to 25 matching messages in relevance order. Submit the returned cursor with
the same query for the next page; a null cursor ends the results. Cursors are
bound to the query. Search terms and credentials do not appear in URLs, and
responses use `Cache-Control: no-store`.

The HTTP action runs a private Convex query against the `search_messages`
full-text index, covering message text, sender IDs, and sender/recipient numbers.
Formatted telephone-number searches are normalized to digits. Matching is
case-insensitive word/prefix search, not arbitrary substring matching. The query
is limited to 256 characters, 16 words, and 32 characters per word. Contact names
are not stored in this index.

New uploads are indexed automatically. After the first deployment, populate
search text for older records with the private, resumable action:

```sh
CONVEX_DEPLOYMENT=prod:peaceful-cuttlefish-556 npx convex run --prod messages:rebuildSearch '{"cursor":null}'
```

It processes at most 100 bounded batches per invocation and reports the patched
count and continuation cursor. If the cursor is non-null, pass it in the next
invocation until it returns null. This migration is idempotent and preserves the
original SMS fields and upload replay behavior. Both history and search omit the
derived `searchText` field from responses.

Run `npm test` for the receiver contract tests and `npm run typecheck` to check
the Convex functions. See [`../android/`](../android/README.md) for app setup.


## Paginated conversation history

`POST /v1/messages/thread` requires the read credential and accepts
`{"conversation":"+880123456","cursor":null}`. It returns up to 25 messages
for that conversation, newest first, with an opaque cursor (null at the end).
Continue with the returned cursor and the same conversation. Cursors are
bound to their conversation. Requests and responses use the same no-store
policy as search.

The `by_conversation_timestamp` index uses the received sender or sent
recipient. Numeric formatting is removed while preserving country prefixes;
alphanumeric sender IDs are trimmed and lowercased. This matches Android
thread grouping and does not upload or group by contact names. After deploying,
run `messages:rebuildSearch` as above to backfill conversation keys for existing
messages as well as search text. Derived index fields are omitted from API
responses.
