import asyncio
from collections import defaultdict
from datetime import UTC, datetime, timedelta

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy import or_, select
from sqlalchemy.orm import selectinload

from app.config import settings
from app.database import SessionLocal
from app.models import ScheduledTask, SentMessage
from app.schedule import next_occurrence


def build_keyboard(rows: list) -> InlineKeyboardMarkup | None:
    keyboard: list[list[InlineKeyboardButton]] = []
    for row in rows or []:
        buttons = []
        for item in row:
            text = str(item.get("text", "")).strip()
            url = str(item.get("url", "")).strip()
            if text and url.startswith(("https://", "http://", "tg://")):
                buttons.append(InlineKeyboardButton(text=text, url=url))
        if buttons:
            keyboard.append(buttons)
    return InlineKeyboardMarkup(inline_keyboard=keyboard) if keyboard else None


async def send_task(bot: Bot, task: ScheduledTask) -> int:
    reply_markup = build_keyboard(task.buttons)
    if task.media and task.media.media_type == "photo":
        message = await bot.send_photo(
            task.chat_id,
            task.media.file_id,
            caption=task.text or None,
            parse_mode="HTML",
            reply_markup=reply_markup,
        )
    elif task.media and task.media.media_type == "video":
        message = await bot.send_video(
            task.chat_id,
            task.media.file_id,
            caption=task.text or None,
            parse_mode="HTML",
            reply_markup=reply_markup,
            supports_streaming=True,
        )
    else:
        message = await bot.send_message(
            task.chat_id,
            task.text,
            parse_mode="HTML",
            reply_markup=reply_markup,
            disable_web_page_preview=False,
        )
    if task.pin_message:
        await bot.pin_chat_message(task.chat_id, message.message_id, disable_notification=True)
    return message.message_id


class Scheduler:
    def __init__(self, bot: Bot) -> None:
        self.bot = bot
        self.semaphore = asyncio.Semaphore(settings.scheduler_concurrency)
        self.chat_locks: defaultdict[int, asyncio.Lock] = defaultdict(asyncio.Lock)
        self.running = True

    async def run(self) -> None:
        while self.running:
            try:
                await self._claim_and_send()
                await self._delete_expired()
            except Exception:
                await asyncio.sleep(2)
            await asyncio.sleep(0.5)

    async def _claim_and_send(self) -> None:
        now = datetime.now(UTC)
        async with SessionLocal() as session:
            query = (
                select(ScheduledTask)
                .where(
                    ScheduledTask.active.is_(True),
                    ScheduledTask.next_run_at <= now,
                    or_(ScheduledTask.locked_until.is_(None), ScheduledTask.locked_until < now),
                )
                .order_by(ScheduledTask.next_run_at)
                .limit(settings.scheduler_concurrency)
                .with_for_update(skip_locked=True)
            )
            tasks = list((await session.scalars(query)).all())
            for task in tasks:
                task.locked_until = now + timedelta(seconds=90)
            await session.commit()

        if tasks:
            await asyncio.gather(*(self._execute(task.id) for task in tasks))

    async def _execute(self, task_id: str) -> None:
        async with self.semaphore, SessionLocal() as session:
            task = await session.scalar(
                select(ScheduledTask)
                .where(ScheduledTask.id == task_id)
                .options(selectinload(ScheduledTask.media))
            )
            if not task or not task.active:
                return
            async with self.chat_locks[task.chat_id]:
                try:
                    message_id = await send_task(self.bot, task)
                    now = datetime.now(UTC)
                    delete_at = (
                        now + timedelta(seconds=task.auto_delete_seconds)
                        if task.auto_delete_seconds
                        else None
                    )
                    session.add(
                        SentMessage(
                            task_id=task.id,
                            chat_id=task.chat_id,
                            message_id=message_id,
                            delete_at=delete_at,
                        )
                    )
                    task.total_sent += 1
                    task.consecutive_failures = 0
                    task.last_error = None
                    task.last_sent_at = now
                    task.next_run_at = next_occurrence(task, task.next_run_at)
                    task.active = task.next_run_at is not None
                    await asyncio.sleep(0.05)
                except TelegramAPIError as exc:
                    task.consecutive_failures += 1
                    task.last_error = str(exc)[:1000]
                    task.next_run_at = datetime.now(UTC) + timedelta(
                        minutes=min(30, 2 ** min(4, task.consecutive_failures))
                    )
                    if task.consecutive_failures >= 8:
                        task.active = False
                finally:
                    task.locked_until = None
                    await session.commit()

    async def _delete_expired(self) -> None:
        now = datetime.now(UTC)
        async with SessionLocal() as session:
            messages = list(
                (
                    await session.scalars(
                        select(SentMessage)
                        .where(
                            SentMessage.deleted.is_(False),
                            SentMessage.delete_at.is_not(None),
                            SentMessage.delete_at <= now,
                        )
                        .limit(50)
                    )
                ).all()
            )
            for item in messages:
                try:
                    await self.bot.delete_message(item.chat_id, item.message_id)
                except TelegramAPIError:
                    pass
                item.deleted = True
            if messages:
                await session.commit()
