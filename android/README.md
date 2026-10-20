# KaiForward for Android

A native Kotlin / Jetpack Compose app for reading forwarded SMS from Convex
and sending SMS directly through the KaiForward phone. Requires Android 8.0 (API 26) or newer.

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
The same app token is used for receiver reads and SMS sending directly through the Nokia.
The app does not need the firmware's upload credential.

## Build and install

Open this directory in Android Studio, use a JDK 17 or newer (tested with 21),
and install Android SDK Platform 36. Android Studio can configure the local SDK
path. From the command line, set `ANDROID_HOME` or put `sdk.dir=/path/to/Android/Sdk`
in an untracked `local.properties`.

The APK embeds fixed receiver and Nokia HTTPS origins from `app/build.gradle.kts`
and the shared app token from the ignored `data/android-viewer.env` file:

```properties
KAIFORWARD_READ_TOKEN=<the receiver read token and Nokia app_token>
```

This file is required for all builds. Its value becomes an APK constant; users
cannot view or edit connection values in the app. To change a URL or token,
rebuild and install the APK. Then run:

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
and access to the Nokia's Tailscale network for SMS sending. The APK includes the
app token and both URLs, so share it only with people who should have message
access. Signing credentials and the firmware upload token are not embedded.
Contact permission is optional and uses contacts on the recipient's Android phone.

The release certificate differs from the development debug certificate. To
replace an existing debug installation, first uninstall it, then install the
release APK. This does not delete SMSes stored on the receiver.

## In-app updates

Version 0.7.0 adds a self updater. Install this signed release once using the
existing APK installation procedure; older versions cannot check for updates.
Afterwards, the app checks when it enters the foreground, at most once every
six hours. **More options → Check for updates** checks immediately in the
background without opening a dialog. If there is no newer version, a short
**You’re up to date** toast appears. Manual check failures also use a toast;
automatic check failures stay quiet.

When a newer compatible release is found, the app downloads it automatically
when its active internet connection uses Wi-Fi. On mobile data, it waits for
Wi-Fi and starts without another tap when Wi-Fi becomes available. There are
no checking, update-available, or download-progress dialogs. Downloads are bound
to the selected Wi-Fi connection and stop if that connection is lost or changes;
interrupted transfers restart on the next Wi-Fi connection or update check.
The existing VPN connection is respected, including Wi-Fi-backed Tailscale.

The app verifies the APK's size, SHA-256, package name, version, minimum Android
version, and signing certificate. Only then does **Install KaiForward …?** appear,
with **Install** and **Later** actions. Android may first ask you to allow
KaiForward to install apps; returning from that setting continues installation
if permission was granted. Android's own installation confirmation remains.
The APK is verified again before opening the installer.

Completed downloads and release metadata survive app restarts; an already
verified download can be offered even while offline. **Later** dismisses the
prompt for the current app session; a manual check can reopen it. Cancelling
Android's installer keeps the downloaded update available for another attempt.
Checks and downloads run asynchronously while the app's process is alive; there
is no periodic background worker. If Android stops the process, opening the app
resumes the flow. Message read status and other app data survive an update.
Debug builds cannot install a release update because their signing certificates
differ.

The manifest URL is
`https://kaiforward.permafrost06.com/updates/latest.json`. Version 0.7.0 uses
`https://permafrost06.com/kaiforward/updates/latest.json`, which remains available
with the same authenticated release files for compatibility. Both the manifest and
APKs require HTTP Basic authentication: username `kaiforward`, password the
embedded app token. The APK contains message access credentials, so keep these
downloads authenticated. The Nginx password file lives outside the served
directory. No update request sends credentials to arbitrary hosts: only HTTPS
on `permafrost06.com` and `kaiforward.permafrost06.com` is allowed, including
redirects. If the primary subdomain cannot be resolved, the app checks the
legacy HTTPS endpoint instead, using the same authentication and verification.
Installation always requires confirmation.

### First-time hosting setup

Build and verify the release, then prepare and privately stage its hosting files:

```sh
cd android
./gradlew assembleRelease testReleaseUnitTest lintRelease
cd ..
python3 -m scripts.publish_android_update \
  --base-url https://permafrost06.com/kaiforward/updates/ \
  --notes 'Adds in-app updates.' --stage-host edges-studio@main-ubuntu-sg
```

