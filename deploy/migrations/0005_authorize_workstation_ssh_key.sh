#!/usr/bin/env bash
# Install the workstation public key in the deploy user's authorized_keys, restoring
# SSH access after the account password was lost. Runs as SERVICE_USER (the same user
# sshd authenticates), so no sudo and no password are involved.
# Idempotent: the key body is matched before appending, so a retry after a partial
# failure cannot duplicate the entry.
set -euo pipefail

KEY="ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIBDTpinmuqeOJP3FP6CKuAdfGSzvNStjboHUh5cLnVh5 loicw@workstation"
# Match on the base64 body only: the trailing comment is cosmetic and may differ.
KEY_BODY="$(printf '%s' "$KEY" | cut -d' ' -f2)"

SSH_DIR="$HOME/.ssh"
AUTH="$SSH_DIR/authorized_keys"

# sshd refuses keys under permissions looser than these, so set them every run
# rather than assuming the directory already existed with the right mode.
mkdir -p "$SSH_DIR"
chmod 700 "$SSH_DIR"
touch "$AUTH"
chmod 600 "$AUTH"

if grep -qF "$KEY_BODY" "$AUTH"; then
    echo "key already authorised for $(whoami) in $AUTH"
else
    printf '%s\n' "$KEY" >> "$AUTH"
    echo "key appended for $(whoami) to $AUTH"
fi
