#!/usr/bin/env bash
# VM A (control plane): Postgres 16, Python env, config, systemd, firewall.
# Idempotent; run as root by infra/deploy.sh after the code is in /opt/replay.
# Reads NODE_TOKEN, ADMIN_PASSWORD, VM_B_IP (+ optional INFERENCE_*) from /root/replay-bootstrap.env.
# Re-running it (deploy.sh setup) rewrites control.env, e.g. to add or change the LLM key.
set -euo pipefail
source /root/replay-bootstrap.env
: "${NODE_TOKEN:?}" "${ADMIN_PASSWORD:?}" "${VM_B_IP:?}"
APP=/opt/replay
export DEBIAN_FRONTEND=noninteractive

cloud-init status --wait >/dev/null 2>&1 || true   # first boot runs its own apt jobs
APT=(apt-get -q -o DPkg::Lock::Timeout=600)
"${APT[@]}" update

# Security updates stay on, but must not restart our services mid-demo (they restart on the next deploy).
install -d /etc/needrestart/conf.d
echo "\$nrconf{restart} = 'l';" > /etc/needrestart/conf.d/replay.conf

# SSH: keys only. 10- sorts before cloud-init's 50- file, and sshd keeps the first value it reads.
printf 'PasswordAuthentication no\nKbdInteractiveAuthentication no\nPermitRootLogin prohibit-password\n' \
  > /etc/ssh/sshd_config.d/10-replay-keys-only.conf
sshd -t && systemctl reload ssh
"${APT[@]}" install -y postgresql python3-venv python3-pip ufw rsync openssl
id replay >/dev/null 2>&1 || useradd --system --home-dir "$APP" --shell /usr/sbin/nologin replay
install -d -m 750 -o root -g replay /etc/replay

# Postgres 16 (Ubuntu 24.04 default), local only: the system of record.
[ -f /etc/replay/db.pass ] || { openssl rand -hex 24 > /etc/replay/db.pass; chmod 600 /etc/replay/db.pass; }
DBPASS=$(cat /etc/replay/db.pass)
if ! sudo -u postgres psql -tAc "SELECT 1 FROM pg_roles WHERE rolname = 'replay'" | grep -q 1; then
  sudo -u postgres psql -q -c "CREATE ROLE replay LOGIN PASSWORD '${DBPASS}'"
fi
if ! sudo -u postgres psql -tAc "SELECT 1 FROM pg_database WHERE datname = 'replay'" | grep -q 1; then
  sudo -u postgres createdb -O replay replay
fi

python3 -m venv "$APP/.venv"
"$APP/.venv/bin/pip" install -q --upgrade pip
"$APP/.venv/bin/pip" install -q "fastapi>=0.115" "uvicorn[standard]>=0.30" "asyncpg>=0.29" "httpx>=0.27" "cryptography>=42"

[ -f /etc/replay/session.secret ] || { openssl rand -hex 32 > /etc/replay/session.secret; chmod 600 /etc/replay/session.secret; }
umask 027
cat > /etc/replay/control.env <<CONF
DATABASE_URL=postgresql://replay:${DBPASS}@127.0.0.1:5432/replay
NODE_TOKEN=${NODE_TOKEN}
SIMNODE_URL=http://${VM_B_IP}:8100
SESSION_SECRET=$(cat /etc/replay/session.secret)
ADMIN_USER=${ADMIN_USER:-ops}
ADMIN_PASSWORD=${ADMIN_PASSWORD}
AUTO_JOBS=1
INFERENCE_PROVIDER=vultr
INFERENCE_KEY=${INFERENCE_KEY:-}
INFERENCE_MODEL=${INFERENCE_MODEL:-}
CONF
umask 022
chown root:replay /etc/replay/control.env
chmod 640 /etc/replay/control.env
chown -R replay:replay "$APP"

install -m 644 "$APP/infra/systemd/replay-control.service" /etc/systemd/system/
systemctl daemon-reload
systemctl enable -q replay-control
systemctl restart replay-control

ufw allow OpenSSH >/dev/null
ufw allow from "$VM_B_IP" to any port 8000 proto tcp >/dev/null   # sim node + workers
if [ "${PUBLIC_WEB:-1}" = "1" ]; then ufw allow 8000/tcp >/dev/null; fi  # web app until NetBird fronts it
ufw --force enable >/dev/null
rm -f /root/replay-bootstrap.env
echo "VM A ready: control plane on :8000"