This bootstraps the legacy path on the existing domain. Run the subdomain
migration below afterwards. The publisher defaults to the subdomain for future
releases.

The publisher requires Android SDK build-tools, `keytool`, and `openssl`. It
reads metadata from the actual APK, verifies its signature against the configured
release key, and writes an ignored bundle to `dist/android-update/`. The staged
bundle is in the deployment user's private `~/kaiforward-update-staging/`, outside
the web root. To activate it, run this once with administrator access:

```sh
ssh -t edges-studio@main-ubuntu-sg sudo python3 kaiforward-update-staging/install-host.py
```

Review the staged `nginx-location.conf` and `install-host.py` first if desired.
The installer serves files from `/srv/kaiforward-updates/public/`, installs a
password file outside that public directory, and adds one include to the existing
HTTPS site's `/etc/nginx/sites-available/permafrost06.com`. It backs up that site
configuration, runs `nginx -t`, and reloads Nginx. A configuration failure restores
the original file. The site's existing routes keep their configuration.

Check that unauthenticated requests to the manifest **and the APK** return 401,
then use the app's manual update check to verify authenticated access. Avoid
putting the password in command-line arguments, URLs, shell history, or logs.

### Publish subsequent versions

Increase `versionCode` and `versionName` in `app/build.gradle.kts`, build with
the same release signing key, and publish:

```sh
python3 -m scripts.publish_android_update \
  --notes 'Describe what changed.' --deploy-host edges-studio@main-ubuntu-sg
```

The publisher uploads the APK to a temporary filename, checks its hash remotely,
then publishes it before atomically replacing `latest.json`. A host-side lock
serializes publication. Downgrades and different APKs reusing an existing
`versionCode` are rejected. Earlier APKs remain available so a client that already
fetched a manifest can finish downloading. Future releases need no Nginx reload.
If you rotate the shared app token, coordinate updating the server password file
with distributing an APK that has the new token; old credentials will stop working.

### Subdomain hosting and legacy compatibility

The updater's primary endpoint is now
`https://kaiforward.permafrost06.com/updates/latest.json`. To migrate an
initialized legacy host, add DNS for `kaiforward.permafrost06.com` pointing to
the same server, stage the migration script, and run it once with sudo:

```sh
scp scripts/migrate_android_update_host.py \
  edges-studio@main-ubuntu-sg:kaiforward-update-staging/migrate-subdomain.py
ssh -t edges-studio@main-ubuntu-sg sudo python3 kaiforward-update-staging/migrate-subdomain.py
```

The migration obtains a Let's Encrypt certificate using the server's existing
Certbot account and an HTTP webroot challenge. It adds a dedicated Nginx site
with the same authenticated files at `/updates/`, preserves the challenge path
for renewal, and installs a deploy hook to reload Nginx after certificate renewal.
The old URL continues to serve the same authenticated files directly. This
allows old installations to update even when their DNS resolver still caches a
negative answer for the newly created subdomain. Certificate or configuration
failures restore the old route and site configuration. Existing
unmanaged subdomain configurations are preserved and require manual integration.

Version 0.7.1 and later default directly to the subdomain. Keep the old domain's
certificate and compatibility route working for version 0.7.0 installations.
After DNS caches have cleared, the migration script can be rerun with
`--redirect-legacy` to use permanent redirects instead, preserving manifest and
APK filenames. Version 0.7.0 supports those redirects, but a stale negative DNS
answer for the new hostname will prevent it from following them. Both endpoints use the same release files and credentials; no
message data or receiver configuration changes. Subsequent releases still use
`--deploy-host` without sudo.

## New message push notifications

Use the **kaiforward** Firebase project and register an Android app with package
name `dev.kaiforward.app`. Download `google-services.json` into
`android/app/google-services.json` (ignored by Git). Rebuild the APK after adding
this file. Builds without it still work as a viewer, with push disabled.

In Firebase **Project settings → Cloud Messaging**, ensure **Firebase Cloud
Messaging API (V1)** is enabled. In **Project settings → Service accounts**,
generate a private service-account JSON key for the same project. The account
must have permission to send FCM messages (`cloudmessaging.messages.create`,
provided by the Firebase Cloud Messaging API Admin role). Store the JSON
privately outside tracked files, for example `data/firebase-service-account.json`.
This private key belongs only in Convex; never put it in the Android app.

