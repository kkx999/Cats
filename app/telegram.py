import asyncio
import logging
import time
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError, TelegramRetryAfter
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy import or_, select
from sqlalchemy.orm import selectinload

from app.config import settings
from app.database import SessionLocal
from app.models import ScheduledTask, SentMessage
from app.schedule import next_future_occurrence

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class SendResult:
    message_id: int
    warning: str | None = None


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


async def send_task(bot: Bot, task: ScheduledTask) -> SendResult:
    reply_markup = build_keyboard(task.buttons)
    parse_mode = "HTML" if (task.schedule_config or {}).get("parse_mode") == "html" else None
    if task.media and task.media.media_type == "photo":
        message = await bot.send_photo(
            task.chat_id,
            task.media.file_id,
            caption=task.text or None,
            parse_mode=parse_mode,
            reply_markup=reply_markup,
        )
    elif task.media and task.media.media_type == "video":
        message = await bot.send_video(
            task.chat_id,
            task.media.file_id,
            caption=task.text or None,
            parse_mode=parse_mode,
            reply_markup=reply_markup,
            supports_streaming=True,
        )
    else:
        message = await bot.send_message(
            task.chat_id,
            task.text,
            parse_mode=parse_mode,
            reply_markup=reply_markup,
            disable_web_page_preview=False,
        )
    warning = None
    if task.pin_message:
        try:
            await bot.pin_chat_message(task.chat_id, message.message_id, disable_notification=True)
        except TelegramAPIError as exc:
            # The content was already accepted by Telegram. Never retry the send
            # just because the optional pin operation failed.
            warning = f"消息已发送，但置顶失败：{exc}"
    return SendResult(message_id=message.message_id, warning=warning)


class Scheduler:
    def __init__(self, bot: Bot) -> None:
        self.bot = bot
        self.semaphore = asyncio.Semaphore(settings.scheduler_concurrency)
        self.chat_locks: defaultdict[int, asyncio.Lock] = defaultdict(asyncio.Lock)
        self.chat_last_sent: dict[int, float] = {}
        self.global_lock = asyncio.Lock()
        self.global_last_sent = 0.0
        self.running = True

    @staticmethod
    def _remember_retry_anchor(task: ScheduledTask) -> None:
        config = dict(task.schedule_config or {})
        if "_retry_anchor" not in config and task.next_run_at:
            config["_retry_anchor"] = task.next_run_at.isoformat()
        task.schedule_config = config

    @staticmethod
    def _consume_retry_anchor(task: ScheduledTask) -> datetime:
        config = dict(task.schedule_config or {})
        raw_anchor = config.pop("_retry_anchor", None)
        task.schedule_config = config
        if raw_anchor:
            return datetime.fromisoformat(raw_anchor)
        return task.next_run_at

    async def _respect_send_limits(self, chat_id: int) -> None:
        # Telegram documents a group limit of 20 messages/minute and a
        # broadcast limit of roughly 30 messages/second. Stay below both.
        elapsed = time.monotonic() - self.chat_last_sent.get(chat_id, 0.0)
        if elapsed < 3.05:
            await asyncio.sleep(3.05 - elapsed)
        async with self.global_lock:
            global_elapsed = time.monotonic() - self.global_last_sent
            if global_elapsed < 0.04:
                await asyncio.sleep(0.04 - global_elapsed)
            self.global_last_sent = time.monotonic()

    async def run(self) -> None:
        while self.running:
            try:
                await self._claim_and_send()
                await self._delete_expired()
            except Exception:
                logger.exception("scheduler loop failed")
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
            now = datetime.now(UTC)
            if task.end_at and task.end_at < now:
                task.active = False
                task.next_run_at = None
                task.locked_until = None
                await session.commit()
                return
            async with self.chat_locks[task.chat_id]:
                try:
                    await self._respect_send_limits(task.chat_id)
                    result = await send_task(self.bot, task)
                    self.chat_last_sent[task.chat_id] = time.monotonic()
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
                            message_id=result.message_id,
                            delete_at=delete_at,
                        )
                    )
                    task.total_sent += 1
                    task.consecutive_failures = 0
                    task.last_error = result.warning
                    task.last_sent_at = now
                    scheduled_for = self._consume_retry_anchor(task)
                    task.next_run_at = next_future_occurrence(task, scheduled_for, now)
                    task.active = task.next_run_at is not None
                except TelegramRetryAfter as exc:
                    self._remember_retry_anchor(task)
                    task.consecutive_failures += 1
                    task.last_error = str(exc)[:1000]
                    task.next_run_at = datetime.now(UTC) + timedelta(seconds=exc.retry_after + 1)
                except TelegramAPIError as exc:
                    self._remember_retry_anchor(task)
                    task.consecutive_failures += 1
                    task.last_error = str(exc)[:1000]
                    task.next_run_at = datetime.now(UTC) + timedelta(
                        minutes=min(30, 2 ** min(4, task.consecutive_failures))
                    )
                    if task.consecutive_failures >= 8:
                        task.active = False
                except Exception as exc:
                    logger.exception("unexpected task failure: %s", task.id)
                    self._remember_retry_anchor(task)
                    task.consecutive_failures += 1
                    task.last_error = f"内部执行错误：{exc}"[:1000]
                    task.next_run_at = datetime.now(UTC) + timedelta(minutes=5)
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
