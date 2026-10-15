# KaiForward call relay

A small server that lets a browser answer, hang up and place calls on the
phone, with two-way call audio, and run USSD codes such as balance checks. Convex receives SMS but cannot hold the
phone's long-lived audio connection. Run this server on a machine that the
phone can reach over Tailscale.

- The phone connects to `--phone-host:--phone-port` (default port 8769) over
  TLS. It authenticates with its receiver `token`.
- The browser page is plain HTTP on `127.0.0.1:8768`. It has no styling, only
  the call controls. Audio is streamed live and never stored.

Requires Python 3.11+ with `websockets` 13 or newer (`pip install websockets`).

## Set up

On the server machine, with the phone connected over root ADB, run from the
repository root:

```sh
python3 -m scripts.call_relay
```

This prepares the server's private credentials in `data/call-relay-server/`
(git-ignored, mode 0600) and configures the phone:

| File | Purpose |
| --- | --- |
| `token` | Copied from the phone's `token` setting. The server accepts only a phone that presents it. It is also the SMS upload token for Convex, so keep it private. |
| `relay.crt`, `relay.key` | Self-signed certificate for the server address, valid for 10 years, and its private key. The phone accepts only a server presenting this certificate. |

On the phone, it installs `relay.crt` as `/data/kaiforward/relay-ca.pem` and
sets `relay_url` to `tls://<host>:8769`. It never prints the token.

The address defaults to this machine's Tailscale IPv4 (`tailscale ip -4`).
Use `--host` for another address or a MagicDNS name, `--port` for another
port, and `--serial` to choose a phone. Running it again is safe. It recopies
the token and keeps the certificate unless the address changed, the
certificate expires within 30 days, or `--renew` is given. Rerun it after
changing the phone's token or moving the server.

The phone tries `relay-ca.pem` first, then `ca.pem`, then the bundled public
roots. A certificate from `tailscale cert` therefore needs no `relay-ca.pem`,
but the phone must then resolve the MagicDNS name. `relay_url` can also be
changed from the keypad (**Settings → Call relay URL**), with
`scripts.settings_native set`, or with `PATCH /v1/settings`. An empty
`relay_url` disables the call relay; the SMS `url` is separate.

## Run

The setup script prints the exact command. For the defaults:

```sh
python3 call-relay/server.py --token-file data/call-relay-server/token \
  --certificate data/call-relay-server/relay.crt --key data/call-relay-server/relay.key \
  --phone-host <tailscale-ip>
```

`--phone-host` defaults to `0.0.0.0`. Set it to the machine's Tailscale IP so
that only the tailnet can reach the phone listener. The server reads the token
at startup, so restart it after rerunning the setup script.

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

## USSD

USSD needs both the updated SMS service and call helper on the phone:

```sh
python3 -m firmware.scripts.build_call_relay
python3 -m scripts.update_native --build --relay-runtime dist/native-call-relay
```

Enter a code such as `*123#` and press **USSD SIM 1** or **USSD SIM 2**. The
page shows the network's answer under the buttons. When the network asks for
a reply ("reply needed", as with a menu), enter the choice, such as `1`, and
press the same SIM's button. **Cancel USSD** ends a waiting or open session.
Codes may contain only digits, `*`, `#` and `+`, up to 40 characters.

Each SIM holds one exchange at a time. A new code can't be sent while the
network has not answered the last one; after 60 seconds without an answer the
exchange is marked failed. A code is sent once, like a call operation, and is
never retried. USSD also works during a call on the same SIM, but not while
the other SIM has a call.

The phone keeps only the latest USSD text per SIM in memory. It is not stored,
uploaded to Convex, or written to the phone's radio log. From a root shell on
the phone, the same operations are available without the server:

```sh
kaiforward-native --call 1 ussd '*123#'
kaiforward-native --call 1 ussd-read
kaiforward-native --call 1 ussd-cancel
```

`ussd-read` prints `state` (`idle`, `waiting`, `reply`, `done`, `failed`,
`cancelled`), the network's `message`, its RIL USSD `type` (`-1` if none),
`error` (a modem error, `-1` lost modem connection, `-2` no answer), and an
`id` that changes with every update.

## systemd

```ini
[Unit]
Description=KaiForward call relay
After=network-online.target tailscaled.service
Wants=network-online.target

[Service]
ExecStart=/usr/bin/python3 /path/to/kaiforward/call-relay/server.py --token-file /path/to/kaiforward/data/call-relay-server/token --certificate /path/to/kaiforward/data/call-relay-server/relay.crt --key /path/to/kaiforward/data/call-relay-server/relay.key --phone-host <tailscale-ip>
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
- `COMMAND` and `RESULT`: call commands, and `ussd <code>` and
  `ussd-cancel`. `STATE` carries a `ussd` array with each SIM's latest
  exchange, or `null` for a SIM when the phone's SMS service predates USSD.
- `START` and `STOP`
- `PING`

`tests/test_call_relay.py` covers authentication, framing, command certainty,
audio ownership and USSD validation.
