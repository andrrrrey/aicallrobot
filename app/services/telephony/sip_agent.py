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
    from pyVoIP.VoIP import VoIPPhone, VoIPCall, CallState, InvalidStateError, PhoneStatus
    PYVOIP_AVAILABLE = True
except Exception as e:  # pragma: no cover - зависит от окружения
    PYVOIP_AVAILABLE = False
    logger.warning(f"pyVoIP недоступен — SIP-агент отключён: {e}")

# Параметры аудио
_FRAME_BYTES = 320          # 20 мс при 8 кГц/16 бит моно
_ANSWER_TIMEOUT = 60.0      # ждать ответа абонента, сек
_MAX_CALL_SECONDS = 600.0   # аварийный лимит длительности звонка

# Watchdog: восстановление после сбоя pyVoIP (recv-поток умирает по Bad fd)
_WATCHDOG_INTERVAL = 20.0   # период проверки живости, сек
_RESTART_MIN_GAP = 30.0     # минимум между перезапусками, сек
_RECV_THREAD_NAME = "SIP Recieve"  # имя потока приёма в pyVoIP (sic)


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
        self._phone_kwargs: dict | None = None
        self._active = 0                          # активных звонков (не рестартим во время звонка)
        self._active_lock = threading.Lock()
        self._restart_lock = threading.Lock()
        self._last_restart = 0.0
        self._stop_flag = threading.Event()
        self._watchdog_thread: threading.Thread | None = None

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
        self._phone_kwargs = dict(
            server=settings.sip_server,
            port=5060,
            username=settings.sip_extension,
            password=settings.sip_password,
            callCallback=self._on_incoming_call,
        )
        if settings.sip_local_ip:
            self._phone_kwargs["myIP"] = settings.sip_local_ip

        if not self._build_and_start_phone():
            return
        self._started = True

        # Проверяем фактическую регистрацию в отдельном потоке (не блокируя startup)
        ext, srv, myip = settings.sip_extension, settings.sip_server, settings.sip_local_ip or "auto"
        threading.Thread(
            target=self._report_registration, args=(ext, srv, myip), daemon=True
        ).start()

        # Watchdog: автоперезапуск при сбое pyVoIP (recv-поток умирает по Bad fd)
        self._stop_flag.clear()
        self._watchdog_thread = threading.Thread(target=self._watchdog_loop, daemon=True)
        self._watchdog_thread.start()

    def _build_and_start_phone(self) -> bool:
        """Создаёт и запускает VoIPPhone из сохранённых параметров."""
        try:
            self._phone = VoIPPhone(**self._phone_kwargs)
            self._phone.start()
            return True
        except Exception as e:
            myip = (self._phone_kwargs or {}).get("myIP", "auto")
            logger.error(
                f"SIP-агент: ошибка запуска pyVoIP (bind {myip}:5060). "
                f"Проверьте, что контейнер в host-режиме и адрес ppp0 = SIP_LOCAL_IP. Детали: {e}"
            )
            self._phone = None
            return False

    def _recv_thread_alive(self) -> bool:
        """Жив ли внутренний поток приёма SIP в pyVoIP."""
        return any(
            t.name == _RECV_THREAD_NAME and t.is_alive() for t in threading.enumerate()
        )

    def _watchdog_loop(self):
        """Следит за живостью SIP и пересоздаёт телефон при сбое (вне звонка)."""
        while not self._stop_flag.wait(_WATCHDOG_INTERVAL):
            if self._phone is None:
                continue  # агент намеренно остановлен/не сконфигурирован
            with self._active_lock:
                busy = self._active > 0
            if busy:
                continue  # не трогаем во время активного звонка
            try:
                status = self._phone.get_status()
            except Exception:
                status = None
            dead = (not self._recv_thread_alive()) or status != PhoneStatus.REGISTERED
            if dead:
                logger.warning(
                    f"SIP-агент: обнаружен сбой (recv_alive={self._recv_thread_alive()}, "
                    f"status={getattr(status, 'name', status)}) — перезапуск pyVoIP"
                )
                self._restart_phone()

    def _restart_phone(self):
        """Пересоздаёт VoIPPhone (с защитой от частых перезапусков)."""
        with self._restart_lock:
            if time.time() - self._last_restart < _RESTART_MIN_GAP:
                return
            self._last_restart = time.time()
            try:
                if self._phone is not None:
                    self._phone.stop()
            except Exception:
                pass
            if self._build_and_start_phone():
                logger.info("SIP-агент: pyVoIP перезапущен")
            else:
                logger.error("SIP-агент: перезапуск pyVoIP не удался")

    def _report_registration(self, ext: str, srv: str, myip: str, timeout: float = 12.0):
        """Ждёт фактической регистрации и логирует реальный статус pyVoIP."""
        deadline = time.time() + timeout
        status = None
        while time.time() < deadline:
            try:
                status = self._phone.get_status()
            except Exception:
                break
            if status not in (PhoneStatus.INACTIVE, PhoneStatus.REGISTERING):
                break
            time.sleep(0.5)

        if status == PhoneStatus.REGISTERED:
            logger.info(f"SIP-агент зарегистрирован: {ext}@{srv} (myIP={myip})")
        else:
            name = status.name if status is not None else "нет ответа"
            logger.error(
                f"SIP-агент НЕ зарегистрирован (статус={name}). Проверьте: "
                f"(1) контейнер в host-режиме сети и виден ppp0; "
                f"(2) SIP_LOCAL_IP={myip} совпадает с адресом ppp0; "
                f"(3) доступность {srv}:5060/UDP по туннелю (tcpdump на ppp0); "
                f"(4) логин/пароль экстеншена."
            )
            # Останавливаем pyVoIP, чтобы не крутился падающий поток приёма
            try:
                self._phone.stop()
            except Exception:
                pass
            self._phone = None
            self._started = False

    def stop(self):
        self._stop_flag.set()  # останавливаем watchdog, чтобы он не перезапускал телефон
        if self._phone is not None:
            try:
                self._phone.stop()
            except Exception as e:
                logger.warning(f"Ошибка остановки SIP-агента: {e}")
        self._phone = None
        self._started = False

    @property
    def ready(self) -> bool:
        """Готов ли агент принимать/совершать звонки (фактически зарегистрирован)."""
        if not self._started or self._phone is None:
            return False
        try:
            return self._phone.get_status() == PhoneStatus.REGISTERED
        except Exception:
            return False

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

        with self._active_lock:
            self._active += 1

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
            with self._active_lock:
                self._active = max(0, self._active - 1)
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
