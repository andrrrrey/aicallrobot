"""SIP-агент: робот как внутренний абонент (экстеншен) Asterisk.

Робот регистрируется на АТС заказчика по SIP (через VPN-туннель) и участвует в
разговоре как обычный телефон — это единственный совместимый с Asterisk 13
способ получить звук разговора в реальном времени (AudioSocket/externalMedia там
недоступны). Кодек — G.711 alaw; pyVoIP декодирует его в PCM 8 кГц 16 бит моно,
что **ровно** совпадает с форматом ``AudioPipeline``.

Библиотека pyVoIP синхронная и работает в своих потоках, а диалоговый движок —
асинхронный. Мост между ними: аудио-цикл звонка крутится в отдельном потоке и
планирует корутины ``ConversationDriver`` в общий event loop через
``run_coroutine_threadsafe``. Синтезированный TTS складывается в потокобезопасную
очередь, которую тот же поток отдаёт в ``call.write_audio``.

Основной рабочий путь — **исходящий набор** (робот сам звонит номеру; чистый
жизненный цикл звонка от pyVoIP). Для сценария click-to-call через ``res24.php``
предусмотрен авто-ответ на входящий вызов (может потребовать подстройки под
реальную маршрутизацию АТС — см. план, открытые вопросы).
"""

from __future__ import annotations

import asyncio
import queue
import threading
import time
from dataclasses import dataclass, field

from loguru import logger

from app.core.config import get_settings
from app.services import registry
from app.services.conversation import ConversationDriver

try:
    from pyVoIP.VoIP import VoIPPhone, VoIPCall, CallState, InvalidStateError
    PYVOIP_AVAILABLE = True
except Exception as e:  # pragma: no cover - зависит от окружения
    PYVOIP_AVAILABLE = False
    logger.warning(f"pyVoIP недоступен — SIP-агент отключён: {e}")

# Параметры аудио
_FRAME_BYTES = 320          # 20 мс при 8 кГц/16 бит моно
_ANSWER_TIMEOUT = 60.0      # ждать ответа абонента, сек
_MAX_CALL_SECONDS = 600.0   # аварийный лимит длительности звонка


@dataclass
class CallResult:
    status: str                       # answered / no_answer / busy / failed
    client_status: str = "unknown"    # квалификация после разговора
    summary: str = ""
    duration: float = 0.0


@dataclass
class _CallCtx:
    call_id: str
    driver: ConversationDriver
    out_queue: "queue.Queue[bytes]" = field(default_factory=queue.Queue)
    ended: threading.Event = field(default_factory=threading.Event)


