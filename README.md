# PiJardin
Micro domotic project for water management, using a raspberry pi and arduino for sensors.


## Pi Initialisation
Run once on a fresh Pi to register all systemd units and start the automation:

```bash
bash /home/pi/PiJardin/deploy/bootstrap.sh
```

After that, `deploy.timer` pulls and redeploys the repo automatically every 15 minutes, and `sensors.timer` reads sensor data every 5 minutes on the clock.