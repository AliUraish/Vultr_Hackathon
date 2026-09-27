#!/usr/bin/env bash
# Deploy Replay to the two Vultr VMs over SSH (never calls the Vultr API).
#
#   infra/deploy.sh setup    first time: copy code, install, configure, start (A then B)
#   infra/deploy.sh push     copy code and restart services
#   infra/deploy.sh web      copy only the web app (no restart)
#   infra/deploy.sh status   service state + health on both VMs
#   infra/deploy.sh logs a|b follow logs
#   infra/deploy.sh harden   policy signing keys (private key stays on VM A), witness store on VM B
#   infra/deploy.sh reset    wipe all data for a clean demo (asks first)
#
# Needs VM_A_IP and VM_B_IP in .env and this machine's SSH key on both VMs (root).
set -euo pipefail
cd "$(dirname "$0")/.."

env_get() { grep -E "^$1=" .env 2>/dev/null | head -1 | cut -d= -f2- | sed 's/[[:space:]]*#.*$//; s/[[:space:]]*$//'; }
VM_A_IP=$(env_get VM_A_IP); VM_B_IP=$(env_get VM_B_IP)
: "${VM_A_IP:?set VM_A_IP in .env}" "${VM_B_IP:?set VM_B_IP in .env}"

SECRETS=infra/.secrets.env
if [ ! -f "$SECRETS" ]; then
  ( umask 077
    printf 'NODE_TOKEN=%s\nADMIN_PASSWORD=%s\n' "$(openssl rand -hex 24)" "$(openssl rand -base64 18 | tr -dc 'A-Za-z0-9' | head -c 16)" > "$SECRETS" )
  echo "generated $SECRETS (node token + operator password; gitignored)"
fi
NODE_TOKEN=$(grep ^NODE_TOKEN= "$SECRETS" | cut -d= -f2-)
ADMIN_PASSWORD=$(grep ^ADMIN_PASSWORD= "$SECRETS" | cut -d= -f2-)

SSH=(ssh -o StrictHostKeyChecking=accept-new -o ConnectTimeout=10)

push_code() {
  rsync -az --delete -e "${SSH[*]}" \
    --exclude .venv --exclude .git --exclude .env --exclude 'infra/.secrets.env' \
    --exclude __pycache__ --exclude .pytest_cache --exclude '*.pyc' \
    ./ "root@$1:/opt/replay/"
}

send_env() {  # secrets go over the SSH channel into a root-only file, not the command line
  "${SSH[@]}" "root@$1" 'umask 077; cat > /root/replay-bootstrap.env'
}