Set the entire JSON as a Convex environment variable and deploy the receiver:

```sh
cd receiver
npx convex env set --prod FIREBASE_SERVICE_ACCOUNT_JSON --from-file ../data/firebase-service-account.json
npx convex deploy
```

For a development deployment, omit `--prod` and use `npx convex dev --once`.
Keep the existing upload and read tokens. The Firebase Android configuration and
service account must refer to the same Firebase project. No firmware changes,
Firebase database, Analytics setup, or Play Store release are required.

Build and install the APK using the commands above, open it once while online,
and allow notifications when prompted on Android 13+. Registration is automatic
and retries network failures using WorkManager. Devices need Google Play services.
If permission was declined, enable **Settings → Apps → KaiForward → Notifications**.
Optional Contacts permission resolves sender names locally for notifications.

Newly stored incoming messages trigger a high-priority FCM data message. The app
shows a forwarding-envelope icon, sender/contact name, SIM number (when known),
and expandable SMS text; tapping opens the matching
conversation and message. The preview is limited to 700 Unicode code points;
the conversation contains the full message. Each message keeps its own
notification, including multiple messages in the same conversation. Sender and
preview text pass through Firebase;
contacts remain local. Lock-screen previews follow Android's private-notification
and user settings. **Mark as read** dismisses alerts and marks the conversation
read locally through the selected message without opening the app; later arrivals
remain unread. Deploy the updated receiver to include SIM and server arrival time
in push payloads. Alerts from older receivers omit the quick action because they
lack a safe read cutoff. There is no inline reply action.

Upload replays, sent messages, and migrations do not notify. A newly uploaded
message from an older phone backlog does notify. Failed uploads never notify.
Push failures do not block SMS storage. Convex retries temporary delivery errors
up to four times; the app suppresses duplicate delivery for its 100 most recent alerts.
Expired Firebase tokens are removed, and registrations unused for 60 days are
pruned during fanout. Opening the app renews registration. Rotating the read
token prevents delivery to registrations with the old credential; rebuild and
open the app with the new token to register again.

To verify delivery, open the installed app online, allow notifications, put it in
the background, and send a real SMS to the Nokia. Check the sender/text alert and
that tapping it opens the conversation. Also test with the Android screen locked,
with Contacts denied, and with notifications denied. Force-stopping the Android
app blocks FCM delivery until the app is opened again. Check Convex function logs
for configuration/delivery errors if messages arrive in the inbox without alerts.

