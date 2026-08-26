import asyncio
import json
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from aiogram import Bot
from aiogram.enums import ChatMemberStatus
from aiogram.exceptions import TelegramAPIError
from aiogram.types import BufferedInputFile
from fastapi import (
    BackgroundTasks,
    Depends,
    FastAPI,
    File,
    Form,
    HTTPException,
    Request,
    UploadFile,
)
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.config import settings
from app.database import SessionLocal, get_session, init_database
from app.models import (
    AppSetting,
    AuditLog,
    Chat,
    LoginChallenge,
    Media,
    QuotaOverride,
    ScheduledTask,
    User,
    UserChat,
)
from app.schedule import describe_schedule, local_to_utc, utc_to_local_input
from app.security import COOKIE_NAME, create_challenge, current_user, is_superadmin, sign_session
from app.telegram import send_task

ROOT = Path(__file__).parent
templates = Jinja2Templates(directory=ROOT / "templates")
templates.env.globals.update(
    describe_schedule=describe_schedule,
    local_datetime=utc_to_local_input,
    is_superadmin=is_superadmin,
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_database()
    app.state.bot = Bot(settings.bot_token) if settings.bot_token else None
    yield
    if app.state.bot:
        await app.state.bot.session.close()


app = FastAPI(title="喵Bot", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")
media_upload_semaphore = asyncio.Semaphore(4)


def render(request: Request, name: str, **context: object) -> HTMLResponse:
    return templates.TemplateResponse(request=request, name=name, context=context)


async def require_user(request: Request, session: AsyncSession) -> User:
    return await current_user(request, session)


async def effective_limit(session: AsyncSession, user_id: int) -> int | None:
    override = await session.get(QuotaOverride, user_id)
    now = datetime.now(UTC)
    if override and (not override.expires_at or override.expires_at > now):
        return override.task_limit
    return settings.default_task_limit


async def owned_chats(session: AsyncSession, user_id: int) -> list[Chat]:
    return list(
        (
            await session.scalars(
                select(Chat)
                .join(UserChat, UserChat.chat_id == Chat.id)
                .where(UserChat.user_id == user_id, Chat.active.is_(True))
                .order_by(Chat.title)
            )
        ).all()
    )


async def owned_task(session: AsyncSession, task_id: str, user_id: int) -> ScheduledTask:
    task = await session.scalar(
        select(ScheduledTask)
        .where(ScheduledTask.id == task_id, ScheduledTask.owner_id == user_id)
        .options(selectinload(ScheduledTask.media), selectinload(ScheduledTask.chat))
    )
    if not task:
        raise HTTPException(404, "任务不存在")
    return task


async def verify_chat_admin(
    request: Request,
    session: AsyncSession,
    user_id: int,
    chat_id: int,
) -> None:
    bot: Bot | None = request.app.state.bot
    if not bot:
        raise HTTPException(503, "机器人尚未配置")
    try:
        member = await bot.get_chat_member(chat_id, user_id)
    except TelegramAPIError as exc:
        raise HTTPException(502, "暂时无法向 Telegram 核对群管理权限") from exc
    if member.status in {ChatMemberStatus.CREATOR, ChatMemberStatus.ADMINISTRATOR}:
        return
    await session.execute(
        delete(UserChat).where(UserChat.user_id == user_id, UserChat.chat_id == chat_id)
    )
    await session.commit()
    raise HTTPException(403, "你的群管理员权限已经失效，请重新绑定")


@app.get("/health")
async def health() -> dict:
    return {"status": "ok", "service": "喵Bot"}


@app.get("/", response_class=HTMLResponse)
async def home(request: Request) -> HTMLResponse:
    return render(request, "home.html")


@app.get("/login", response_class=HTMLResponse)
async def login(request: Request, session: AsyncSession = Depends(get_session)) -> HTMLResponse:
    challenge, code = await create_challenge(session)
    return render(
        request,
        "login.html",
        challenge=challenge,
        code=code,
        expires_seconds=settings.login_ttl_seconds,
        bot_username=settings.bot_username,
    )


@app.get("/api/auth/status/{challenge_id}")
async def auth_status(challenge_id: str, session: AsyncSession = Depends(get_session)) -> dict:
    challenge = await session.get(LoginChallenge, challenge_id)
    if not challenge or challenge.expires_at <= datetime.now(UTC):
        return {"status": "expired"}
    if challenge.confirmed_at and challenge.user_id:
        return {"status": "confirmed", "url": f"/auth/link/{challenge.access_token}"}
    return {"status": "pending"}


@app.get("/auth/link/{token}")
async def login_link(token: str, session: AsyncSession = Depends(get_session)) -> RedirectResponse:
    challenge = await session.scalar(
        select(LoginChallenge).where(
            LoginChallenge.access_token == token,
            LoginChallenge.expires_at > datetime.now(UTC),
            LoginChallenge.user_id.is_not(None),
        )
    )
    if not challenge or not challenge.user_id:
        return RedirectResponse("/login?expired=1", status_code=303)
    response = RedirectResponse("/dashboard", status_code=303)
    response.set_cookie(
        COOKIE_NAME,
        sign_session(challenge.user_id),
        max_age=60 * 60 * 24 * 30,
        httponly=True,
        secure=settings.public_base_url.startswith("https://"),
        samesite="lax",
    )
    return response


@app.get("/logout")
async def logout() -> RedirectResponse:
    response = RedirectResponse("/login", status_code=303)
    response.delete_cookie(COOKIE_NAME)
    return response


@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard(request: Request, session: AsyncSession = Depends(get_session)) -> HTMLResponse:
    user = await require_user(request, session)
    tasks = list(
        (
            await session.scalars(
                select(ScheduledTask)
                .where(ScheduledTask.owner_id == user.id)
                .options(selectinload(ScheduledTask.chat), selectinload(ScheduledTask.media))
                .order_by(ScheduledTask.active.desc(), ScheduledTask.created_at.desc())
            )
        ).all()
    )
    chats = await owned_chats(session, user.id)
    limit = await effective_limit(session, user.id)
    active_count = sum(1 for task in tasks if task.active)
    return render(
        request,
        "dashboard.html",
        user=user,
        tasks=tasks,
        chats=chats,
        task_limit=limit,
        active_count=active_count,
        timezone=settings.default_timezone,
    )


@app.get("/tasks/new", response_class=HTMLResponse)
async def new_task(request: Request, session: AsyncSession = Depends(get_session)) -> HTMLResponse:
    user = await require_user(request, session)
    chats = await owned_chats(session, user.id)
    media = list(
        (
            await session.scalars(
                select(Media)
                .where(Media.owner_id == user.id)
                .order_by(Media.created_at.desc())
                .limit(30)
            )
        ).all()
    )
    return render(
        request,
        "task_form.html",
        user=user,
        task=None,
        chats=chats,
        media_items=media,
        default_timezone=settings.default_timezone,
    )


@app.get("/tasks/{task_id}/edit", response_class=HTMLResponse)
async def edit_task(
    request: Request, task_id: str, session: AsyncSession = Depends(get_session)
) -> HTMLResponse:
    user = await require_user(request, session)
    task = await owned_task(session, task_id, user.id)
    await verify_chat_admin(request, session, user.id, task.chat_id)
    chats = await owned_chats(session, user.id)
    media = list(
        (
            await session.scalars(
                select(Media)
                .where(Media.owner_id == user.id)
                .order_by(Media.created_at.desc())
                .limit(30)
            )
        ).all()
    )
    return render(
        request,
        "task_form.html",
        user=user,
        task=task,
        chats=chats,
        media_items=media,
        default_timezone=task.timezone,
    )


def parse_buttons(raw: str) -> list:
    try:
        data = json.loads(raw or "[]")
    except json.JSONDecodeError as exc:
        raise HTTPException(422, "按钮格式错误") from exc
    if not isinstance(data, list) or len(data) > 8:
        raise HTTPException(422, "按钮最多 8 行")
    clean = []
    for row in data:
        if not isinstance(row, list) or len(row) > 4:
            raise HTTPException(422, "每行最多 4 个按钮")
        clean_row = []
        for item in row:
            if not isinstance(item, dict):
                raise HTTPException(422, "按钮格式错误")
            text = str(item.get("text", "")).strip()[:64]
            url = str(item.get("url", "")).strip()
            if len(url) > 2048:
                raise HTTPException(422, "按钮链接过长")
            if text and url.startswith(("https://", "http://", "tg://")):
                clean_row.append({"text": text, "url": url})
        if clean_row:
            clean.append(clean_row)
    return clean


async def save_task_from_form(
    request: Request,
    session: AsyncSession,
    user: User,
    task: ScheduledTask | None,
    *,
    title: str,
    chat_id: int,
    text: str,
    media_id: str,
    buttons_json: str,
    schedule_kind: str,
    start_at: str,
    end_at: str,
    timezone: str,
    interval: int,
    interval_unit: str,
    parse_mode: str,
    pin_message: bool,
    auto_delete_seconds: int | None,
) -> ScheduledTask:
    await verify_chat_admin(request, session, user.id, chat_id)
    relation = await session.scalar(
        select(UserChat).where(UserChat.user_id == user.id, UserChat.chat_id == chat_id)
    )
    if not relation:
        raise HTTPException(403, "你没有管理这个群组的权限")
    if not text.strip() and not media_id:
        raise HTTPException(422, "文字和素材不能同时为空")
    try:
        ZoneInfo(timezone)
        start_utc = local_to_utc(start_at, timezone)
        end_utc = local_to_utc(end_at, timezone) if end_at else None
    except (ValueError, ZoneInfoNotFoundError) as exc:
        raise HTTPException(422, "时间或时区格式错误") from exc
    if end_utc and end_utc < start_utc:
        raise HTTPException(422, "结束时间不能早于开始时间")
    if schedule_kind not in {"once", "daily", "weekly", "monthly", "interval"}:
        raise HTTPException(422, "不支持的发送周期")
    if parse_mode not in {"plain", "html"}:
        raise HTTPException(422, "不支持的文字格式")
    if not 1 <= interval <= 999:
        raise HTTPException(422, "自定义间隔必须在 1 到 999 之间")
    if auto_delete_seconds is not None and not 60 <= auto_delete_seconds <= 604800:
        raise HTTPException(422, "自动删除时间必须在 1 分钟到 7 天之间")
    content_limit = 1024 if media_id else 4096
    if len(text) > content_limit:
        label = "媒体说明" if media_id else "消息文字"
        raise HTTPException(422, f"{label}不能超过 {content_limit} 个字符")
    if end_utc and end_utc < datetime.now(UTC):
        raise HTTPException(422, "结束时间已经过去")

    if task is None:
        await session.execute(select(User.id).where(User.id == user.id).with_for_update())
        active_count = await session.scalar(
            select(func.count())
            .select_from(ScheduledTask)
            .where(ScheduledTask.owner_id == user.id, ScheduledTask.active.is_(True))
        )
        limit = await effective_limit(session, user.id)
        if limit is not None and int(active_count or 0) >= limit:
            raise HTTPException(409, f"当前最多可启用 {limit} 个定时任务")
        task = ScheduledTask(owner_id=user.id, chat_id=chat_id, title=title[:100])
        session.add(task)

    if media_id:
        media = await session.scalar(
            select(Media).where(Media.id == media_id, Media.owner_id == user.id)
        )
        if not media:
            raise HTTPException(422, "素材不存在")
    task.title = title.strip()[:100] or "未命名任务"
    task.chat_id = chat_id
    task.text = text.strip()
    task.media_id = media_id or None
    task.buttons = parse_buttons(buttons_json)
    task.schedule_kind = schedule_kind
    task.schedule_config = {
        "interval": max(1, interval),
        "unit": interval_unit if interval_unit in {"minutes", "hours", "days"} else "hours",
        "parse_mode": parse_mode,
        "day_of_month": start_utc.astimezone(ZoneInfo(timezone)).day,
    }
    task.timezone = timezone
    task.start_at = start_utc
    task.end_at = end_utc
    task.next_run_at = start_utc
    task.active = True
    task.pin_message = pin_message
    task.auto_delete_seconds = (
        auto_delete_seconds if auto_delete_seconds and auto_delete_seconds > 0 else None
    )
    task.locked_until = None
    await session.commit()
    return task


@app.post("/tasks/save")
async def create_task(
    request: Request,
    title: str = Form(...),
    chat_id: int = Form(...),
    text: str = Form(""),
    media_id: str = Form(""),
    buttons_json: str = Form("[]"),
    schedule_kind: str = Form("once"),
    start_at: str = Form(...),
    end_at: str = Form(""),
    timezone: str = Form("Asia/Shanghai"),
    interval: int = Form(1),
    interval_unit: str = Form("hours"),
    parse_mode: str = Form("plain"),
    pin_message: bool = Form(False),
    auto_delete_seconds: int | None = Form(None),
    session: AsyncSession = Depends(get_session),
) -> RedirectResponse:
    user = await require_user(request, session)
    await save_task_from_form(
        request,
        session,
        user,
        None,
        title=title,
        chat_id=chat_id,
        text=text,
        media_id=media_id,
        buttons_json=buttons_json,
        schedule_kind=schedule_kind,
        start_at=start_at,
        end_at=end_at,
        timezone=timezone,
        interval=interval,
        interval_unit=interval_unit,
        parse_mode=parse_mode,
        pin_message=pin_message,
        auto_delete_seconds=auto_delete_seconds,
    )
    return RedirectResponse("/dashboard?saved=1", status_code=303)


@app.post("/tasks/{task_id}/save")
async def update_task(
    request: Request,
    task_id: str,
    title: str = Form(...),
    chat_id: int = Form(...),
    text: str = Form(""),
    media_id: str = Form(""),
    buttons_json: str = Form("[]"),
    schedule_kind: str = Form("once"),
    start_at: str = Form(...),
    end_at: str = Form(""),
    timezone: str = Form("Asia/Shanghai"),
    interval: int = Form(1),
    interval_unit: str = Form("hours"),
    parse_mode: str = Form("plain"),
    pin_message: bool = Form(False),
    auto_delete_seconds: int | None = Form(None),
    session: AsyncSession = Depends(get_session),
) -> RedirectResponse:
    user = await require_user(request, session)
    task = await owned_task(session, task_id, user.id)
    await save_task_from_form(
        request,
        session,
        user,
        task,
        title=title,
        chat_id=chat_id,
        text=text,
        media_id=media_id,
        buttons_json=buttons_json,
        schedule_kind=schedule_kind,
        start_at=start_at,
        end_at=end_at,
        timezone=timezone,
        interval=interval,
        interval_unit=interval_unit,
        parse_mode=parse_mode,
        pin_message=pin_message,
        auto_delete_seconds=auto_delete_seconds,
    )
    return RedirectResponse("/dashboard?saved=1", status_code=303)


@app.post("/tasks/{task_id}/toggle")
async def toggle_task(
    request: Request, task_id: str, session: AsyncSession = Depends(get_session)
) -> RedirectResponse:
    user = await require_user(request, session)
    task = await owned_task(session, task_id, user.id)
    await verify_chat_admin(request, session, user.id, task.chat_id)
    if not task.active:
        await session.execute(select(User.id).where(User.id == user.id).with_for_update())
        limit = await effective_limit(session, user.id)
        count = await session.scalar(
            select(func.count())
            .select_from(ScheduledTask)
            .where(ScheduledTask.owner_id == user.id, ScheduledTask.active.is_(True))
        )
        if limit is not None and int(count or 0) >= limit:
            return RedirectResponse("/dashboard?quota=1", status_code=303)
        task.next_run_at = max(task.start_at, datetime.now(UTC))
    task.active = not task.active
    task.locked_until = None
    await session.commit()
    return RedirectResponse("/dashboard", status_code=303)


@app.post("/tasks/{task_id}/delete")
async def delete_task_route(
    request: Request, task_id: str, session: AsyncSession = Depends(get_session)
) -> RedirectResponse:
    user = await require_user(request, session)
    await owned_task(session, task_id, user.id)
    await session.execute(delete(ScheduledTask).where(ScheduledTask.id == task_id))
    await session.commit()
    return RedirectResponse("/dashboard?deleted=1", status_code=303)


@app.post("/tasks/{task_id}/copy")
async def copy_task(
    request: Request, task_id: str, session: AsyncSession = Depends(get_session)
) -> RedirectResponse:
    user = await require_user(request, session)
    source = await owned_task(session, task_id, user.id)
    await verify_chat_admin(request, session, user.id, source.chat_id)
    await session.execute(select(User.id).where(User.id == user.id).with_for_update())
    limit = await effective_limit(session, user.id)
    count = await session.scalar(
        select(func.count())
        .select_from(ScheduledTask)
        .where(ScheduledTask.owner_id == user.id, ScheduledTask.active.is_(True))
    )
    active = limit is None or int(count or 0) < limit
    clone = ScheduledTask(
        owner_id=user.id,
        chat_id=source.chat_id,
        media_id=source.media_id,
        title=f"{source.title} - 副本"[:100],
        text=source.text,
        buttons=source.buttons,
        schedule_kind=source.schedule_kind,
        schedule_config=source.schedule_config,
        timezone=source.timezone,
        start_at=source.start_at,
        end_at=source.end_at,
        next_run_at=max(source.start_at, datetime.now(UTC)) if active else None,
        active=active,
        pin_message=source.pin_message,
        auto_delete_seconds=source.auto_delete_seconds,
    )
    session.add(clone)
    await session.commit()
    return RedirectResponse("/dashboard?copied=1", status_code=303)


@app.post("/tasks/{task_id}/test")
async def test_task(
    request: Request, task_id: str, session: AsyncSession = Depends(get_session)
) -> RedirectResponse:
    user = await require_user(request, session)
    task = await owned_task(session, task_id, user.id)
    await verify_chat_admin(request, session, user.id, task.chat_id)
    bot: Bot | None = request.app.state.bot
    if not bot:
        raise HTTPException(503, "机器人尚未配置")
    try:
        send_result = await send_task(bot, task)
        task.last_error = send_result.warning
        await session.commit()
        result = "test_ok=1"
    except Exception as exc:
        task.last_error = str(exc)[:1000]
        await session.commit()
        result = "test_failed=1"
    return RedirectResponse(f"/dashboard?{result}", status_code=303)


@app.post("/api/media")
async def upload_media(
    request: Request,
    file: UploadFile = File(...),
    session: AsyncSession = Depends(get_session),
) -> JSONResponse:
    user = await require_user(request, session)
    bot: Bot | None = request.app.state.bot
    if not bot or not settings.material_channel_id:
        raise HTTPException(503, "公开素材频道尚未配置")
    content_type = file.content_type or ""
    media_type = (
        "photo"
        if content_type.startswith("image/")
        else "video"
        if content_type.startswith("video/")
        else ""
    )
    if not media_type:
        raise HTTPException(415, "目前只支持图片和视频")
    async with media_upload_semaphore:
        data = await file.read(50 * 1024 * 1024 + 1)
        if len(data) > 50 * 1024 * 1024:
            raise HTTPException(413, "素材不能超过 50MB")
        upload = BufferedInputFile(data, filename=file.filename or f"media.{media_type}")
        caption = "🐾 喵Bot 公开素材"
        try:
            if media_type == "photo":
                message = await bot.send_photo(
                    settings.material_channel_id, upload, caption=caption
                )
                telegram_file = message.photo[-1]
            else:
                message = await bot.send_video(
                    settings.material_channel_id, upload, caption=caption, supports_streaming=True
                )
                telegram_file = message.video
        except TelegramAPIError as exc:
            raise HTTPException(502, f"素材上传到 Telegram 失败：{exc}") from exc
    public_url = None
    if settings.material_channel_username:
        public_url = (
            f"https://t.me/{settings.material_channel_username.lstrip('@')}/{message.message_id}"
        )
    item = Media(
        owner_id=user.id,
        media_type=media_type,
        file_id=telegram_file.file_id,
        file_unique_id=telegram_file.file_unique_id,
        channel_id=settings.material_channel_id,
        channel_message_id=message.message_id,
        public_url=public_url,
        original_name=(file.filename or "素材")[:255],
    )
    session.add(item)
    await session.commit()
    return JSONResponse(
        {
            "id": item.id,
            "type": item.media_type,
            "name": item.original_name,
            "public_url": item.public_url,
        }
    )


@app.get("/superadmin", response_class=HTMLResponse)
async def superadmin(
    request: Request, session: AsyncSession = Depends(get_session)
) -> HTMLResponse:
    user = await require_user(request, session)
    if not is_superadmin(user.id):
        raise HTTPException(403, "无权访问")
    active_tasks = (
        select(func.count(ScheduledTask.id))
        .where(ScheduledTask.owner_id == User.id, ScheduledTask.active.is_(True))
        .correlate(User)
        .scalar_subquery()
    )
    users = list(
        (
            await session.execute(
                select(User, QuotaOverride, active_tasks.label("active_tasks"))
                .outerjoin(QuotaOverride, QuotaOverride.user_id == User.id)
                .order_by(User.last_seen_at.desc())
            )
        ).all()
    )
    app_settings = {
        item.key: item.value for item in (await session.scalars(select(AppSetting))).all()
    }
    stats = {
        "users": await session.scalar(select(func.count()).select_from(User)),
        "chats": await session.scalar(
            select(func.count()).select_from(Chat).where(Chat.active.is_(True))
        ),
        "tasks": await session.scalar(
            select(func.count()).select_from(ScheduledTask).where(ScheduledTask.active.is_(True))
        ),
        "sent": await session.scalar(select(func.sum(ScheduledTask.total_sent))) or 0,
    }
    return render(
        request,
        "superadmin.html",
        user=user,
        users=users,
        stats=stats,
        app_settings=app_settings,
        default_limit=settings.default_task_limit,
    )


@app.post("/superadmin/quota/{user_id}")
async def update_quota(
    request: Request,
    user_id: int,
    task_limit: str = Form(...),
    session: AsyncSession = Depends(get_session),
) -> RedirectResponse:
    actor = await require_user(request, session)
    if not is_superadmin(actor.id):
        raise HTTPException(403, "无权访问")
    override = await session.get(QuotaOverride, user_id)
    if not override:
        override = QuotaOverride(user_id=user_id)
        session.add(override)
    override.task_limit = None if task_limit == "unlimited" else max(0, int(task_limit))
    session.add(
        AuditLog(
            actor_id=actor.id,
            action="quota.update",
            target=str(user_id),
            detail={"task_limit": task_limit},
        )
    )
    await session.commit()
    return RedirectResponse("/superadmin?saved=1", status_code=303)


@app.post("/superadmin/settings")
async def update_settings(
    request: Request,
    feedback_username: str = Form(""),
    session: AsyncSession = Depends(get_session),
) -> RedirectResponse:
    actor = await require_user(request, session)
    if not is_superadmin(actor.id):
        raise HTTPException(403, "无权访问")
    item = await session.get(AppSetting, "feedback_username")
    if not item:
        item = AppSetting(key="feedback_username")
        session.add(item)
    item.value = feedback_username.strip().lstrip("@")
    session.add(
        AuditLog(
            actor_id=actor.id,
            action="settings.update",
            target="feedback_username",
            detail={"value": item.value},
        )
    )
    await session.commit()
    return RedirectResponse("/superadmin?saved=1", status_code=303)


async def broadcast_announcement(text: str) -> None:
    if not settings.bot_token:
        return
    bot = Bot(settings.bot_token)
    try:
        async with SessionLocal() as session:
            user_ids = list(
                (await session.scalars(select(User.id).where(User.blocked.is_(False)))).all()
            )
        for user_id in user_ids:
            try:
                await bot.send_message(user_id, f"🐾 喵Bot 公告\n\n{text}")
            except Exception:
                pass
            await asyncio.sleep(0.04)
    finally:
        await bot.session.close()


@app.post("/superadmin/announcement")
async def announcement(
    request: Request,
    background_tasks: BackgroundTasks,
    text: str = Form(...),
    session: AsyncSession = Depends(get_session),
) -> RedirectResponse:
    actor = await require_user(request, session)
    if not is_superadmin(actor.id):
        raise HTTPException(403, "无权访问")
    if not text.strip():
        raise HTTPException(422, "公告不能为空")
    if len(text) > 4000:
        raise HTTPException(422, "公告不能超过 4000 个字符")
    session.add(
        AuditLog(actor_id=actor.id, action="announcement.send", detail={"text": text[:500]})
    )
    await session.commit()
    background_tasks.add_task(broadcast_announcement, text.strip())
    return RedirectResponse("/superadmin?broadcast=1", status_code=303)
