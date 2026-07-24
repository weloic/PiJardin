###################################################################################################
# -------------------------------------------------------------------------------------------------
# Shared logging setup for all PiJardin Python entrypoints.
#
# The logging model is stdout -> journald (see README): every service writes to stdout and
# systemd's journal captures it, so we never manage log files or rotation ourselves. This module
# is the single place that configures Python's logging. It also provides an optional InfluxDB
# handler that records only WARNING+ events from the data-collection path (the scheduled
# sensors run) — the Telegram bot deliberately never attaches it.
import logging
import os
import subprocess
import sys

# journald and `/logs -o short-iso` already timestamp every line, so no asctime here.
DEFAULT_FORMAT = "%(levelname)s [%(name)s] %(message)s"


def setup_logging(level=None):
    """Configure root logging: stdout, line-buffered, `LEVEL [name] message`.

    Idempotent — logging.basicConfig is a no-op once the root logger has handlers, so calling
    this from every entrypoint is safe. Level comes from the arg or the LOG_LEVEL env var
    (default INFO), so a service can be bumped to DEBUG via systemd without a code change.
    """
    if level is None:
        level = os.getenv("LOG_LEVEL", "INFO")

    # Line-buffer stdout so log/print lines reach the journal immediately (systemd otherwise
    # block-buffers a non-tty), and so an imported module's output interleaves correctly.
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except (AttributeError, ValueError):
        pass  # not a real stream (e.g. captured in a test harness); harmless

    logging.basicConfig(level=level, stream=sys.stdout, format=DEFAULT_FORMAT)

    # httpx logs every request at INFO — including the bot token in the URL. Keep it (and the
    # journal read via /logs) quiet unless something goes wrong.
    logging.getLogger("httpx").setLevel(logging.WARNING)


# -------------------------------------------------------------------------------------------------
# VERSION HELPERS (used by the deploy-time / flash version marker)

def _git_short(repo_dir, rev):
    try:
        result = subprocess.run(
            ["git", "-C", repo_dir, "rev-parse", "--short", rev],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0:
            return result.stdout.strip() or "unknown"
    except Exception:
        pass
    return "unknown"


def pi_version(repo_dir):
    """Short git hash of the deployed repo — the 'Pi firmware' version."""
    return _git_short(repo_dir, "HEAD")


def grafana_version(repo_dir):
    """Short hash of the grafana/ subtree — bumps only when Grafana config actually changes."""
    return _git_short(repo_dir, "HEAD:grafana")


def arduino_version(repo_dir):
    """The `version=` value from arduino/VERSION, or 'unknown' if absent.

    Kept in sync with flash_firmware.read_version; duplicated (not imported) because importing
    flash_firmware pulls in read_puit/alerts and their side effects.
    """
    path = os.path.join(repo_dir, "arduino", "VERSION")
    try:
        with open(path) as f:
            for line in f:
                if line.startswith("version="):
                    return line.split("=", 1)[1].strip()
    except OSError:
        pass
    return "unknown"


# -------------------------------------------------------------------------------------------------
# INFLUXDB LOG HANDLER

class InfluxLogHandler(logging.Handler):
    """Record WARNING+ log records to InfluxDB as Point('log').

    Attached only in the data-collection path (read_puit.__main__), never in the Telegram bot.
    A failed InfluxDB write must never crash the caller, so emit() routes any exception to
    handleError() (which just prints to stderr) rather than propagating.
    """

    def __init__(self, write_api, bucket, org, level=logging.WARNING):
        super().__init__(level)
        self.write_api = write_api
        self.bucket = bucket
        self.org = org

    def emit(self, record):
        try:
            from influxdb_client import Point  # lazy: keep this module importable without the dep
            point = (
                Point("log")
                .tag("level", record.levelname)
                .tag("source", record.name)
                .field("message", self.format(record))
            )
            self.write_api.write(bucket=self.bucket, org=self.org, record=point)
        except Exception:
            self.handleError(record)


def attach_influx_handler(write_api, bucket, org, level=logging.WARNING):
    """Add an InfluxLogHandler to the root logger. No-op (returns None) if write_api is None.

    Call this ONLY from the scheduled sensors path — the Telegram bot must not write to
    InfluxDB. Returns the handler so callers can inspect/remove it if needed.
    """
    if write_api is None:
        return None
    handler = InfluxLogHandler(write_api, bucket, org, level=level)
    logging.getLogger().addHandler(handler)
    return handler
