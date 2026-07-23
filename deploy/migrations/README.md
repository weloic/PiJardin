# One-time migrations

Scripts here run **exactly once** on each Pi, then never again — the run-once
counterpart to the idempotent, path-diff–triggered steps in `deploy.sh`. Use one
whenever a deploy must trigger a one-shot action: a data backfill, an InfluxDB
schema change, flashing new Arduino firmware, etc.

## How it works

At the end of every deploy, `deploy/deploy.sh` walks this folder in filename order
and runs each script that is **not** yet listed in the ledger
`deploy/.migrations_applied` (gitignored, per-Pi, so `git reset --hard` never
re-runs a migration). On exit 0 the name is appended to the ledger. On any non-zero
exit the migration stays unrecorded, admins get a Telegram warning, and the runner
**stops** — that migration and every later one are retried on the next deploy
(every 15 min). Stopping keeps ordering intact when a later migration depends on an
earlier one.

Each migration is invoked as `bash <file>` with these environment variables set:

| Variable             | Meaning                                             |
|----------------------|-----------------------------------------------------|
| `REPO_DIR`           | Absolute path to the repo checkout on the Pi        |
| `VENV_DIR`           | Python virtualenv dir (use `$VENV_DIR/bin/python`)  |
| `TELEGRAM_BOT_TOKEN` | Bot token, so a migration can notify admins itself  |

## Convention

- Name: `NNNN_short_description.ext`, `NNNN` a strictly increasing 4-digit prefix
  (`0001_`, `0002_`, …). The prefix defines run order.
- Any language; include a shebang. Keep it short — put real logic in a reusable
  module and have the migration call it (see the Arduino flash migration).
- Exit 0 only on success; a non-zero exit means "retry me next deploy".
- Make migrations safe to re-run: a migration can fail partway and be retried, so
  guard against double-application where it matters.
- **Never edit, rename, or renumber a migration once it has run** anywhere — the
  ledger tracks it by exact filename, so a change would silently never re-run (or,
  if renamed, run again). To redo work, add a new higher-numbered migration.

## Example

```bash
#!/usr/bin/env bash
set -euo pipefail
echo "backfilling …"
exec "$VENV_DIR/bin/python" "$REPO_DIR/tools/backfill.py"
```
