# devstack — the Grafana dashboard, off the Pi

The Pi's Grafana listens on its own `localhost` and there is no route to it. This stack solves
that by moving **the data** rather than a picture of the panels: the bot exports, a local
InfluxDB ingests, and a local Grafana mounts **this repo's own provisioning files**. What you
look at is therefore the real dashboard — same panel JSON, same Flux, same datasource uid — and
not a reimplementation that could disagree with the Pi about what the data says.

None of this runs on the Pi or reaches it. `deploy.sh` acts only on the paths it lists
(`systemd/`, `grafana/`, `sensors/`, `common/`, `telegram_bot/`, `deploy/`), so a `git pull`
on the Pi drops this directory on its disk and ignores it.

## Prerequisites

Docker and the compose plugin. On Debian/Ubuntu:

```bash
sudo apt install docker.io docker-compose-v2
sudo usermod -aG docker $USER      # then log out and back in
docker run hello-world             # check
```

## Use

```bash
cd devstack
./up.sh                                    # starts both containers
#   on Telegram (admin):  /export 8d
./import.sh ~/Downloads/pijardin_8d_*.lp.gz
```

Then <http://localhost:3000> — no login, the "PiJardin" dashboard is already provisioned.

The first `up.sh` pulls ~400 MB of images, once. Later ones start in seconds.

To erase everything, data included:

```bash
docker compose down -v
```

Without `-v` the database survives a restart, which is handy for keeping an import between
sessions. With it you are back to a virgin database in ten seconds — and that is the real
reason this is containerised rather than installed: a bad import is replayed, not repaired.

## What matches the Pi, and what does not

The images are pinned to the versions the Pi actually runs — **Grafana 12.0.2**, **InfluxDB
2.7.11** — as measured by `deploy/migrations/0007_probe_environment.sh`. That parity is not
cosmetic: Grafana migrates a dashboard's `schemaVersion` when it loads one written by a
different major, so a "close enough" tag would draw something the Pi never draws.

`up.sh` regenerates `provisioning/` from the production files on every run, with exactly three
substitutions:

| What | Why |
|---|---|
| `__INFLUXDB_TOKEN__` → `DEV_TOKEN` | the real token stays in the Pi's `.env` |
| `localhost:8086` → `influxdb:8086` | under compose the database is a service name |
| `allowUiUpdates: false` → `true` | locally, poking at a panel is the point |

Everything else is untouched — including the uid `influxdb-pijardin`, which all seven panels
reference and which is what lets the committed dashboard JSON work here unmodified.
`provisioning/` is generated and gitignored: do not edit it, edit `grafana/`.

## The timezone trap

The dashboard is `timezone: browser` ([puit.json](../grafana/dashboards/puit.json)), so it
renders in **your browser's** timezone — here and on the Pi alike. The two therefore agree
automatically.

The bot's charts do not: `/graphe24h` and friends hardcode `Europe/Zurich`
([graph.py](../telegram_bot/graph.py)). If this machine is on another timezone, a discrepancy
between `/graphe7j` and the local "Historique semaine" panel comes from that and nothing else.
The dashboard is editable locally, so its timezone can be forced in its settings.

## Checking the local copy against the Pi

Three checks, each against a bot command:

| Check | Expected |
|---|---|
| "Volume actuel" gauge | ≈ the last `/mesure`, within sensor noise |
| "Eau utilisée / perdue" table | the same 3 rows as the first 3 of `/pertes` |
| "Historique semaine" | the same shape as `/graphe7j` |

`import.sh` already does the minimum one: it recounts `lenght_median` points after writing,
because a run of `204`s does not prove a panel will have anything to draw.

## If something is wrong

- **Empty panels** — the dashboard window (7 days by default) is wider than the export.
  `/export 8d` covers it; otherwise narrow the range at the top right.
- **`import.sh` exits on 422** — a field type conflict. The response body names the field and
  the offending line; that is a bug in `telegram_bot/export.py`, not in the import.
- **Datasource failing in Grafana** — `docker compose logs influxdb`. The healthcheck makes
  Grafana wait, so this usually means the database never finished its initial `setup`.
- **`provisioning is not writable`** — `docker compose up` was run directly instead of
  `./up.sh`, so Docker created the bind-mount source as root. `sudo rm -rf provisioning &&
  ./up.sh`.
