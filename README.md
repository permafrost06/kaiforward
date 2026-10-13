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
