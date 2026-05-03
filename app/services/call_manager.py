"""Call manager: управление звонками, состоянием диалогов, конкурентными сессиями."""

import asyncio
import json
import uuid
import time
from dataclasses import dataclass, field, asdict
from enum import Enum
from pathlib import Path
from loguru import logger
from app.core.config import get_settings


class CallStatus(str, Enum):
    PENDING = "pending"
    RINGING = "ringing"
    ACTIVE = "active"
    ON_HOLD = "on_hold"
    TRANSFERRING = "transferring"
    COMPLETED = "completed"
    FAILED = "failed"


@dataclass
class CallSession:
    """Сессия звонка."""
    call_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    phone_number: str = ""
    scenario_id: str = "default"
    status: CallStatus = CallStatus.PENDING
    current_step: str = "start"
    started_at: float = field(default_factory=time.time)
    ended_at: float | None = None
    transcript: list = field(default_factory=list)
    summary: str = ""
    client_status: str = "unknown"  # interested / not_interested / callback / unknown
    retries: dict = field(default_factory=dict)  # step_id -> retry_count


class CallManager:
    """Менеджер параллельных звонков."""

    def __init__(self):
        self.settings = get_settings()
        self.active_calls: dict[str, CallSession] = {}
        self._lock = asyncio.Lock()

    @property
    def active_count(self) -> int:
        return len([c for c in self.active_calls.values() if c.status == CallStatus.ACTIVE])

    async def can_start_call(self) -> bool:
        """Проверяет, можно ли начать новый звонок."""
        return self.active_count < self.settings.max_concurrent_calls

    async def start_call(self, phone_number: str, scenario_id: str = "default") -> CallSession:
        """Создаёт новую сессию звонка."""
        async with self._lock:
            if not await self.can_start_call():
                raise Exception(
                    f"Max concurrent calls reached ({self.settings.max_concurrent_calls})"
                )

            session = CallSession(
                phone_number=phone_number,
                scenario_id=scenario_id,
                status=CallStatus.ACTIVE,
            )
            self.active_calls[session.call_id] = session
            logger.bind(call=True).info(
                f"Call started: {session.call_id} -> {phone_number} (scenario: {scenario_id})"
            )
            return session

    async def add_to_transcript(self, call_id: str, role: str, text: str):
        """Добавляет реплику в транскрипт."""
        if call_id in self.active_calls:
            self.active_calls[call_id].transcript.append({
                "role": role,  # "robot" or "client"
                "text": text,
                "timestamp": time.time(),
            })

    async def update_step(self, call_id: str, step_id: str):
        """Обновляет текущий шаг сценария."""
        if call_id in self.active_calls:
            self.active_calls[call_id].current_step = step_id

    async def end_call(
        self,
        call_id: str,
        client_status: str = "unknown",
        summary: str = "",
    ) -> CallSession | None:
        """Завершает звонок и сохраняет историю в JSON файл."""
        async with self._lock:
            session = self.active_calls.get(call_id)
            if not session:
                return None

            session.status = CallStatus.COMPLETED
            session.ended_at = time.time()
            session.client_status = client_status
            session.summary = summary

            duration = session.ended_at - session.started_at
            logger.bind(call=True).info(
                f"Call ended: {call_id} | duration={duration:.1f}s | "
                f"status={client_status} | messages={len(session.transcript)}"
            )

            # Сохраняем историю звонка на диск
            self._persist_call(session)
            return session

    def _persist_call(self, session: "CallSession"):
        """Сохраняет завершённый звонок в JSON файл."""
        try:
            history_dir = Path(self.settings.call_history_dir)
            history_dir.mkdir(parents=True, exist_ok=True)
            data = {
                "call_id": session.call_id,
                "phone_number": session.phone_number,
                "scenario_id": session.scenario_id,
                "status": session.status.value,
                "client_status": session.client_status,
                "started_at": session.started_at,
                "ended_at": session.ended_at,
                "duration": int((session.ended_at or 0) - session.started_at),
                "transcript": session.transcript,
                "summary": session.summary,
            }
            path = history_dir / f"{session.call_id}.json"
            with open(path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error(f"Failed to persist call {session.call_id}: {e}")

    async def get_call(self, call_id: str) -> CallSession | None:
        return self.active_calls.get(call_id)

    async def list_active(self) -> list[dict]:
        return [
            {
                "call_id": c.call_id,
                "phone": c.phone_number,
                "status": c.status.value,
                "step": c.current_step,
                "duration": int(time.time() - c.started_at),
                "messages": len(c.transcript),
            }
            for c in self.active_calls.values()
            if c.status == CallStatus.ACTIVE
        ]

    async def list_completed(self) -> list[dict]:
        """Список завершённых звонков из памяти, отсортированных по времени."""
        completed = [
            {
                "call_id": c.call_id,
                "phone": c.phone_number,
                "scenario_id": c.scenario_id,
                "status": c.status.value,
                "client_status": c.client_status,
                "duration": int((c.ended_at or 0) - c.started_at),
                "messages": len(c.transcript),
                "summary": c.summary,
                "started_at": c.started_at,
            }
            for c in self.active_calls.values()
            if c.status == CallStatus.COMPLETED
        ]
        return sorted(completed, key=lambda x: x["started_at"], reverse=True)

    async def get_stats(self) -> dict:
        total = len(self.active_calls)
        active = self.active_count
        completed = len([c for c in self.active_calls.values() if c.status == CallStatus.COMPLETED])
        return {
            "total_calls": total,
            "active_calls": active,
            "completed_calls": completed,
            "max_concurrent": self.settings.max_concurrent_calls,
            "slots_available": self.settings.max_concurrent_calls - active,
        }
