#!/usr/bin/env bash
set -Eeuo pipefail

trap 'echo "安装在第 ${LINENO} 行失败，请保留上方错误信息。" >&2' ERR

if [[ ${EUID} -ne 0 ]]; then
  echo "请使用 root 用户运行此安装脚本。" >&2
  exit 1
fi

if [[ ! -f docker-compose.yml || ! -f .env.example ]]; then
  echo "请进入喵Bot仓库根目录后运行：bash deploy/install.sh" >&2
  exit 1
fi

if [[ -f .env ]]; then
  echo "检测到现有 .env。为避免覆盖密钥，安装已停止。" >&2
  echo "如需更新，请使用：docker compose up -d --build" >&2
  exit 1
fi

source /etc/os-release
if [[ ${ID:-} != "debian" ]]; then
  echo "当前脚本仅支持 Debian，检测到：${ID:-unknown}" >&2
  exit 1
fi

read_required() {
  local prompt=$1
  local variable=$2
  local value=""
  while [[ -z ${value} ]]; do
    read -r -p "${prompt}: " value
  done
  printf -v "${variable}" '%s' "${value}"
}

echo "喵Bot · Debian 12 安装"
echo "密钥只会写入服务器的 .env，不会上传到 GitHub。"

read_required "后台域名（例如 bot.example.com）" MIAO_DOMAIN
read_required "机器人用户名（不带 @）" MIAO_BOT_USERNAME
read -r -s -p "Bot Token（输入时不会显示）: " MIAO_BOT_TOKEN
echo
read_required "超级管理员 Telegram 数字 ID" MIAO_ADMIN_IDS
read_required "公开素材频道 ID（以 -100 开头）" MIAO_CHANNEL_ID
read_required "公开素材频道用户名（不带 @）" MIAO_CHANNEL_USERNAME
read -r -p "问题反馈用户名（不带 @，可留空）: " MIAO_FEEDBACK_USERNAME

if [[ ! ${MIAO_DOMAIN} =~ ^[A-Za-z0-9.-]+$ ]]; then
  echo "域名格式不正确。" >&2
  exit 1
fi
if [[ ! ${MIAO_BOT_USERNAME} =~ ^[A-Za-z0-9_]{5,32}$ ]]; then
  echo "机器人用户名格式不正确。" >&2
  exit 1
fi
if [[ ! ${MIAO_BOT_TOKEN} =~ ^[0-9]+:[A-Za-z0-9_-]+$ ]]; then
  echo "Bot Token 格式不正确。" >&2
  exit 1
fi
if [[ ! ${MIAO_ADMIN_IDS} =~ ^[0-9]+(,[0-9]+)*$ ]]; then
  echo "超级管理员 ID 格式不正确；多个 ID 使用英文逗号分隔。" >&2
  exit 1
fi
if [[ ! ${MIAO_CHANNEL_ID} =~ ^-100[0-9]+$ ]]; then
  echo "公开素材频道 ID 应以 -100 开头。" >&2
  exit 1
fi
if [[ ! ${MIAO_CHANNEL_USERNAME} =~ ^[A-Za-z0-9_]{5,32}$ ]]; then
  echo "素材频道用户名格式不正确。" >&2
  exit 1
fi
if [[ -n ${MIAO_FEEDBACK_USERNAME} && ! ${MIAO_FEEDBACK_USERNAME} =~ ^[A-Za-z0-9_]{5,32}$ ]]; then
  echo "问题反馈用户名格式不正确。" >&2
  exit 1
fi

if command -v docker >/dev/null 2>&1 && ! docker compose version >/dev/null 2>&1; then
  echo "检测到已有 Docker，但缺少 Compose Plugin。为避免破坏现有容器，安装已停止。" >&2
  echo "请先按照 Docker 官方文档补装 docker-compose-plugin，再重新运行。" >&2
  exit 1
fi

if ! docker compose version >/dev/null 2>&1; then
  echo "正在从 Docker 官方软件源安装 Docker Engine 与 Compose Plugin…"
  apt-get update
  apt-get install -y ca-certificates curl openssl
  install -m 0755 -d /etc/apt/keyrings
  curl -fsSL https://download.docker.com/linux/debian/gpg \
    -o /etc/apt/keyrings/docker.asc
  chmod a+r /etc/apt/keyrings/docker.asc
  cat > /etc/apt/sources.list.d/docker.sources <<EOF
Types: deb
URIs: https://download.docker.com/linux/debian
Suites: ${VERSION_CODENAME}
Components: stable
Architectures: $(dpkg --print-architecture)
Signed-By: /etc/apt/keyrings/docker.asc
EOF
  apt-get update
  apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
fi

systemctl enable --now docker

MIAO_POSTGRES_PASSWORD=$(openssl rand -hex 24)
MIAO_APP_SECRET=$(openssl rand -hex 32)
MIAO_LOGIN_PEPPER=$(openssl rand -hex 32)

umask 077
cat > .env <<EOF
BOT_TOKEN=${MIAO_BOT_TOKEN}
BOT_USERNAME=${MIAO_BOT_USERNAME}
SUPERADMIN_IDS=${MIAO_ADMIN_IDS}
PUBLIC_BASE_URL=https://${MIAO_DOMAIN}
FEEDBACK_USERNAME=${MIAO_FEEDBACK_USERNAME}
MATERIAL_CHANNEL_ID=${MIAO_CHANNEL_ID}
MATERIAL_CHANNEL_USERNAME=${MIAO_CHANNEL_USERNAME}
DOMAIN=${MIAO_DOMAIN}
POSTGRES_DB=cats
POSTGRES_USER=cats
POSTGRES_PASSWORD=${MIAO_POSTGRES_PASSWORD}
DATABASE_URL=postgresql+asyncpg://cats:${MIAO_POSTGRES_PASSWORD}@postgres:5432/cats
APP_SECRET=${MIAO_APP_SECRET}
LOGIN_CODE_PEPPER=${MIAO_LOGIN_PEPPER}
DEFAULT_TASK_LIMIT=10
LOGIN_TTL_SECONDS=300
DEFAULT_TIMEZONE=Asia/Shanghai
SCHEDULER_CONCURRENCY=20
EOF
chmod 600 .env

echo "正在构建并启动喵Bot…"
docker compose pull
docker compose up -d --build

echo
docker compose ps
echo
echo "喵Bot已启动：https://${MIAO_DOMAIN}"
echo "如果域名刚解析，请等待 DNS 和 HTTPS 证书生效后再访问。"
echo "查看日志：docker compose logs -f --tail=200 web bot caddy"