case "${1:-}" in
  setup)
    echo "== VM A ($VM_A_IP): control plane + Postgres"
    push_code "$VM_A_IP"
    printf 'NODE_TOKEN=%s\nADMIN_PASSWORD=%s\nVM_B_IP=%s\nINFERENCE_PROVIDER=%s\nINFERENCE_KEY=%s\nINFERENCE_MODEL=%s\n' \
      "$NODE_TOKEN" "$ADMIN_PASSWORD" "$VM_B_IP" "$(env_get INFERENCE_PROVIDER)" \
      "$(env_get INFERENCE_KEY)" "$(env_get INFERENCE_MODEL)" | send_env "$VM_A_IP"
    "${SSH[@]}" "root@$VM_A_IP" 'bash /opt/replay/infra/bootstrap-a.sh'
    echo "== VM B ($VM_B_IP): sim + replay workers"
    push_code "$VM_B_IP"
    printf 'NODE_TOKEN=%s\nVM_A_IP=%s\n' "$NODE_TOKEN" "$VM_A_IP" | send_env "$VM_B_IP"
    "${SSH[@]}" "root@$VM_B_IP" 'bash /opt/replay/infra/bootstrap-b.sh'
    "$0" harden
    "${SSH[@]}" "root@$VM_A_IP" 'systemctl restart replay-control'
    "${SSH[@]}" "root@$VM_B_IP" 'systemctl restart replay-sim "replay-worker@*"'
    echo
    echo "Web app: http://$VM_A_IP:8000   user: ops   password: in $SECRETS"
    ;;
  harden)  # Ed25519 policy signing: the private key is generated on VM A and never leaves it
    "${SSH[@]}" "root@$VM_A_IP" 'set -e
      /opt/replay/.venv/bin/pip install -q "cryptography>=42"
      [ -f /etc/replay/policy_signing.pem ] || (umask 077; openssl genpkey -algorithm ed25519 -out /etc/replay/policy_signing.pem)
      chown root:replay /etc/replay/policy_signing.pem && chmod 640 /etc/replay/policy_signing.pem
      grep -q ^POLICY_SIGNING_KEY_FILE= /etc/replay/control.env ||
        echo POLICY_SIGNING_KEY_FILE=/etc/replay/policy_signing.pem >> /etc/replay/control.env'
    "${SSH[@]}" "root@$VM_A_IP" 'openssl pkey -in /etc/replay/policy_signing.pem -pubout' |
      "${SSH[@]}" "root@$VM_B_IP" 'set -e
        /opt/replay/.venv/bin/pip install -q "cryptography>=42"
        cat > /etc/replay/policy_public.pem && chown root:replay /etc/replay/policy_public.pem && chmod 644 /etc/replay/policy_public.pem
        install -d -m 750 -o replay -g replay /var/lib/replay
        grep -q ^POLICY_PUBLIC_KEY_FILE= /etc/replay/sim.env ||
          echo POLICY_PUBLIC_KEY_FILE=/etc/replay/policy_public.pem >> /etc/replay/sim.env
        grep -q ^WITNESS_FILE= /etc/replay/sim.env || echo WITNESS_FILE=/var/lib/replay/witness.jsonl >> /etc/replay/sim.env'
    echo "policy signing key on VM A, public key + witness store on VM B (restart services to apply)"
    ;;
  push)
    push_code "$VM_A_IP"
    "${SSH[@]}" "root@$VM_A_IP" 'chown -R replay:replay /opt/replay &&
      install -m 644 /opt/replay/infra/systemd/replay-control.service /etc/systemd/system/ &&
      systemctl daemon-reload && systemctl restart replay-control'
    push_code "$VM_B_IP"
    "${SSH[@]}" "root@$VM_B_IP" 'chown -R replay:replay /opt/replay &&
      install -m 644 /opt/replay/infra/systemd/replay-sim.service "/opt/replay/infra/systemd/replay-worker@.service" /etc/systemd/system/ &&
      systemctl daemon-reload && systemctl restart replay-sim "replay-worker@*"'
    echo "pushed and restarted"
    ;;
  web)  # only the web app: served from disk, so no restart
    rsync -az --delete -e "${SSH[*]}" ./web/ "root@$VM_A_IP:/opt/replay/web/"
    "${SSH[@]}" "root@$VM_A_IP" 'chown -R replay:replay /opt/replay/web'
    echo "web app updated"
    ;;
  reset)  # clean slate for a demo: wipes the database (events, capsules, policies) and restarts the fleet
    if [ "${CONFIRM:-}" != "RESET" ]; then
      read -r -p "Wipe ALL Replay data on $VM_A_IP (type RESET): " answer
      [ "$answer" = "RESET" ] || { echo "aborted"; exit 1; }
    fi
    "${SSH[@]}" "root@$VM_A_IP" 'systemctl stop replay-control &&
      sudo -u postgres dropdb --if-exists replay && sudo -u postgres createdb -O replay replay &&
      systemctl start replay-control'
    "${SSH[@]}" "root@$VM_B_IP" 'rm -f /var/lib/replay/witness.jsonl; systemctl restart replay-sim "replay-worker@*"'
    echo "reset: empty database, new site profile, policy v1, empty ledger, new sim run"
    ;;
  status)
    "${SSH[@]}" "root@$VM_A_IP" 'systemctl is-active replay-control postgresql; curl -s localhost:8000/healthz; echo'
    "${SSH[@]}" "root@$VM_B_IP" 'systemctl is-active replay-sim; systemctl list-units --no-legend "replay-worker@*" | wc -l | xargs echo workers:; curl -s localhost:8100/health; echo'
    ;;
  logs)
    case "${2:-a}" in
      a) "${SSH[@]}" "root@$VM_A_IP" 'journalctl -f -u replay-control' ;;
      b) "${SSH[@]}" "root@$VM_B_IP" 'journalctl -f -u replay-sim -u "replay-worker@*"' ;;
    esac
    ;;
  *) sed -n '2,10p' "$0"; exit 1 ;;
esac
