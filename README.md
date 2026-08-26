# 喵Bot

喵Bot 是一个面向 Telegram 群组的定时发送机器人。第一版聚焦一件事：让群主快速创建稳定、可管理的定时消息，不把服务器配置和复杂概念暴露给普通用户。

## 已实现

- Telegram 六位验证码登录，网页验证后自动跳转
- 随机后缀登录链接，生成后 5 分钟内有效且可重复使用
- `/start` 提供「管理后台」「把我添加到群」「问题反馈」入口
- 群主或管理员通过 `/connect` 绑定群组
- 文字、图片、视频与多行 URL 按钮
- 单次、每天、每周、每月、自定义分钟/小时/天间隔
- 开始时间、结束时间、自动置顶、发送后自动删除
- 任务创建、编辑、暂停、启用、复制、删除、手动试发
- 数据库持久调度、任务抢占锁、失败退避重试、重启恢复
- 图片和视频发布到公开 Telegram 素材频道
- 同时保存 `file_id`、频道消息 ID 和公开消息链接
- 用户额度与超级管理员后台，支持 10/20/50/100/无限制
- 超级管理员可配置问题反馈账号并群发公告
- 中文响应式后台，桌面和手机均可使用

## 架构

- Web/API：FastAPI
- Telegram：aiogram 3（长轮询，不要求 Telegram webhook）
- 数据库：PostgreSQL 16
- Redis：为后续多实例限流和队列扩展预留
- HTTPS：Caddy 自动申请和续期证书
- 部署：Docker Compose

定时任务以 PostgreSQL 为事实来源。Bot Worker 按到期时间领取任务并加抢占锁，发送完成后计算下一次执行时间。即使进程重启，未完成任务仍会继续执行；同一任务不会因为并发扫描而重复领取。

## 部署前准备

1. Debian 12、1 核 2GB、20GB 硬盘的 VPS。
2. 一个解析到服务器 IP 的域名，例如 `bot.example.com`。Cloudflare 可以开启橙色云朵。
3. 从 `@BotFather` 创建的 Bot Token 和机器人用户名。
4. 一个公开 Telegram 素材频道，并给机器人「发布消息」管理员权限。
5. 你的 Telegram 数字 ID，用作首位超级管理员。

不要把 Bot Token、服务器 root 密码或 `.env` 文件提交到 GitHub，也不要发送到聊天中。

## 首次部署

```bash
apt update && apt install -y git docker.io docker-compose-plugin
systemctl enable --now docker
git clone https://github.com/kkx999/Cats.git /opt/miaobot
cd /opt/miaobot
cp .env.example .env
nano .env
docker compose up -d --build
```

如果系统仓库没有 `docker-compose-plugin`，可按照 Docker 官方 Debian 文档安装 Compose Plugin 后再继续。

`.env` 中必须填写：

- `BOT_TOKEN`
- `BOT_USERNAME`（不带 `@`）
- `SUPERADMIN_IDS`
- `PUBLIC_BASE_URL`
- `DOMAIN`
- `FEEDBACK_USERNAME`
- `MATERIAL_CHANNEL_ID`
- `MATERIAL_CHANNEL_USERNAME`（不带 `@`）
- `POSTGRES_PASSWORD`
- `DATABASE_URL` 中同步替换数据库密码
- `APP_SECRET` 与 `LOGIN_CODE_PEPPER`

生成两个随机密钥：

```bash
openssl rand -hex 32
openssl rand -hex 32
```

## 素材频道配置

1. 建立公开频道并设置公开用户名。
2. 将喵Bot设为频道管理员，至少开启「发布消息」。
3. 将频道 ID 填入 `MATERIAL_CHANNEL_ID`，将公开用户名填入 `MATERIAL_CHANNEL_USERNAME`。
4. 用户在后台上传素材后，机器人会发布到该频道，并将 Telegram 返回的 `file_id` 用于后续定时发送。

公开频道消息链接用于后台查看素材，真正执行发送时优先使用 `file_id`，不会显示“转发自频道”。

## 群组权限

把喵Bot添加到群组并授予以下权限：

- 发送消息
- 发送图片和视频
- 删除消息（使用自动删除时需要）
- 置顶消息（使用自动置顶时需要）

随后由群主或管理员在群内发送：

```text
/connect
```

## 常用运维命令

```bash
cd /opt/miaobot
docker compose ps
docker compose logs -f --tail=200 web bot
docker compose restart web bot
git pull --ff-only && docker compose up -d --build
```

## 本地检查

```bash
python -m venv .venv
. .venv/bin/activate
pip install -e '.[dev]'
ruff check app tests
pytest
```

## 当前边界

- 第一版只处理定时发送，不包含积分、骰子、竞猜、成员等级等群管功能。
- 网页上传单个素材限制为 50MB，以控制 1C2G 服务器的瞬时内存占用。
- 超级后台降低用户额度时，不会删除或暂停已有任务；用户需要先降到额度以内，才能新增或重新启用任务。
