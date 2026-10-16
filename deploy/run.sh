#!/usr/bin/env bash
# SerialLens Web 智能体一键部署（Ubuntu 裸机 / 阿里云腾讯云 ECS 通用）
# 用法：git clone 仓库后，在仓库根目录执行 bash deploy/run.sh
set -euo pipefail

cd "$(dirname "$0")/.."
ROOT=$(pwd)

echo "[1/4] Python 虚拟环境与依赖"
python3 -m venv .venv
.venv/bin/pip install -q -U pip
.venv/bin/pip install -q . fastapi "uvicorn[standard]"

echo "[2/4] 访问密码（写入 .env，不进 git）"
if [ ! -f .env ]; then
  read -r -p "设置访问密码（直接回车用默认 sgry..）: " PW
  PW=${PW:-sgry..}
  echo "SERIALLENS_ACCESS_PASSWORD=$PW" > .env
  echo "已写入 $ROOT/.env"
fi

echo "[3/4] systemd 常驻服务（重启自动拉起）"
sudo tee /etc/systemd/system/seriallens.service >/dev/null <<EOF
[Unit]
Description=SerialLens Web Agent
After=network.target

[Service]
WorkingDirectory=$ROOT
EnvironmentFile=$ROOT/.env
Environment=PYTHONUTF8=1
ExecStart=$ROOT/.venv/bin/python -m uvicorn webapp.app:app --host 0.0.0.0 --port 8300
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF
sudo systemctl daemon-reload
sudo systemctl enable --now seriallens

echo "[4/4] 完成"
sleep 2
systemctl is-active seriallens && echo "服务运行中"
IP=$(curl -s --max-time 5 ifconfig.me || echo "<服务器公网IP>")
echo "访问地址: http://$IP:8300  （记得在云控制台安全组放行 8300 端口）"
