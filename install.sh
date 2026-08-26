#!/usr/bin/env bash
set -Eeuo pipefail

INSTALL_DIR=/opt/miaobot
REPOSITORY_URL=https://github.com/kkx999/Cats.git

fail() {
  echo
  echo "❌ $1" >&2
  trap - ERR
  exit 1
}

trap 'fail "安装在第 ${LINENO} 行中断，请保留上方错误信息。"' ERR

normalize_domain() {
  local value=$1
  value=${value#http://}
  value=${value#https://}
  value=${value%%/*}
  value=${value%.}
  value=${value,,}
  if [[ ! ${value} =~ ^([a-z0-9]([a-z0-9-]*[a-z0-9])?\.)+[a-z]{2,63}$ ]]; then
    return 1
  fi
  printf '%s' "${value}"
}

normalize_channel() {
  local value=$1
  local path

  if [[ ${value} =~ ^-100[0-9]+$ ]]; then
    printf '%s' "${value}"
    return 0
  fi

  value=${value#@}
  if [[ ${value} == tg://resolve\?domain=* ]]; then
    value=${value#tg://resolve\?domain=}
    value=${value%%\&*}
  fi

  value=${value#http://}
  value=${value#https://}
  value=${value#www.}
  case "${value}" in
    t.me/*|telegram.me/*|telegram.dog/*)
      path=${value#*/}
      path=${path#/}
      if [[ ${path} == +* || ${path} == joinchat/* || ${path} == c/* ]]; then
        return 2
      fi
      path=${path#s/}
      value=${path%%/*}
      ;;
  esac

  value=${value%%\?*}
  value=${value%%\#*}
  value=${value%/}
  value=${value#@}
  if [[ ${value} == +* || ${value} == joinchat* || ${value} == c/* ]]; then
    return 2
  fi
  if [[ ! ${value} =~ ^[A-Za-z0-9_]{5,32}$ ]]; then
    return 1
  fi
  printf '@%s' "${value}"
}

if [[ ${1:-} == "--normalize-domain" ]]; then
  trap - ERR
  set +e
  normalize_domain "${2:-}"
  exit $?
fi
if [[ ${1:-} == "--normalize-channel" ]]; then
  trap - ERR
  set +e
  normalize_channel "${2:-}"
  exit $?
fi

if [[ ${EUID} -ne 0 ]]; then
  fail "请使用 root 用户运行安装命令。"
fi

source /etc/os-release
if [[ ${ID:-} != "debian" ]]; then
  fail "当前一键安装仅支持 Debian，检测到 ${ID:-unknown}。"
fi

echo ""
echo "╭────────────────────────────────────────╮"
echo "│          喵Bot · 一键安装              │"
echo "╰────────────────────────────────────────╯"
echo ""
echo "开始前请确认："
echo "  1. 域名已添加 A 记录并指向本机 IP（可开启 Cloudflare 小云朵）"
echo "  2. 已将机器人加入公开素材频道，并设为可发布消息的管理员"
echo "  3. 准备好 Telegram 管理员数字 ID 和 Bot Token"
echo ""
echo "安装只会询问 4 项信息，不会修改 SSH、root 密码或登录方式。"
echo "Bot Token 只保存在服务器 /opt/miaobot/.env，不会上传 GitHub。"
echo ""

read -r -p "① 超级管理员 Telegram 数字 ID: " MIAO_ADMIN_IDS
read -r -s -p "② Bot Token（输入时隐藏）: " MIAO_BOT_TOKEN
echo
read -r -p "③ 后台域名（域名或 https://域名 均可）: " MIAO_DOMAIN_INPUT
read -r -p "④ 公开素材频道（链接、@用户名或 -100频道ID）: " MIAO_CHANNEL_INPUT

MIAO_ADMIN_IDS=${MIAO_ADMIN_IDS//[[:space:]]/}
MIAO_BOT_TOKEN=${MIAO_BOT_TOKEN//[[:space:]]/}
MIAO_DOMAIN_INPUT=${MIAO_DOMAIN_INPUT//[[:space:]]/}
MIAO_CHANNEL_INPUT=${MIAO_CHANNEL_INPUT//[[:space:]]/}

if [[ ! ${MIAO_ADMIN_IDS} =~ ^[0-9]+(,[0-9]+)*$ ]]; then
  fail "管理员 ID 格式不正确；多个管理员请使用英文逗号分隔。"
fi
if [[ ! ${MIAO_BOT_TOKEN} =~ ^[0-9]+:[A-Za-z0-9_-]+$ ]]; then
  fail "Bot Token 格式不正确。请从 @BotFather 完整复制。"
fi

MIAO_DOMAIN=$(normalize_domain "${MIAO_DOMAIN_INPUT}") || fail "域名格式不正确。"
set +e
MIAO_CHANNEL_REF=$(normalize_channel "${MIAO_CHANNEL_INPUT}")
CHANNEL_PARSE_STATUS=$?
set -e
if [[ ${CHANNEL_PARSE_STATUS} -eq 2 ]]; then
  fail "检测到私密邀请链接。素材频道必须是设置了公开用户名的公开频道。"
elif [[ ${CHANNEL_PARSE_STATUS} -ne 0 ]]; then
  fail "无法识别频道写法。支持 @用户名、t.me 链接、频道消息链接、tg://链接或 -100频道ID。"
fi

echo
echo "正在准备安装环境…"
apt-get update
apt-get install -y ca-certificates curl git jq openssl

telegram_api() {
  local method=$1
  shift
  curl -fsS --retry 2 --connect-timeout 10 --max-time 30 \
    --get "https://api.telegram.org/bot${MIAO_BOT_TOKEN}/${method}" "$@"
}

echo "正在验证机器人和素材频道…"
BOT_INFO=$(telegram_api getMe) || fail "无法连接 Telegram 或 Bot Token 无效。"
if [[ $(jq -r '.ok // false' <<<"${BOT_INFO}") != "true" ]]; then
  fail "Bot Token 验证失败：$(jq -r '.description // "未知错误"' <<<"${BOT_INFO}")"
fi
MIAO_BOT_ID=$(jq -r '.result.id' <<<"${BOT_INFO}")
MIAO_BOT_USERNAME=$(jq -r '.result.username // empty' <<<"${BOT_INFO}")
[[ -n ${MIAO_BOT_USERNAME} ]] || fail "Telegram 没有返回机器人用户名。"

CHAT_INFO=$(telegram_api getChat --data-urlencode "chat_id=${MIAO_CHANNEL_REF}") \
  || fail "无法读取公开频道，请检查频道链接。"
if [[ $(jq -r '.ok // false' <<<"${CHAT_INFO}") != "true" ]]; then
  fail "频道验证失败：$(jq -r '.description // "未知错误"' <<<"${CHAT_INFO}")"
fi
MIAO_CHANNEL_ID=$(jq -r '.result.id' <<<"${CHAT_INFO}")
MIAO_CHANNEL_USERNAME=$(jq -r '.result.username // empty' <<<"${CHAT_INFO}")
MIAO_CHANNEL_TYPE=$(jq -r '.result.type // empty' <<<"${CHAT_INFO}")
if [[ ${MIAO_CHANNEL_TYPE} != "channel" || -z ${MIAO_CHANNEL_USERNAME} ]]; then
  fail "指定目标不是公开频道，或频道尚未设置公开用户名。"
fi

MEMBER_INFO=$(telegram_api getChatMember \
  --data-urlencode "chat_id=${MIAO_CHANNEL_ID}" \
  --data-urlencode "user_id=${MIAO_BOT_ID}") || fail "无法检查机器人频道权限。"
MIAO_MEMBER_STATUS=$(jq -r '.result.status // empty' <<<"${MEMBER_INFO}")
MIAO_CAN_POST=$(jq -r '.result.can_post_messages // false' <<<"${MEMBER_INFO}")
if [[ ${MIAO_MEMBER_STATUS} != "administrator" && ${MIAO_MEMBER_STATUS} != "creator" ]]; then
  fail "机器人尚未成为素材频道管理员，请添加管理员后重新运行。"
fi
if [[ ${MIAO_MEMBER_STATUS} != "creator" && ${MIAO_CAN_POST} != "true" ]]; then
  fail "机器人没有频道“发布消息”权限，请开启后重新运行。"
fi

echo "✅ 机器人：@${MIAO_BOT_USERNAME}"
echo "✅ 素材频道：@${MIAO_CHANNEL_USERNAME}（${MIAO_CHANNEL_ID}）"
echo "✅ 后台地址：https://${MIAO_DOMAIN}"

if ! getent ahosts "${MIAO_DOMAIN}" >/dev/null 2>&1; then
  echo "⚠️  当前还查询不到域名解析；程序会继续安装，解析生效后 HTTPS 会自动启用。"
fi

if command -v docker >/dev/null 2>&1 && ! docker compose version >/dev/null 2>&1; then
  fail "检测到已有 Docker 但缺少 Compose Plugin。为避免影响现有容器，请先补装插件。"
fi

if ! docker compose version >/dev/null 2>&1; then
  echo "正在安装 Docker Engine 与 Compose Plugin…"
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

if [[ -e ${INSTALL_DIR}/.env ]]; then
  fail "${INSTALL_DIR}/.env 已存在。为避免覆盖现有密钥，本次安装已停止。"
fi
if [[ -d ${INSTALL_DIR}/.git ]]; then
  git -C "${INSTALL_DIR}" pull --ff-only
elif [[ -e ${INSTALL_DIR} ]]; then
  fail "${INSTALL_DIR} 已存在但不是喵Bot仓库，请先确认该目录用途。"
else
  git clone "${REPOSITORY_URL}" "${INSTALL_DIR}"
fi

MIAO_POSTGRES_PASSWORD=$(openssl rand -hex 24)
MIAO_APP_SECRET=$(openssl rand -hex 32)
MIAO_LOGIN_PEPPER=$(openssl rand -hex 32)

umask 077
cat > "${INSTALL_DIR}/.env" <<EOF
BOT_TOKEN=${MIAO_BOT_TOKEN}
BOT_USERNAME=${MIAO_BOT_USERNAME}
SUPERADMIN_IDS=${MIAO_ADMIN_IDS}
PUBLIC_BASE_URL=https://${MIAO_DOMAIN}
FEEDBACK_USERNAME=
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
chmod 600 "${INSTALL_DIR}/.env"

echo "正在构建并启动喵Bot…"
cd "${INSTALL_DIR}"
docker compose pull
docker compose up -d --build

echo
docker compose ps
echo
echo "✅ 喵Bot安装完成：https://${MIAO_DOMAIN}"
echo "问题反馈账号可登录超级后台后配置。"
echo "查看运行日志：cd ${INSTALL_DIR} && docker compose logs -f --tail=200"
