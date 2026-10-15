# KaiForward for Android

A native Kotlin / Jetpack Compose viewer for SMSes uploaded by the KaiForward
phone to the existing Convex receiver. Requires Android 8.0 (API 26) or newer.

## Configure the receiver

Deploy the updated receiver and set a separate, randomly generated read token
in the intended Convex deployment:

```sh
cd receiver
npm ci
npx convex dev --once
npx convex env set KAIFORWARD_READ_TOKEN '<random read token>'
```

For production use `npx convex deploy` and `npx convex env set --prod
KAIFORWARD_READ_TOKEN '<random read token>'` instead. Keep the firmware's existing
`KAIFORWARD_TOKEN` unchanged. The read token authorizes access to all messages in
that deployment; it is a shared viewer credential, not a user account system.
The app does not need the firmware's upload credential or call relay token.

## Build and install

Open this directory in Android Studio, use a JDK 17 or newer (tested with 21),
and install Android SDK Platform 36. Android Studio can configure the local SDK
path. From the command line, set `ANDROID_HOME` or put `sdk.dir=/path/to/Android/Sdk`
in an untracked `local.properties`, then run:

```sh
cd android
./gradlew assembleDebug testDebugUnitTest lintDebug
adb install -r app/build/outputs/apk/debug/app-debug.apk
```

The Gradle wrapper downloads Gradle 8.13. Build dependencies are pinned in the
Gradle files.

### Signed release APK

Release builds use the private signing configuration at
`data/android-release/signing.properties` (relative to the repository root).
The configuration references `data/android-release/kaiforward-release.p12` and
contains `storeFile` (relative to `android/`), `storePassword`, `keyAlias`, and
`keyPassword`. These files are ignored by Git and restricted to the local user.
Back up the entire `data/android-release/` directory privately: future release
updates must use the same signing key. Never include this directory in files
shared with app users. Release builds fail if the signing configuration is absent.

```sh
cd android
./gradlew assembleRelease testReleaseUnitTest lintRelease
```

The signed APK is `app/build/outputs/apk/release/app-release.apk`. Release builds
disable debugging and enable code/resource shrinking. A copy with a descriptive
name is placed in `dist/android/` when preparing a release for sharing.

To use the shared APK, the recipient needs Android 8.0 or later, internet access,
the receiver URL, and `KAIFORWARD_READ_TOKEN`. The existing production URL is
prefilled in the app. Contact permission is optional and uses contacts on the
recipient's Android phone. The read token grants access to all forwarded SMSes
in the deployment; send it privately, separately from the APK. The local token
copy is in the ignored `data/android-viewer.env` file. No signing credentials or
receiver token are embedded in the APK.

The release certificate differs from the development debug certificate. To
replace an existing debug installation, first uninstall it (this clears the
app's local connection settings), install the release APK, then reconnect using
the read token. This does not delete SMSes stored on the receiver.

## Connect and view

1. Open KaiForward and enter the receiver's HTTPS origin, for example
   `https://your-deployment.convex.site`. Do not append `/v1/messages`.
2. Enter `KAIFORWARD_READ_TOKEN` and tap **Connect and view messages**.
3. Optionally allow Contacts to resolve numbers against this Android phone's
   address book. Denying permission keeps the viewer usable with raw numbers.

Conversations are ordered by their latest loaded message. Incoming messages use
the sender as the other party; outgoing messages use the recipient. Opening a
conversation shows sent/received messages, timestamps and the SIM slot if known.
Long-press message text to select and copy it. Tap the search icon in the Messages
toolbar to open a separate search screen. Typing is debounced for 350 ms, then
the app sends the query to the receiver's authenticated search HTTP action.
Convex searches indexed message text, sender IDs, and phone numbers across all
stored history and returns 25 matching messages at a time in relevance order.
This is word/prefix matching rather than arbitrary substring matching. Search
has its own cursor, results, loading, and retry state, independent of the inbox.
Changing or clearing the query cancels the previous request; stale responses
cannot replace the current results.

Each result previews the matching SMS. Opening it loads consecutive pages of
that conversation through the matching SMS and jumps to it. Going back preserves the query and result pages;
closing search returns to the full conversation list. Contact-name shortcuts
for already loaded conversations remain local; the address book is not uploaded.
The inbox reads `GET /v1/conversations`, with 25 distinct conversations per
page and server-maintained latest-message previews. Reaching the bottom
loads another conversation page. It never scans raw SMS pages to discover
senders. In a conversation, opening the thread fetches its newest 25 messages from
`POST /v1/messages/thread`. Scrolling to the oldest loaded message fetches
only older messages for that conversation, using a separate cursor and retry
state. Sent and received messages share a thread by their normalized peer
number or sender ID; contact names are display labels only.
Every history view requests at most one page per scroll to the end. Short lists
offer **Load more conversations**, **Load older messages**, or
**Load more results** according to the view. Finishing
a page, staying at the end, or dismissing an error never starts another request
automatically. Threads show **No older messages** when history is exhausted.
In search, exhausted results show **No more results**; errors offer **Retry
search**, which retries only the failed page.
A failed page pauses automatic loading and offers **Retry older
messages**. In a thread, **Refresh** starts a fresh thread history from the
newest page. In the inbox, **Refresh** restarts the conversation list and its pagination
from the newest page. The viewer does not poll in the background.

Connection settings let you disconnect and clear the saved credential. Messages
are held in memory only and fetched again after a process restart. The token is
encrypted using an Android Keystore AES-GCM key. App backup and cleartext network
traffic are disabled. Contacts are queried locally with Android `PhoneLookup`;
the address book is never sent to the receiver. Search queries are sent over
HTTPS in the request body. Names refresh when the app resumes.

This first version only views forwarded SMSes. It requests `INTERNET` and
optional `READ_CONTACTS`; it does not request local SMS, call, or USSD access.

## Verification

```sh
cd receiver
npm test
npm run typecheck
cd ../android
./gradlew testDebugUnitTest lintDebug assembleDebug
```

Receiver tests exercise read authorization, credential separation, pagination,
indexed search, cursor/query binding, search backfill, empty history, cursor
limits, and existing upload/replay behavior. Android unit
tests cover conversation grouping, ordering, deduplication, and URL/token
validation, search debounce/cancellation, stale responses, separate pagination,
and failed-page retries. On a device, also check denied/revoked Contacts access, name lookup
with international numbers, reconnect after restart, offline retry, older
history, dark mode, and long/Unicode SMSes.

An opt-in live-device check is included in `src/androidTest`. Build it with
`./gradlew assembleDebugAndroidTest` and install the test APK from
`app/build/outputs/apk/androidTest/debug/`. Its instrumentation runner is
`dev.kaiforward.app.test/dev.kaiforward.app.DeviceSmokeRunner`; it accepts
`receiver_url` and `read_token` instrumentation arguments. The check reads two
history pages, resolves available contact names, verifies encrypted credential
storage, saves the connection, and launches the app. It reports counts without
logging message contents, contact names, or credentials. Without both arguments
it skips live access. Keep credential arguments out of shell history.

The same runner accepts `-e search_pagination true` or `-e thread_pagination true`
for synthetic UI regression checks with no credentials: idle views must make
zero requests, each history/retry tap must request exactly one page, failed
pages must wait for retry, and an exhausted cursor must show the completion
label. Each check restores the normal app screen afterward.
