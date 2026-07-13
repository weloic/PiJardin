###################################################################################################
# -------------------------------------------------------------------------------------------------
# IMPORTS
import io
from zoneinfo import ZoneInfo

## The bot runs as a headless systemd service (no display); select the Agg backend
## BEFORE importing pyplot so matplotlib never tries to open a GUI window.
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.dates as mdates

# -------------------------------------------------------------------------------------------------
# CONFIGURATION

## InfluxDB stores timestamps in UTC; show them in local time on the axis.
LOCAL_TZ = ZoneInfo("Europe/Zurich")

## Green line, matching the Grafana "Historique" panels' overall look.
LINE_COLOR = "#3fa34d"

# -------------------------------------------------------------------------------------------------
# FUNCTIONS

def render_volume_chart(times, volumes_m3, title, autoscale_y=False):
    """Render a volume-over-time line chart to PNG bytes.

    times: list of timezone-aware (UTC) datetimes.
    volumes_m3: parallel list of volumes in m³.
    title: chart title (also used as the caption by the caller).

    Returns the PNG image as bytes.
    """
    local_times = [t.astimezone(LOCAL_TZ) for t in times]

    fig, ax = plt.subplots(figsize=(9, 4.5), dpi=110)
    ax.plot(local_times, volumes_m3, color=LINE_COLOR, linewidth=1.6)

    ax.set_title(title)
    ax.set_ylabel("Volume (m³)")
    if autoscale_y:
        lo, hi = min(volumes_m3), max(volumes_m3)
        pad = (hi - lo) * 0.1 or (abs(hi) * 0.01 or 1)
        ax.set_ylim(lo - pad, hi + pad)
        baseline = lo - pad
    else:
        ax.set_ylim(bottom=0)
        baseline = 0
    ax.fill_between(local_times, volumes_m3, baseline, color=LINE_COLOR, alpha=0.12)
    ax.grid(True, alpha=0.3)

    ## Let matplotlib pick sensible date ticks for the window, formatted compactly.
    locator = mdates.AutoDateLocator()
    ax.xaxis.set_major_locator(locator)
    ax.xaxis.set_major_formatter(mdates.ConciseDateFormatter(locator, tz=LOCAL_TZ))

    fig.tight_layout()

    buf = io.BytesIO()
    fig.savefig(buf, format="png")
    plt.close(fig)
    return buf.getvalue()
