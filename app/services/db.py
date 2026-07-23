"""Асинхронное подключение к PostgreSQL (SQLAlchemy 2.0).

Один engine и фабрика сессий на процесс. Для MVP схема создаётся через
``init_db()`` (``create_all``) на старте приложения; при усложнении — перейти на
Alembic-миграции.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from loguru import logger
from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.core.config import get_settings
from app.services.models import Base

_engine: AsyncEngine | None = None
_sessionmaker: async_sessionmaker[AsyncSession] | None = None


def get_engine() -> AsyncEngine:
    global _engine, _sessionmaker
    if _engine is None:
        settings = get_settings()
        _engine = create_async_engine(settings.database_url, pool_pre_ping=True)
        _sessionmaker = async_sessionmaker(_engine, expire_on_commit=False)
        logger.info("Database engine created")
    return _engine


def get_sessionmaker() -> async_sessionmaker[AsyncSession]:
    if _sessionmaker is None:
        get_engine()
    assert _sessionmaker is not None
    return _sessionmaker


@asynccontextmanager
async def session_scope() -> AsyncIterator[AsyncSession]:
    """Транзакционная сессия: commit при успехе, rollback при ошибке."""
    sm = get_sessionmaker()
    async with sm() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


# Лёгкие «миграции» на случай уже существующей БД: create_all не делает ALTER,
# поэтому новые колонки добавляем идемпотентно (PostgreSQL: ADD COLUMN IF NOT EXISTS).
_COLUMN_MIGRATIONS = (
    "ALTER TABLE campaigns ADD COLUMN IF NOT EXISTS max_concurrent INTEGER DEFAULT 0",
    "ALTER TABLE campaigns ADD COLUMN IF NOT EXISTS base_id INTEGER",
    "ALTER TABLE clients ADD COLUMN IF NOT EXISTS duration INTEGER DEFAULT 0",
    "ALTER TABLE clients ADD COLUMN IF NOT EXISTS ended_at DOUBLE PRECISION",
    # Расширяем телефон до 64 символов (раньше 32 — узко для некоторых форматов)
    "ALTER TABLE clients ALTER COLUMN phone TYPE VARCHAR(64)",
)


async def init_db():
    """Создаёт таблицы, если их ещё нет (MVP вместо миграций)."""
    engine = get_engine()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        # ADD COLUMN IF NOT EXISTS — синтаксис PostgreSQL; на свежей БД create_all
        # уже создаёт все колонки, миграции нужны только для существующих БД.
        if engine.dialect.name == "postgresql":
            for stmt in _COLUMN_MIGRATIONS:
                try:
                    await conn.execute(text(stmt))
                except Exception as e:  # noqa: BLE001 — не роняем старт из-за миграции
                    logger.warning(f"init_db migration skipped ({stmt}): {e}")
    logger.info("Database schema ensured")


async def dispose_db():
    global _engine
    if _engine is not None:
        await _engine.dispose()
        _engine = None