Official setup: [Firebase Android setup](https://firebase.google.com/docs/android/setup)
and [FCM Android setup](https://firebase.google.com/docs/cloud-messaging/android/get-started).

## Open and view

Open KaiForward to load conversations automatically using the embedded
connection. Optionally allow Contacts to resolve numbers against this Android
phone's address book. Denying permission keeps the viewer usable with raw numbers.

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
messages**. Pull down at the top of a conversation to refresh its history from
the newest page. In the inbox, pull down to restart the conversation list and its
pagination from the newest page, including when the inbox is empty. The viewer
does not poll in the background.

Read status is stored only on this Android device. The first successful sync
marks all existing history as read, including conversations/pages loaded later.
Incoming SMSes uploaded after that sync are unread until their conversation is
opened successfully. Unread rows have bold text and a dot; opening from search
or a notification also marks the conversation read. Sent SMSes are always read.
Notifications also offer **Mark as read**; there is no manual unread action. Read status survives app updates and
process restarts, and resets when app data is cleared or the app is uninstalled.
The receiver supplies snapshot/arrival times (including an indexed latest
incoming arrival per conversation); it stores no read status. Deploy the receiver
update before installing this app version.

Message contents are held in memory only and fetched again after a process restart.
Previous editable connection preferences are cleared on startup; the app always
uses its embedded configuration. App backup and cleartext network traffic are
disabled. Contacts are queried locally with Android `PhoneLookup`;
the address book is never sent to the receiver. Search queries are sent over
HTTPS in the request body. Names refresh when the app resumes.

## Send SMS

Update the Nokia's native runtime and set its `app_token` setting to the exact
same value as the Convex receiver's `KAIFORWARD_READ_TOKEN`. This uses the same
app token for reading and sending; the Nokia's upload token stays separate.
See [the direct SMS API setup](../firmware/README.md#direct-sms-api).

Expose the Nokia's local HTTP service through Tailscale Serve on the Nokia at
the HTTPS origin embedded in the APK. SMS connects automatically at launch.
Both phones need to be online in the tailnet. Use **Reconnect SMS** in the inbox
or composer after a connection failure. SMS needs no call relay server.

Tap the floating **New SMS** button at the bottom right of the inbox or **Reply** in a conversation.
The floating button shrinks to its chat icon after scrolling down and expands again near the top.
Enter a recipient,
message, and SIM 1 or 2, then tap **Send SMS**. Recipients must have 3–20 digits
with an optional leading `+`; spaces, parentheses, and hyphens are removed.
The composer counts GSM septets or UTF-16 units and prevents messages exceeding
the single-segment limit (160 GSM septets or 70 UTF-16 units).

The phone reports queued, sending, sent, failed, or uncertain status. Queued
means accepted by the phone; unavailable SIMs retain queued jobs. Sent means
accepted by the modem, not a delivery receipt. Sent SMSes upload to Convex
through the existing firmware path. The app refreshes history on sent status;
pull down again if the upload has not arrived yet.

Before submission, the app saves a random request ID and SIM locally, without
recipient or message text. The Nokia saves the request ID and outbox job in
one transaction. Reusing an ID with the same payload returns that job; changing
the payload with that ID is rejected. Submission is never retried automatically.
After a lost response or process restart, **Reconnect SMS** checks the saved ID
with a GET request, without submitting another SMS. If the Nokia has no queued
job for that ID, the app allows an explicit Send tap using the same ID, so a
late original request cannot create a duplicate. If an earlier request already
contains different text or a different SIM, the app keeps the new draft and
checks the earlier job. If the result remains unknown, check with the recipient
before sending again.

While the app is in the foreground, it checks the last job every two seconds
until a terminal status arrives. Checks pause in the background and resume when
the app becomes active. Network failures pause checks until reconnect. An
accepted submission clears the draft; a rejected or unknown submission keeps it.
Draft text stays in memory and clears when the app process exits; tracking IDs
persist until the tracked job is resolved or app data is cleared. Carrier SMS
charges apply to the KaiForward phone.

The app requests `INTERNET`, optional `READ_CONTACTS`, and notification permission; SMS goes through the
KaiForward phone rather than Android's local SMS service.

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
and failed-page retries. SMS tests cover GSM/Unicode limits, recipient validation,
one submission per tap sequence, status updates, timeout uncertainty, and stale
responses after reconnect. On a device, also check denied/revoked Contacts access, name lookup
with international numbers, reconnect after restart, offline retry, older
history, dark mode, and long/Unicode SMSes.

An opt-in live-device check is included in `src/androidTest`. Build it with
`./gradlew assembleDebugAndroidTest` and install the test APK from
`app/build/outputs/apk/androidTest/debug/`. Its instrumentation runner is
`dev.kaiforward.app.test/dev.kaiforward.app.DeviceSmokeRunner`; it accepts
`receiver_url` and `read_token` instrumentation arguments. The check reads two
history pages, resolves available contact names, and launches the app. It reports counts without
logging message contents, contact names, or credentials. Without both arguments
it skips live access. Keep credential arguments out of shell history.

The same runner accepts `-e search_pagination true` or `-e thread_pagination true`
for synthetic UI regression checks with no credentials: idle views must make
zero requests, each history/retry tap must request exactly one page, failed
pages must wait for retry, and an exhausted cursor must show the completion
label. Each check restores the normal app screen afterward.

For a device-only read-status demo, a test APK signed with the same certificate
can run `-e seed_unread_top 3` to seed three recent incoming conversations as
unread locally. `-e verify_read_status true` checks the visible unread rows,
opens one, and verifies its read cutoff was saved. These are instrumentation
options only; they add no controls or receiver mutations to the shipped app.

Run the instrumentation runner with `-e notifications true` (notification permission
required) to check that same-sender and different-sender alerts coexist, tap and
mark-read actions stay distinct, duplicate pushes are suppressed, and marking an
older alert read preserves newer and unrelated alerts. This uses synthetic
notifications and does not contact the receiver.
