#!/usr/bin/env bash
# Proof migration: verifies the run-once mechanism end to end. Side-effect-free —
# it only prints, so it is safe if the ledger is ever wiped and it runs again.
set -euo pipefail
echo "migration 0001_hello ran at $(date -Is)"
