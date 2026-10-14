# KaiForward call relay

A small server that lets a browser answer, hang up and place calls on the
phone, with two-way call audio. Convex receives SMS but cannot hold the
phone's long-lived audio connection. Run this server on a machine that the
phone can reach over Tailscale.

- The phone connects to `--phone-host:--phone-port` (default port 8769) over
  TLS. It authenticates with its receiver `token`.
- The browser page is plain HTTP on `127.0.0.1:8768`. It has no styling, only
  the call controls. Audio is streamed live and never stored.

Requires Python 3.11+ with `websockets` 13 or newer (`pip install websockets`).

## Run

```sh
umask 077
printf '%s\n' '<receiver token>' > token     # the phone's `token` setting
python3 call-relay/server.py --token-file token \
  --certificate relay.crt --key relay.key \
  --phone-host <tailscale-ip>
```

`--phone-host` defaults to `0.0.0.0`. Set it to the machine's Tailscale IP
(`tailscale ip -4`) so that only the tailnet can reach the phone listener.

### Certificate

The phone verifies the server certificate. It tries
`/data/kaiforward/relay-ca.pem` first, then `ca.pem`, then the bundled public
roots. The simplest setup is a self-signed certificate for the Tailscale IP:

```sh
openssl req -x509 -newkey ec -pkeyopt ec_paramgen_curve:P-256 -nodes -days 3650 \
  -subj /CN=kaiforward-relay -addext "subjectAltName=IP:<tailscale-ip>" \
  -keyout relay.key -out relay.crt
adb push relay.crt /data/kaiforward/relay-ca.pem
adb shell chmod 600 /data/kaiforward/relay-ca.pem
```

`python3 -m scripts.configure_native --relay-ca relay.crt --install` installs it
too. A certificate from `tailscale cert` needs no `relay-ca.pem`, but the phone
must then resolve the MagicDNS name.

### Phone setting

Set the phone's `relay_url` to `tls://<tailscale-ip>:8769` using any of these:
- the keypad (**Settings → Call relay URL**)
- `python3 -m scripts.settings_native set` with `{"relay_url":"tls://…"}`
- `PATCH /v1/settings`

The port defaults to 8769 when omitted. An empty `relay_url` disables the
call relay. The SMS `url` is separate.

## Browser access

Open <http://127.0.0.1:8768> on the server. Browsers allow microphone access
only on `localhost` or HTTPS. To use the page from another tailnet device,
publish it with Tailscale's HTTPS proxy and allow that host name:

```sh
tailscale serve --bg 8768
python3 call-relay/server.py ... --allow-host <machine>.<tailnet>.ts.net
```

Then open `https://<machine>.<tailnet>.ts.net`. Every device on the tailnet can
then control calls, so use Tailscale ACLs to limit access.

Only one SIM can have a call at a time, so the page shows a single call and
names its SIM, for example "SIM 2: incoming call". **Answer** and **Hang up**
act on that call. **Call SIM 1** and **Call SIM 2** dial the number entered
from the chosen SIM. Answering or dialling turns the audio on automatically.
**Audio on** connects to a call that is already active. Only one browser can
use the audio at a time. If the browser cannot open a microphone, the call
still goes ahead listen-only: the caller hears silence and the status line
shows why the microphone failed.

## systemd

```ini
[Unit]
Description=KaiForward call relay
After=network-online.target tailscaled.service
Wants=network-online.target

[Service]
ExecStart=/usr/bin/python3 /path/to/kaiforward/call-relay/server.py --token-file /etc/kaiforward/token --certificate /etc/kaiforward/relay.crt --key /etc/kaiforward/relay.key --phone-host <tailscale-ip>
Restart=on-failure
RestartSec=3
UMask=0077
NoNewPrivileges=true

[Install]
WantedBy=multi-user.target
```

If the server starts before Tailscale has assigned its IP, the bind fails. The
`Restart=on-failure` setting retries until the address is available.

## Protocol

The protocol is the PoC's call protocol. Each frame is a 4-byte big-endian
length, then a 1-byte type, then the payload. The types are:
- `HELLO` `{"protocol":1,"token":…}`
- `STATE`
- `DOWNLINK` and `UPLINK`: 320-byte, 8 kHz, 16-bit mono PCM
- `COMMAND` and `RESULT`
- `START` and `STOP`
- `PING`

`tests/test_call_relay.py` covers authentication, framing, command certainty
and audio ownership.
