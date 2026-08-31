#!/usr/bin/env bash
# Run-once probe: report what this Pi actually is, into the deploy journal.
#
# Strictly read-only — every command is a query. Nothing is installed, changed
# or written, so this is safe if the ledger is ever wiped and it runs again.
#
# It exists because three pending decisions all hinge on facts about this
# machine that cannot be read from the repo, and there is no SSH to go look:
#   - which Grafana image a local debug stack must pin, so the dashboard is
#     drawn by the same version that draws it here (a different major migrates
#     the dashboard's schemaVersion on load and can render differently);
#   - whether grafana-image-renderer is viable at all — it ships no bundled
#     Chromium for ARM, and needs ~300 MB of RAM to render a single panel next
#     to influxd, grafana-server and three Python services;
#   - whether InfluxDB 2 containers even exist for this architecture, should
#     the datastore ever move into Docker (no official armv7 images).
#
# Nothing here may fail the migration. With `set -e` a single missing binary
# would abort it, leave it unrecorded and STOP the runner — blocking every
# later migration on a script that only prints. So every probe is gated on the
# binary existing, and reports one clean line when it does not.
#
# Read the result on Telegram with `/logs deploy 120`. It arrives as a .txt
# attachment: this prints well past the 15-line inline limit.
set -euo pipefail

# Run a probe, folding stderr into stdout — some tools print their version
# there — but reporting a missing binary as a single line of our own. Without
# the `command -v` gate the shell writes its own "command not found" to stderr,
# which journald interleaves with this report; noise in the /logs window is the
# one thing that reliably makes a remote diagnostic unreadable.
probe() {
    local bin="$1"; shift
    if command -v "$bin" > /dev/null 2>&1; then
        "$bin" "$@" 2>&1 || echo "$bin: installed but exited non-zero"
    else
        echo "$bin: not installed"
    fi
}

# Package version straight from dpkg — the answer to trust when pinning a
# container image tag, since the binary was renamed across Grafana majors.
pkg() {
    if command -v dpkg > /dev/null 2>&1; then
        dpkg-query -W -f='${Package} ${Version} ${Status}\n' "$1" 2>/dev/null \
            || echo "$1: not an apt package"
    else
        echo "dpkg: not installed"
    fi
}

echo "=== 0007 probe: $(date -Is) ==="

echo "--- machine ---"
# The device-tree model is NUL-terminated; strip it or the journal line garbles.
# Redirection failure is the shell's own, so silence it outside the subshell.
echo "model       : $( ( tr -d '\0' < /proc/device-tree/model ) 2>/dev/null || echo unavailable )"
echo "kernel arch : $(uname -m 2>/dev/null || echo '?')"
# Not the same question as the line above: Raspberry Pi OS runs a 64-bit kernel
# under a 32-bit userland, and the two answers diverge. Container images follow
# the kernel arch; apt packages follow this one.
echo "userland    : $(dpkg --print-architecture 2>/dev/null || echo '?')"
echo "kernel      : $(uname -r 2>/dev/null || echo '?')"
grep PRETTY_NAME /etc/os-release 2>/dev/null || echo "os-release  : unavailable"

echo "--- resources ---"
probe free -m
probe df -h /

echo "--- grafana ---"
# v10+ ships `grafana server`; older debs ship `grafana-server`. Ask both.
probe grafana-server -v
probe grafana -v
pkg grafana
# Says whether the image renderer is already installed.
probe grafana-cli plugins ls

echo "--- influxdb ---"
probe influxd version
pkg influxdb2
probe influx version

echo "--- docker ---"
# Relevant only to the open question of moving Grafana/InfluxDB into containers.
probe docker --version

echo "=== 0007 probe done ==="
