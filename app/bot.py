import asyncio
import contextlib
import re
from datetime import UTC, datetime

from aiogram import Bot, Dispatcher, F, Router
from aiogram.enums import ChatMemberStatus, ChatType
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    ChatMemberUpdated,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from sqlalchemy import select

from app.config import settings
from app.database import SessionLocal, init_database
from app.models import AppSetting, Chat, LoginChallenge, User, UserChat
from app.security import hash_login_code
from app.telegram import Scheduler

router = Router()


async def upsert_user(message: Message) -> User:
    sender = message.from_user
    if not sender:
        raise RuntimeError("缺少用户信息")
    async with SessionLocal() as session:
        user = await session.get(User, sender.id)
        if not user:
            user = User(id=sender.id)
            session.add(user)
        user.username = sender.username
        user.display_name = sender.full_name
        user.last_seen_at = datetime.now(UTC)
        await session.commit()
        return user


async def setting(key: str, fallback: str = "") -> str:
    async with SessionLocal() as session:
        item = await session.get(AppSetting, key)
        return item.value if item else fallback


async def main_keyboard(bot: Bot) -> InlineKeyboardMarkup:
    me = await bot.get_me()
    feedback = await setting("feedback_username", settings.feedback_username)
    rows = [
        [InlineKeyboardButton(text="⚙️ 管理后台", url=f"{settings.normalized_base_url}/login")],
        [
            InlineKeyboardButton(
                text="✅ 把我添加到群", url=f"https://t.me/{me.username}?startgroup=true"
            )
        ],
    ]
    if feedback:
        rows.append(
            [InlineKeyboardButton(text="💡 问题反馈", url=f"https://t.me/{feedback.lstrip('@')}")]
        )
    return InlineKeyboardMarkup(inline_keyboard=rows)


@router.message(CommandStart(), F.chat.type == ChatType.PRIVATE)
async def start(message: Message, bot: Bot) -> None:
    await upsert_user(message)
    await message.answer(
        "<b>欢迎使用喵Bot</b> 🐾\n\n"
        "把文字、图片或视频按照设定时间发送到你的群组。任务会持久保存，服务重启后也不会丢失。\n\n"
        "点击下方按钮进入管理后台。",
        parse_mode="HTML",
        reply_markup=await main_keyboard(bot),
    )


@router.message(Command("connect"), F.chat.type.in_({ChatType.GROUP, ChatType.SUPERGROUP}))
async def connect_group(message: Message, bot: Bot) -> None:
    if not message.from_user:
        return
    member = await bot.get_chat_member(message.chat.id, message.from_user.id)
    if member.status not in {ChatMemberStatus.CREATOR, ChatMemberStatus.ADMINISTRATOR}:
        await message.reply("只有群主或管理员可以绑定这个群。")
        return
    await register_chat(
        message.chat.id,
        message.chat.title or "未命名群组",
        message.chat.username,
        message.chat.type,
        message.from_user.id,
        member.status.value,
    )
    await message.reply("✅ 群组已绑定。现在可以进入喵Bot后台创建定时消息。")


async def register_chat(
    chat_id: int,
    title: str,
    username: str | None,
    chat_type: str,
    user_id: int,
    role: str,
) -> None:
    async with SessionLocal() as session:
        user = await session.get(User, user_id)
        if not user:
            user = User(id=user_id, display_name="Telegram 用户")
            session.add(user)
            await session.flush()
        chat = await session.get(Chat, chat_id)
        if not chat:
            chat = Chat(id=chat_id, title=title, username=username, chat_type=chat_type)
            session.add(chat)
        else:
            chat.title = title
            chat.username = username
            chat.active = True
        relation = await session.scalar(
            select(UserChat).where(UserChat.user_id == user_id, UserChat.chat_id == chat_id)
        )
        if not relation:
            session.add(UserChat(user_id=user_id, chat_id=chat_id, role=role))
        else:
            relation.role = role
        await session.commit()


@router.my_chat_member()
async def membership_changed(change: ChatMemberUpdated) -> None:
    if change.chat.type not in {ChatType.GROUP, ChatType.SUPERGROUP}:
        return
    active = change.new_chat_member.status in {
        ChatMemberStatus.MEMBER,
        ChatMemberStatus.ADMINISTRATOR,
    }
    if active:
        await register_chat(
            change.chat.id,
            change.chat.title or "未命名群组",
            change.chat.username,
            change.chat.type,
            change.from_user.id,
            "administrator",
        )
    else:
        async with SessionLocal() as session:
            chat = await session.get(Chat, change.chat.id)
            if chat:
                chat.active = False
                await session.commit()


@router.message(F.chat.type == ChatType.PRIVATE, F.text.regexp(r"^\d{6}$"))
async def confirm_login(message: Message) -> None:
    await upsert_user(message)
    code = re.sub(r"\D", "", message.text or "")
    now = datetime.now(UTC)
    async with SessionLocal() as session:
        challenge = await session.scalar(
            select(LoginChallenge)
            .where(
                LoginChallenge.code_hash == hash_login_code(code),
                LoginChallenge.expires_at > now,
                LoginChallenge.confirmed_at.is_(None),
            )
            .order_by(LoginChallenge.created_at.desc())
        )
        if not challenge:
            await message.answer("验证码无效或已过期，请返回后台重新获取。")
            return
        challenge.user_id = message.from_user.id
        challenge.confirmed_at = now
        await session.commit()
        link = f"{settings.normalized_base_url}/auth/link/{challenge.access_token}"
    await message.answer(
        "✅ 验证成功，浏览器会自动进入后台。\n\n"
        "你也可以点击下面的链接登录；链接在生成后的 5 分钟内保持有效。",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text="打开喵Bot后台", url=link)]]
        ),
    )


async def run() -> None:
    if not settings.bot_token:
        raise RuntimeError("BOT_TOKEN 未配置")
    await init_database()
    bot = Bot(settings.bot_token)
    dispatcher = Dispatcher()
    dispatcher.include_router(router)
    scheduler = Scheduler(bot)
    scheduler_task = asyncio.create_task(scheduler.run())
    try:
        await bot.delete_webhook(drop_pending_updates=False)
        await dispatcher.start_polling(bot, allowed_updates=dispatcher.resolve_used_update_types())
    finally:
        scheduler.running = False
        scheduler_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await scheduler_task
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(run())
