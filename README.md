# KaiForward

The [native Gerda UI and SMS service](firmware/NATIVE_UI.md) replaces the
phone's browser interface with a keypad status/settings screen and forwards
SMS over configurable Wi-Fi. It is installed; native boot, Wi-Fi/HTTPS
reconnection, SMS reception and call status have been verified.

The [native call relay](firmware/CALL_RELAY.md) now answers cellular calls in
a browser on this PC. Open **http://127.0.0.1:8768**, select SIM 2 and click
**Answer** when it rings. Allow microphone access; use **Hang up** to end the
call. The owner confirmed audio in both directions over Wi-Fi and browser
hangup. SMSes remain available at **http://127.0.0.1:8766**.
Latest complete firmware: `dist/gerda-native-calls-20261013`.
The stock-app setup below records the earlier prototype and its restore path.

Copies received and sent SMS from a Nokia 8110 4G to a private receiver. The current prototype targets a TA-1059 running stock KaiOS 2.5, with reported firmware `12.00.17.10` (the device's Android build property reports `12.00.17.06`). GerdaOS is not required for the installed prototype.

## Earlier stock-app device setup

KaiForward is installed as a removable certified app, `app://kaiforward.local`. The current revision was installed using the normal Gecko debugger protocol. The system, boot, recovery and modem partitions were not written.

The receiver listens on this PC's loopback interface. An ADB reverse tunnel makes it available to the phone at `http://127.0.0.1:8765`. This test setup requires the phone to stay connected by USB and the receiver process to remain running. Wi-Fi/mobile-data syncing will require a reachable HTTPS server and a newly configured app package.

Open **KaiForward** on the phone to start the first SMS scan. It copies existing received and successfully sent SMSes as well as future incoming and outgoing SMSes. Messages are neither deleted nor marked read. MMS and outgoing messages still sending or marked failed are excluded.

The app stores pending messages in IndexedDB before uploading them. It keeps them until the receiver acknowledges the entire batch after committing to SQLite. An ID and timestamp form the deduplication key. It scans the SMS database to recover missed incoming and outgoing events and schedules a retry alarm five minutes ahead. The button shows “Syncing…” during work, the scan reports progress, and an SMS database that stops returning scan results produces an error after 45 seconds without progress. Background receipt, retry after loss of the upload connection, and automatic recovery after reboot have been verified on this device.

## Run the PC receiver

```sh
python3 server/receiver.py
adb -s 22aaa475 reverse tcp:8765 tcp:8765
```

The first run generates a random authentication token in `data/config.json`. The app package embeds that token. Messages are stored in `data/sms.sqlite3`. These files and generated packages have private filesystem permissions and are excluded from Git.

## Read messages in your browser

Open **http://127.0.0.1:8766** on this PC. The React viewer uses shadcn/ui components and shows complete message bodies, senders or recipients, and Sent/Received badges with local timestamps. Search by contact or message text, browse older pages, or use Refresh. Sort the whole archive by newest/oldest or contact A–Z/Z–A, including search results. Filter by All SMSes, Received or Sent. The number filter offers **All numbers** or **+880 or 01**; the latter matches those exact starting prefixes on received senders and sent recipients, and combines with search, direction, sorting and pagination. Choose 25, 50 or 100 messages per page. The first page refreshes automatically every 30 seconds when you are not searching or selecting text.

The theme defaults to **Auto**, follows the PC's light/dark setting, and updates immediately when the system preference changes. Explicit Light and Dark overrides are also available. Theme and sort choices are remembered in the browser.

The viewer is read-only and runs separately from the SMS receiver. It can display already saved messages even when the phone is disconnected. The receiver and viewer run as systemd user services, start when you log in, and restart after a crash. They do not depend on this chat's command sessions. View their status or restart them:

```sh
systemctl --user status kaiforward-receiver kaiforward-viewer
systemctl --user restart kaiforward-receiver kaiforward-viewer
```

The installed unit files are copied from `deploy/systemd/` and use this checkout's absolute path. To reinstall them on this PC:

```sh
mkdir -p ~/.config/systemd/user
cp deploy/systemd/kaiforward-*.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now kaiforward-receiver kaiforward-viewer
```

Stop any manually launched receiver/viewer before starting the services, since they use the same ports. To stop the services and disable login startup, use `systemctl --user disable --now kaiforward-receiver kaiforward-viewer`. The PC must be awake and the USB tunnel must be restored after a phone reboot with `adb -s 22aaa475 reverse tcp:8765 tcp:8765`.

The frontend is built into `server/viewer-build/`. After editing `viewer-ui/`, rebuild it:

```sh
cd viewer-ui
npm install
npm run build
```

The source includes official shadcn/ui Button, Input, Badge, Card, Skeleton and NativeSelect components and their MIT license.

It binds only to the PC's loopback interface, rejects cross-origin archive requests, and reads the SQLite database in read-only mode.

Check the archived count without displaying message contents:

```sh
python3 -c 'import sqlite3; print(sqlite3.connect("data/sms.sqlite3").execute("SELECT COUNT(*) FROM messages").fetchone()[0])'
```

The API accepts `POST /v1/messages`, with a bearer token and a JSON body containing `device_id` and a batch of 1–50 messages. Each message contains `key`, `id`, `timestamp` (Unix milliseconds), `sender`, `recipient`, `direction` (`received` or `sent`) and `body`. Sent messages require a recipient. Records from the received-only app remain compatible: missing direction defaults to `received` and missing recipient to an empty string. Replaying an identical message succeeds without creating another row. Conflicting content for the same key returns HTTP 409 and rolls back the batch. `GET /v1/stats` requires the same token; `GET /health` returns no message data.

This standard-library HTTP server is for the local USB test. Public hosting is a separate deployment step.

## Build and test

```sh
python3 tools/package_phone.py
npm ci --ignore-scripts
npm test
python3 -m unittest discover -s tests -v
```

The phone app uses JavaScript syntax supported by Gecko 48. Test dependencies are only used on the PC.

## Installation tools and backups

`tools/root_shell.py` connects to the temporary root shell at forwarded PC port 12323. `tools/device-access/index.html` enables that shell using the older firmware's command API. The daemon binds to the phone's loopback interface, not its Wi-Fi interface; rebooting stops it.

`tools/backup_firmware.py` copies firmware partitions and compares each phone-side SHA-256 with the PC copy. The verified backup is in `backups/TA-1059-12.00.17.10-20261012-123621/`: 30 partitions, 1,033,380,864 bytes. Userdata and cache were excluded, so this is not a full backup of messages, applications or settings. A separate KaiOS profile archive and the exact pre-install app registry and permissions database are also stored there.

`tools/install_phone.py` uses the normal Gecko debugger installation protocol. Connections can require accepting a remote-debugging prompt. This is the preferred installation path and succeeded for the current revision.

On this device, subsequent debugger connections have sometimes closed immediately after one successful connection. Toggling debugging and reconnecting USB did not clear that state. A full phone reboot restored a working connection for the sent-SMS update. If this happens again, reboot the phone, restore the ADB forward and reverse tunnels, keep the phone unlocked, and run the installer directly in its first debugger session rather than testing a separate session first. The installer closes the existing KaiForward window before updating it and launches the new version in the same debugger connection.

The initial installation used `tools/install_phone_offline.py`. Restarting only the KaiOS interface was followed by both SIMs showing “No SIM card” and an inbox scan that never completed. `tools/disable_phone.py` removed only KaiForward's registration and permissions, retaining its files. A full phone reboot restored both SIMs. Normal debugger installation then succeeded, and the receiver stored 3,433 SMSes. The precise cause of the radio state problem has not been established. Avoid the interface-only restart path on this device; use the normal debugger installer.

The offline installer and disable tool save the original registry and permissions and roll back their database edits on failure. They now request a full phone reboot after stopping KaiOS, including after rollback. They are retained for recovery and documentation, not as the preferred installation workflow.

The device tools contain this phone's serial number and profile path. They are specific to this device, not general-purpose flashing instructions. No restoration or firmware flashing command is run automatically.

## Verified behavior

The initial inbox upload copied 3,433 SMSes. Two fresh test SMSes brought the total to 3,435, with no duplicate keys.

1. Replaying a real stored SMS through the receiver API was acknowledged with exactly one stored copy remaining.
2. A fresh SMS uploaded with KaiForward closed and the screen off.
3. With the USB upload tunnel removed, another SMS arrived and the PC count stayed unchanged. The phone was rebooted, and the tunnel was recreated. Its retry alarm uploaded the message automatically while KaiForward stayed closed, approximately four minutes after monitoring began.
4. The user confirmed both SIMs remained detected after that reboot.

The sent-SMS update was installed using the normal debugger protocol after another full reboot; the user again confirmed both SIMs were detected. It copied 10 existing sent SMSes, bringing the archive to 3,445 messages. All 3,435 original received message keys remained present, with no duplicates. Recipient fields and the viewer's Sent filter were checked, and the phone reported “Sync complete”. New outgoing system events and their offline persistence are covered by the phone logic tests; a fresh outgoing SMS has not yet been tested on the physical phone.

Ten phone logic tests, nine receiver tests and ten viewer tests pass, covering received and sent records, outgoing events and offline retries, legacy archive compatibility, recipient search and filtering, queue persistence, invalid acknowledgements, Unicode, deduplication, authentication, batch limits, stalled scan feedback and multiple upload batches.

Temporary rooting and the installation page are not required for everyday synchronization. The temporary root daemon stopped on reboot; the installed app uses its own permissions. The PC receiver remains running for the USB test.
