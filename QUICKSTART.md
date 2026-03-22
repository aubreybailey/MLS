how to run
systemctl --user start podman.socket
cd ~/code/MLS
#docker compose rm -f   # only needed if it fails to start
docker compose up -d