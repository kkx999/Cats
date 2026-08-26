from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import SendMessage

from app.models import ScheduledTask
from app.telegram import Scheduler, send_task


class FakeBot:
    def __init__(self, pin_fails: bool = False) -> None:
        self.pin_fails = pin_fails
        self.parse_mode = "not-called"

    async def send_message(self, *args, **kwargs):
        self.parse_mode = kwargs.get("parse_mode")
        return SimpleNamespace(message_id=42)

    async def pin_chat_message(self, *args, **kwargs):
        if self.pin_fails:
            raise TelegramBadRequest(
                method=SendMessage(chat_id=-1001, text="test"),
                message="not enough rights",
            )


def make_task(*, pin: bool = False, parse_mode: str = "plain") -> ScheduledTask:
    return ScheduledTask(
        owner_id=1,
        chat_id=-1001,
        title="test",
        text="3 < 5 & 7 > 2",
        buttons=[],
        schedule_kind="once",
        schedule_config={"parse_mode": parse_mode},
        timezone="Asia/Shanghai",
        pin_message=pin,
    )


@pytest.mark.asyncio
async def test_plain_text_does_not_enable_html_parser() -> None:
    bot = FakeBot()
    result = await send_task(bot, make_task())
    assert result.message_id == 42
    assert bot.parse_mode is None


@pytest.mark.asyncio
async def test_pin_failure_does_not_fail_successful_send() -> None:
    bot = FakeBot(pin_fails=True)
    result = await send_task(bot, make_task(pin=True))
    assert result.message_id == 42
    assert "置顶失败" in result.warning


@pytest.mark.asyncio
async def test_html_is_only_enabled_explicitly() -> None:
    bot = FakeBot()
    await send_task(bot, make_task(parse_mode="html"))
    assert bot.parse_mode == "HTML"


def test_retry_keeps_original_schedule_anchor() -> None:
    item = make_task()
    original = datetime(2026, 8, 26, 12, 0, tzinfo=UTC)
    item.next_run_at = original
    Scheduler._remember_retry_anchor(item)
    item.next_run_at = original + timedelta(minutes=5)
    assert Scheduler._consume_retry_anchor(item) == original
    assert "_retry_anchor" not in item.schedule_config
