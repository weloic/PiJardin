# PiJardin
Micro domotic project for water management, using a raspberry pi and arduino for sensors.


## Pi Initialisation
Tasks are runned periodically using systemd timer, you need to "start the process" once (bootstrap) (after that, everything should by automatic):

sudo cp /home/pi/PiJardin/systemd/deploy.service \
        /home/pi/PiJardin/systemd/deploy.timer \
        /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now deploy.timer