#!/bin/bash
set -e
VERSION="2026.9.3"

if [ "$EUID" -eq 0 ]; then
  echo "WARNING: Installing as root. For user-level install, run without sudo."
  read -p "Continue with system-wide install? [y/N] " confirm
  [[ "$confirm" == [yY]* ]] || exit 1

  LIB_DIR="/usr/local/lib/microsre"
  BIN_DIR="/usr/local/bin"
  SYSTEMD_DIR="/etc/systemd/system"
  USER=""
  SYSTEMD_CFG="User=root
Group=root
[Install]
WantedBy=multi-user.target
"
else
  LIB_DIR="${HOME}/.local/lib/microsre"
  BIN_DIR="${HOME}/.local/bin"
  SYSTEMD_DIR="${HOME}/.config/systemd/user"
  USER="--user"
  SYSTEMD_CFG="[Install]
WantedBy=default.target
"
fi

mkdir -p "${BIN_DIR}" "${LIB_DIR}"

curl -fsSL "https://github.com/aivarsk/microsre/releases/download/${VERSION}/microsre-${VERSION}.pyz" -o "${LIB_DIR}/microsre.pyz"
chmod +x "${LIB_DIR}/microsre.pyz"

cat >"${BIN_DIR}/microsre" <<EOF
#!/bin/bash
ulimit -s 1024
exec python3 "${LIB_DIR}/microsre.pyz" "\$@"
EOF
chmod +x "${BIN_DIR}/microsre"

cat >"${SYSTEMD_DIR}/microsre.service" <<EOF
[Unit]
Description=60-second Linux health checks.
After=network.target

[Service]
Type=simple
ExecStart=${BIN_DIR}/microsre --daemon
Restart=always
RestartSec=10
LimitSTACK=1M
${SYSTEMD_CFG}
EOF

systemctl ${USER} daemon-reload
systemctl ${USER} enable microsre.service
systemctl ${USER} start microsre
systemctl ${USER} status microsre
