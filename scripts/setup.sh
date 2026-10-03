#!/usr/bin/env bash
# btc15 setup for Ubuntu 22.04 / 24.04. Run as root from the repository root:  sudo bash scripts/setup.sh
# Installs Python 3.11+, a venv, the app, a locked-down systemd unit (Restart=always) and an .env template.
# The service starts in PAPER mode. It never enables DEMO/LIVE on its own.
set -euo pipefail

[[ $EUID -eq 0 ]] || { echo "run as root (sudo)"; exit 1; }
SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
APP=/opt/btc15; DATA=/var/lib/btc15; CONF=/etc/btc15; ENVF=$CONF/btc15.env

source /etc/os-release
[[ "$ID" == "ubuntu" && ( "$VERSION_ID" == "22.04" || "$VERSION_ID" == "24.04" ) ]] \
  || echo "WARNING: tested on Ubuntu 22.04/24.04 only (found $PRETTY_NAME)"

export DEBIAN_FRONTEND=noninteractive
apt-get update -y
apt-get install -y --no-install-recommends ca-certificates curl rsync software-properties-common
PY=""
for c in python3.12 python3.11; do command -v "$c" >/dev/null && { PY=$c; break; }; done
if [[ -z "$PY" ]]; then                       # 22.04 ships 3.10: take 3.11 from deadsnakes
  add-apt-repository -y ppa:deadsnakes/ppa
  apt-get update -y
  apt-get install -y python3.11 python3.11-venv
  PY=python3.11
fi
apt-get install -y "${PY}-venv" 2>/dev/null || apt-get install -y python3-venv

id btc15 &>/dev/null || useradd --system --home "$DATA" --shell /usr/sbin/nologin btc15
install -d -o btc15 -g btc15 -m 750 "$DATA"
install -d -o root -g btc15 -m 750 "$CONF"
rsync -a --delete --exclude .git --exclude .venv --exclude '*.db' --exclude .env --exclude '__pycache__' "$SRC"/ "$APP"/
"$PY" -m venv "$APP/.venv"
"$APP/.venv/bin/pip" install --quiet --upgrade pip
"$APP/.venv/bin/pip" install --quiet "$APP"

if [[ ! -f "$ENVF" ]]; then
  PW="$(head -c 24 /dev/urandom | base64 | tr -d '/+=' | head -c 24)"
  { grep -v '^DASHBOARD_PASSWORD=' "$SRC/.env.example" | grep -v -E '^(DB_PATH|KILL_FILE|KALSHI_PRIVATE_KEY_PATH)='
    echo "DASHBOARD_PASSWORD=$PW"
    echo "KALSHI_PRIVATE_KEY_PATH=$CONF/kalshi_private_key.pem"; } > "$ENVF"
  chown root:btc15 "$ENVF"; chmod 640 "$ENVF"
  echo "Created $ENVF with a random dashboard password (read it with: sudo grep DASHBOARD_PASSWORD $ENVF)"
else
  echo "$ENVF already exists - left untouched"
fi

install -m 644 "$SRC/deploy/btc15.service" /etc/systemd/system/btc15.service
cat > /usr/local/bin/btc15ctl <<'W'
#!/usr/bin/env bash
# Operator CLI with the service's environment:  btc15ctl report | kill | reset-halt | backfill-results
set -a; . /etc/btc15/btc15.env; set +a
export DB_PATH=/var/lib/btc15/btc15.db KILL_FILE=/var/lib/btc15/KILL
exec sudo -E -u btc15 /opt/btc15/.venv/bin/btc15 "$@"
W
chmod 755 /usr/local/bin/btc15ctl

systemctl daemon-reload
systemctl enable --now btc15.service

if command -v ufw >/dev/null && ufw status | grep -q "Status: active"; then
  ufw allow 8080/tcp comment "btc15 dashboard (basic auth)"
fi
cat <<MSG

btc15 is running in PAPER mode.  Dashboard: http://<server-ip>:8080  (user: admin; password in $ENVF)
NOTE: HTTP basic auth over plain HTTP sends the password in clear text. Put the dashboard behind TLS
(Caddy/nginx), a VPN or an SSH tunnel before exposing it to the internet.
Logs:  journalctl -u btc15 -f        Operator CLI:  btc15ctl report | kill | reset-halt
MSG
