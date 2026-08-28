#!/bin/bash
#
# Install the systemd integration: run ./setup.sh && ./start.sh at boot and
# ./stop.sh at shutdown.
#
# One-time root step, deliberately kept out of setup.sh so the boot path never
# has to install anything. Idempotent — re-run after ./config.sh.
#
set -eu

here="$(cd "$(dirname "$0")" && pwd)"
cd "$here"

unit=systemd/bmrb-extract.service
sudoers=systemd/bmrb-extract.sudoers
sysctl_conf=systemd/99-bmrb-extract.sysctl.conf

for f in $unit $sudoers ; do
  if [[ ! -e $f ]] ; then
    echo "Error: Missing $f. Run ./config.sh at first."
    exit 1
  fi
done

if [[ ! -e $sysctl_conf ]] ; then
  echo "Error: Missing $sysctl_conf."
  exit 1
fi

#
# Validate the sudoers fragment BEFORE installing it — a malformed file in
# /etc/sudoers.d locks out sudo entirely.
#
echo "Validating $sudoers ..."
visudo -cf $sudoers

#
# Passwordless sudo for the commands setup.sh invokes (no TTY under systemd)
#
sudo install -m 0440 -o root -g root $sudoers /etc/sudoers.d/bmrb-extract

#
# Persist the kernel tunables so setup.sh's `sudo sysctl -w` guards are no-ops
#
sudo install -m 0644 -o root -g root $sysctl_conf /etc/sysctl.d/99-bmrb-extract.conf
sudo sysctl --system > /dev/null

#
# The unit itself. Not `enable --now`: the stack may already be running, and
# `systemctl start` would launch a second, concurrent setup.sh.
#
sudo install -m 0644 -o root -g root $unit /etc/systemd/system/bmrb-extract.service
sudo systemctl daemon-reload
sudo systemctl enable bmrb-extract.service

cat <<'MSG'

Installed:
  /etc/systemd/system/bmrb-extract.service   (enabled — starts at boot)
  /etc/sudoers.d/bmrb-extract                (0440 root:root)
  /etc/sysctl.d/99-bmrb-extract.conf

From now on, prefer these over ./start.sh and ./stop.sh:
  sudo systemctl start bmrb-extract     # runs ./setup.sh && ./start.sh
  sudo systemctl stop bmrb-extract      # runs ./stop.sh
  systemctl status bmrb-extract
  journalctl -u bmrb-extract -f

The service is NOT started by this script. If the stack is currently down:
  sudo systemctl start bmrb-extract
If it is up and was started by hand, adopt it on the next reboot, or:
  ./stop.sh && sudo systemctl start bmrb-extract

Note: `systemctl restart docker` stops this unit without restarting it
(Requires=docker.service). Follow it with `sudo systemctl restart bmrb-extract`.
MSG
