from collections.abc import AsyncIterator

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import settings

engine = create_async_engine(settings.database_url, pool_pre_ping=True)
SessionLocal = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)


async def get_session() -> AsyncIterator[AsyncSession]:
    async with SessionLocal() as session:
        yield session


async def init_database() -> None:
    from app.models import Base

    async with engine.begin() as connection:
        if connection.dialect.name == "postgresql":
            # The web and bot containers start together. A transaction-scoped
            # advisory lock prevents both processes from creating tables at once.
            await connection.execute(text("SELECT pg_advisory_xact_lock(68422681)"))
        await connection.run_sync(Base.metadata.create_all)