class SipAgent:
    """Управляет SIP-регистрацией и проведением звонков."""

    def __init__(self):
        self._phone: "VoIPPhone | None" = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._started = False

    # --- Жизненный цикл ---

    def start(self, loop: asyncio.AbstractEventLoop):
        """Регистрирует робота на АТС. Вызывать один раз при старте приложения."""
        if not PYVOIP_AVAILABLE:
            logger.warning("SIP-агент не запущен: pyVoIP недоступен")
            return
        settings = get_settings()
        if not settings.sip_server or not settings.sip_extension:
            logger.info("SIP-агент не сконфигурирован (sip_server/sip_extension пусты) — пропуск")
            return

        self._loop = loop
        self._phone = VoIPPhone(
            server=settings.sip_server,
            port=5060,
            username=settings.sip_extension,
            password=settings.sip_password,
            callCallback=self._on_incoming_call,
        )
        self._phone.start()
        self._started = True
        logger.info(
            f"SIP-агент зарегистрирован: {settings.sip_extension}@{settings.sip_server}"
        )

    def stop(self):
        if self._phone is not None:
            try:
                self._phone.stop()
            except Exception as e:
                logger.warning(f"Ошибка остановки SIP-агента: {e}")
        self._started = False

    @property
    def ready(self) -> bool:
        return self._started

    # --- Исходящий звонок (основной путь) ---

    async def originate(self, call_id: str, session, scenario, number: str,
                        greeting: str = "", voice_config: dict | None = None) -> CallResult:
        """Набирает ``number`` и проводит разговор. Блокирующая часть — в потоке."""
        if not self._started or self._phone is None:
            return CallResult(status="failed")
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            None, self._place_and_run, call_id, session, scenario, number, greeting,
            voice_config or {},
        )

    def _place_and_run(self, call_id, session, scenario, number, greeting, voice_config) -> CallResult:
        """Синхронный жизненный цикл исходящего звонка (выполняется в потоке)."""
        assert self._phone is not None
        try:
            call = self._phone.call(number)
        except Exception as e:
            logger.error(f"SIP originate failed for {number}: {e}")
            return CallResult(status="failed")

        # Ждём ответа абонента
        waited = 0.0
        while call.state == CallState.RINGING and waited < _ANSWER_TIMEOUT:
            time.sleep(0.2)
            waited += 0.2
        if call.state != CallState.ANSWERED:
            state = call.state
            try:
                call.hangup()
            except Exception:
                pass
            # RINGING по таймауту → не ответил; ENDED сразу → занято/сброс
            status = "no_answer" if state == CallState.RINGING else "busy"
            logger.info(f"Call {call_id} not answered (state={state}) → {status}")
            return CallResult(status=status)

        logger.info(f"Call {call_id} answered → ведём разговор")
        return self._run_conversation(call_id, session, scenario, call, greeting, voice_config)

    # --- Входящий звонок (click-to-call через res24.php) ---

    def _on_incoming_call(self, call: "VoIPCall"):
        """Авто-ответ на входящий вызов (робот вызван как экстеншен из res24 call)."""
        ctx = _pop_pending()
        if ctx is None:
            logger.warning("Входящий SIP-вызов без ожидающего контекста — отклоняю")
            try:
                call.hangup()
            except Exception:
                pass
            return
        try:
            call.answer()
        except InvalidStateError:
            return
        logger.info(f"Входящий вызов принят для call_id={ctx['call_id']}")
        self._run_conversation(
            ctx["call_id"], ctx["session"], ctx["scenario"], call,
            ctx.get("greeting", ""), ctx.get("voice_config", {}),
        )

    # --- Общий цикл разговора ---

    def _run_conversation(self, call_id, session, scenario, call, greeting, voice_config) -> CallResult:
        loop = self._loop
        assert loop is not None
        started = time.time()

        ctx = _CallCtx(call_id=call_id, driver=None)  # type: ignore

        async def send_audio(chunk: bytes):
            ctx.out_queue.put(chunk)

        driver = ConversationDriver(
            call_id=call_id, session=session, scenario=scenario, send_audio=send_audio,
        )
        if voice_config:
            driver.set_tts_config(voice_config)
        ctx.driver = driver

        # Приветствие (после реального ответа абонента)
        if greeting:
            self._await(loop, driver.speak(greeting))

        try:
            while True:
                if call.state == CallState.ENDED or driver.should_end:
                    break
                if time.time() - started > _MAX_CALL_SECONDS:
                    logger.warning(f"Call {call_id} превысил лимит длительности")
                    break

                # Читаем входящее аудио абонента (PCM 8кГц) и отдаём в пайплайн
                try:
                    frame = call.read_audio(_FRAME_BYTES, blocking=True)
                except (InvalidStateError, Exception):
                    break
                if frame:
                    self._await(loop, driver.feed_chunk(frame))

                # Отдаём накопленный TTS абоненту
                self._drain_tts(ctx, call)
        finally:
            # Финализация: саммари/квалификация
            status, summary = "unknown", ""
            try:
                status, summary = self._await(loop, driver.finalize())
            except Exception as e:
                logger.error(f"finalize failed for {call_id}: {e}")
            try:
                if call.state != CallState.ENDED:
                    call.hangup()
            except Exception:
                pass
            return CallResult(
                status="answered", client_status=status, summary=summary,
                duration=time.time() - started,
            )

    def _drain_tts(self, ctx: _CallCtx, call: "VoIPCall"):
        """Пишет накопленные TTS-чанки в исходящий RTP."""
        while True:
            try:
                chunk = ctx.out_queue.get_nowait()
            except queue.Empty:
                break
            try:
                call.write_audio(chunk)
            except Exception as e:
                logger.warning(f"write_audio failed: {e}")
                break

    @staticmethod
    def _await(loop: asyncio.AbstractEventLoop, coro):
        """Выполняет корутину в общем event loop из рабочего потока и ждёт результат."""
        fut = asyncio.run_coroutine_threadsafe(coro, loop)
        return fut.result()


# === Реестр ожидающих контекстов для входящих (click-to-call) ===
# res24 call инициирует входящий вызов на наш экстеншен; сопоставляем его с
# заранее подготовленным контекстом звонка (FIFO). Требует одиночного темпа
# набора при click-to-call; подстроить под реальную маршрутизацию при тестах.

_pending_lock = threading.Lock()
_pending: list[dict] = []


def push_pending(call_id: str, session, scenario, greeting: str = ""):
    with _pending_lock:
        _pending.append({
            "call_id": call_id, "session": session, "scenario": scenario,
            "greeting": greeting, "ts": time.time(),
        })


def _pop_pending() -> dict | None:
    with _pending_lock:
        return _pending.pop(0) if _pending else None


# Синглтон агента
sip_agent = SipAgent()
