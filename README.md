# PiJardin
Micro domotic project for water management, using a raspberry pi and arduino for sensors.


## Pi Initialisation
Run once on a fresh Pi to register all systemd units and start the automation:

```bash
bash ~/PiJardin/deploy/bootstrap.sh
```

After that, `deploy.timer` pulls and redeploys the repo automatically every 15 minutes, and `sensors.timer` reads sensor data every 5 minutes on the clock.

## Manual testing
You can force data collection with:
```
systemctl start sensors.service
```

And visualize output using:
```
journalctl -u sensors.service -n 30
```

## Telegram bot deploy
Once .env is set, run following to deploy:
```
sudo systemctl restart telegram-bot.service
```

Users register themselves by messaging the bot `/start <password>`. The password determines their role:
- `TELEGRAM_PASSWORD_ADMIN` in `.env` — registers as `admin`
- `TELEGRAM_PASSWORD_VIEWER` in `.env` — registers as `viewer`

Registered chat IDs and roles are stored in `telegram_bot/.telegram_users.json` (gitignored, not committed).

Alert delivery is independent of role and off by default. Once registered, a user toggles it with `/alertes on` or `/alertes off` (or `/alertes` with no argument to check the current setting).

## Low-volume alerts
After each measurement (every 5 minutes, and on `/mesure`), the volume is checked against thresholds defined in `telegram_bot/alerts.py`:
- **3 m³** ("volume bas") — opted-in users are alerted once when the volume drops below.
- **0.5 m³** ("volume critique") — alerted on crossing, then reminded every day at 18:00 while the level stays below.

A recovery message is sent when the volume rises back above a threshold (with a 0.2 m³ hysteresis margin to avoid flapping). Alert state is kept in `telegram_bot/.alert_state.json` (gitignored).

To test alert delivery on the Pi without touching the sensor:
```
python telegram_bot/alerts.py 2.8   # fake a 2.8 m³ measurement
```