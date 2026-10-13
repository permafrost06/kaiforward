# KaiForward

The refined project will be developed on `master`, which starts with its own
root commit and shares no commit history with the `poc` branch.

The original proof of concept is preserved on the public repository's
[`poc` branch](https://github.com/permafrost06/kaiforward/tree/poc).
The local `poc/` directory is a separate clone checked out on that branch and
is ignored by this repository.

To recreate the reference checkout:

```sh
git clone --branch poc --single-branch git@github.com:permafrost06/kaiforward.git poc
```

Run the POC checks from its checkout:

```sh
cd poc
npm ci --ignore-scripts
npm test
python3 -m unittest discover -s tests -v
cd viewer-ui
pnpm install --frozen-lockfile
pnpm run build
```

Local private data, backups, dependencies, and generated artifacts are
preserved inside `poc/` on the original development machine. They are excluded
from Git and are not included in a fresh clone. Existing services and external
commands that refer to the old project paths need to use the `poc/` paths.

## Root access

Reading partitions needs root on the phone. Custom firmware such as the Gerda
build runs `adbd` as root. Stock firmware does not, so start the temporary root
shell first:

```sh
python3 -m scripts.access
```

Then open **http://127.0.0.1:8767** in the phone's browser and press
**Enable temporary access**. The command returns once the shell answers.

The page (`scripts/access.html`) calls the firmware's `startUniversalCommand`
API, which the browser can reach on this firmware. The root `b2g` system
process runs the command: `busybox telnetd` bound to the phone's loopback
interface. Nothing is installed, and rebooting the phone stops it. The page is
served from the PC through an ADB reverse tunnel; the phone's browser does not
open `file://` addresses. `scripts/root_shell.py` reaches the shell through an
ADB forward from PC port 12323 to phone port 2323.
