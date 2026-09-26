#!/usr/bin/env bash
# VM B (sim + replay workers): Python env, config, systemd, firewall.
# Idempotent; run as root by infra/deploy.sh. Reads NODE_TOKEN, VM_A_IP from /root/replay-bootstrap.env.
set -euo pipefail
source /root/replay-bootstrap.env
: "${NODE_TOKEN:?}" "${VM_A_IP:?}"
APP=/opt/replay
WORKERS=${WORKERS:-$(nproc)}
export DEBIAN_FRONTEND=noninteractive

cloud-init status --wait >/dev/null 2>&1 || true   # first boot runs its own apt jobs
APT=(apt-get -q -o DPkg::Lock::Timeout=600)
"${APT[@]}" update

# Security updates stay on, but must not restart our services mid-demo (they restart on the next deploy).
install -d /etc/needrestart/conf.d
echo "\$nrconf{restart} = 'l';" > /etc/needrestart/conf.d/replay.conf
"${APT[@]}" install -y python3-venv python3-pip ufw rsync
id replay >/dev/null 2>&1 || useradd --system --home-dir "$APP" --shell /usr/sbin/nologin replay
install -d -m 750 -o root -g replay /etc/replay

python3 -m venv "$APP/.venv"
"$APP/.venv/bin/pip" install -q --upgrade pip
"$APP/.venv/bin/pip" install -q "fastapi>=0.115" "uvicorn[standard]>=0.30" "httpx>=0.27"

umask 027
cat > /etc/replay/sim.env <<CONF
CONTROL_URL=http://${VM_A_IP}:8000
NODE_TOKEN=${NODE_TOKEN}
SIM_SEED=${SIM_SEED:-}
CONF
chown root:replay /etc/replay/sim.env
chmod 640 /etc/replay/sim.env
chown -R replay:replay "$APP"

install -m 644 "$APP/infra/systemd/replay-sim.service" "$APP/infra/systemd/replay-worker@.service" /etc/systemd/system/
systemctl daemon-reload
systemctl enable -q replay-sim
systemctl restart replay-sim
for i in $(seq 1 "$WORKERS"); do
  systemctl enable -q "replay-worker@$i"
  systemctl restart "replay-worker@$i"
done

ufw allow OpenSSH >/dev/null
ufw allow from "$VM_A_IP" to any port 8100 proto tcp >/dev/null   # fleet API: only the control plane
ufw --force enable >/dev/null
rm -f /root/replay-bootstrap.env
echo "VM B ready: sim on :8100, ${WORKERS} replay workers"
