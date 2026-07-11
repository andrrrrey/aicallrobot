"""Модели базы данных: кампании обзвона и клиенты.

Хранятся в PostgreSQL (SQLAlchemy 2.0, async). Заменяют/дополняют JSON-историю
звонков: клиент — это цель обзвона со своим статусом, попытками и результатом.
"""

import time
from enum import Enum

from sqlalchemy import String, Integer, Float, Text, ForeignKey, Boolean
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class ClientStatus(str, Enum):
    PENDING = "pending"        # ещё не звонили
    CALLING = "calling"        # звонок идёт прямо сейчас
    DONE = "done"              # дозвонились, разговор завершён
    NO_ANSWER = "no_answer"    # не ответил
    BUSY = "busy"              # занято
    FAILED = "failed"          # ошибка набора
    CALLBACK = "callback"      # запланирован перезвон


class CampaignStatus(str, Enum):
    DRAFT = "draft"
    RUNNING = "running"
    PAUSED = "paused"
    DONE = "done"


class Campaign(Base):
    __tablename__ = "campaigns"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(255))
    scenario_id: Mapped[str] = mapped_column(String(128), default="default")
    algo_version: Mapped[str] = mapped_column(String(8), default="v2")
    status: Mapped[str] = mapped_column(String(16), default=CampaignStatus.DRAFT.value)
    # Голосовой конфиг TTS (provider/voice/role/speed) — сериализуем как строку JSON
    voice_config: Mapped[str] = mapped_column(Text, default="{}")
    # Окно обзвона (часы, локальные), напр. 9..20; 0..24 = без ограничения
    call_window_start: Mapped[int] = mapped_column(Integer, default=0)
    call_window_end: Mapped[int] = mapped_column(Integer, default=24)
    created_at: Mapped[float] = mapped_column(Float, default=time.time)

    clients: Mapped[list["Client"]] = relationship(
        back_populates="campaign", cascade="all, delete-orphan"
    )


class Client(Base):
    __tablename__ = "clients"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    campaign_id: Mapped[int] = mapped_column(ForeignKey("campaigns.id"), index=True)
    phone: Mapped[str] = mapped_column(String(32), index=True)
    name: Mapped[str] = mapped_column(String(255), default="")
    company: Mapped[str] = mapped_column(String(255), default="")

    status: Mapped[str] = mapped_column(
        String(16), default=ClientStatus.PENDING.value, index=True
    )
    route: Mapped[str] = mapped_column(String(16), default="")  # local / t2 / ...
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    last_attempt_at: Mapped[float | None] = mapped_column(Float, nullable=True)
    next_attempt_at: Mapped[float | None] = mapped_column(Float, nullable=True, index=True)

    call_id: Mapped[str] = mapped_column(String(64), default="")
    asterisk_uniqueid: Mapped[str] = mapped_column(String(64), default="")
    client_status: Mapped[str] = mapped_column(String(32), default="unknown")  # квалификация
    summary: Mapped[str] = mapped_column(Text, default="")
    recording_url: Mapped[str] = mapped_column(Text, default="")

    campaign: Mapped["Campaign"] = relationship(back_populates="clients")
