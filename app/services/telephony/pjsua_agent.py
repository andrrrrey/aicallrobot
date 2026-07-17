"""SIP-агент на pjsua2 (PJSIP) — устойчивый бэкенд телефонии.

В отличие от pyVoIP, pjsua2 штатно отвечает на OPTIONS (qualify), корректно держит
перерегистрацию, RTP и кодеки — поэтому это целевой продакшн-вариант для работы с
реальным Asterisk заказчика.

Модель реального времени:
* медиа-callback'и pjsua2 (``onFrameReceived``/``onFrameRequested``) вызываются в
  потоке движка каждые 20 мс и должны быть **быстрыми** — они только кладут/берут
  PCM-кадры из потокобезопасных очередей;
* тяжёлая обработка (ASR → движок диалога → TTS) крутится в отдельном рабочем
  потоке на звонок и общается с asyncio-циклом через ``run_coroutine_threadsafe``.

Формат аудио порта — PCM 8 кГц, 16 бит, моно (совпадает с ``AudioPipeline``);
кодек G.711 alaw pjsua2 согласует и декодирует сам.

Интерфейс (``start/stop/ready/originate`` + ``CallResult``) совпадает с pyVoIP-
агентом, поэтому ``ConversationDriver``, диалер и роуты не меняются.
"""

from __future__ import annotations

import asyncio
import queue
import threading
import time

from loguru import logger

from app.core.config import get_settings
from app.services.conversation import ConversationDriver
from app.services.telephony.base import CallResult

try:
    import pjsua2 as pj
    PJSUA2_AVAILABLE = True
except Exception as e:  # pragma: no cover - зависит от сборки образа
    PJSUA2_AVAILABLE = False
    logger.warning(f"pjsua2 недоступен — pjsua-бэкенд отключён: {e}")

# Параметры аудио (20 мс при 8 кГц/16 бит/моно = 320 байт)
_CLOCK_RATE = 8000
_CHANNELS = 1
_BITS = 16
_FRAME_USEC = 20000
_FRAME_BYTES = _CLOCK_RATE * _CHANNELS * (_BITS // 8) * _FRAME_USEC // 1_000_000  # 320
_ANSWER_TIMEOUT = 60.0
_MAX_CALL_SECONDS = 600.0
_IN_QUEUE_MAX = 200   # ~4 c аудио; защита от разрастания при долгой обработке


def _to_bytes(buf) -> bytes:
    """MediaFrame.buf (ByteVector/bytes) → bytes."""
    try:
        return bytes(buf)
    except Exception:
        return b""


def _to_bufvector(data: bytes):
    """bytes → pj.ByteVector для MediaFrame.buf."""
    return pj.ByteVector(data)


if PJSUA2_AVAILABLE:

    class _AudioBridgePort(pj.AudioMediaPort):
        """Кастомный аудиопорт: мост между RTP-звонком и очередями PCM."""

        def __init__(self, in_queue: "queue.Queue[bytes]", out_queue: "queue.Queue[bytes]"):
            super().__init__()
            self._in = in_queue
            self._out = out_queue
            self._residual = b""

        def onFrameReceived(self, frame):
            # Звук абонента (PCM) → очередь на распознавание
            try:
                data = _to_bytes(frame.buf)
                if data:
                    if self._in.full():
                        try:
                            self._in.get_nowait()  # дропаем самый старый кадр
                        except queue.Empty:
                            pass
                    self._in.put_nowait(data)
            except Exception:
                pass

        def onFrameRequested(self, frame):
            # Отдаём TTS абоненту кадрами по _FRAME_BYTES; если нечего — тишина
            try:
                while len(self._residual) < _FRAME_BYTES:
                    try:
                        self._residual += self._out.get_nowait()
                    except queue.Empty:
                        break
                if len(self._residual) >= _FRAME_BYTES:
                    chunk = self._residual[:_FRAME_BYTES]
                    self._residual = self._residual[_FRAME_BYTES:]
                else:
                    chunk = self._residual + b"\x00" * (_FRAME_BYTES - len(self._residual))
                    self._residual = b""
                frame.type = pj.PJMEDIA_FRAME_TYPE_AUDIO
                frame.buf = _to_bufvector(chunk)
                frame.size = len(chunk)
            except Exception:
                pass

    class _Call(pj.Call):
        """Один звонок pjsua2 + сигналы состояния."""

        def __init__(self, acc, agent: "PjsuaAgent", call_id: str):
            super().__init__(acc)
            self.agent = agent
            self.call_id = call_id
            self.answered = threading.Event()
            self.disconnected = threading.Event()
            self.port: "_AudioBridgePort | None" = None
            self.in_queue: "queue.Queue[bytes]" = queue.Queue(maxsize=_IN_QUEUE_MAX)
            self.out_queue: "queue.Queue[bytes]" = queue.Queue()
            self._audmed = None

        def onCallState(self, prm):
            try:
                ci = self.getInfo()
            except Exception:
                return
            state = ci.state
            if state == pj.PJSIP_INV_STATE_CONFIRMED:
                self.answered.set()
            elif state == pj.PJSIP_INV_STATE_DISCONNECTED:
                self.answered.set()      # разблокировать ожидание
                self.disconnected.set()

        def onCallMediaState(self, prm):
            try:
                ci = self.getInfo()
            except Exception:
                return
            for i, mi in enumerate(ci.media):
                if mi.type == pj.PJMEDIA_TYPE_AUDIO and mi.status == pj.PJSUA_CALL_MEDIA_ACTIVE:
                    try:
                        am = pj.AudioMedia.typecastFromMedia(self.getMedia(i))
                    except Exception:
                        continue
                    if self.port is None:
                        self.port = _AudioBridgePort(self.in_queue, self.out_queue)
                        fmt = pj.MediaFormatAudio()
                        fmt.type = pj.PJMEDIA_TYPE_AUDIO
                        fmt.clockRate = _CLOCK_RATE
                        fmt.channelCount = _CHANNELS
                        fmt.bitsPerSample = _BITS
                        fmt.frameTimeUsec = _FRAME_USEC
                        self.port.createPort("aibridge", fmt)
                    self._audmed = am
                    # remote → наш порт (onFrameReceived); наш порт → remote (onFrameRequested)
                    am.startTransmit(self.port)
                    self.port.startTransmit(am)

    class _Account(pj.Account):
        def __init__(self, agent: "PjsuaAgent"):
            super().__init__()
            self.agent = agent

        def onRegState(self, prm):
            try:
                self.agent._registered = (prm.code // 100 == 2) and self.getInfo().regIsActive
            except Exception:
                self.agent._registered = False
            logger.info(f"pjsua2 регистрация: code={getattr(prm, 'code', '?')} "
                        f"reg_active={self.agent._registered}")


class PjsuaAgent:
    """SIP-агент на pjsua2 (интерфейс совместим с pyVoIP-агентом)."""

    def __init__(self):
        self._ep = None
        self._acc = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._started = False
        self._registered = False
        self._active = 0
        self._active_lock = threading.Lock()

    # --- Жизненный цикл ---

    def start(self, loop: asyncio.AbstractEventLoop):
        if not PJSUA2_AVAILABLE:
            logger.warning("pjsua-агент не запущен: pjsua2 недоступен в образе")
            return
        settings = get_settings()
        if not settings.sip_server or not settings.sip_extension:
            logger.info("pjsua-агент не сконфигурирован (sip_server/sip_extension пусты) — пропуск")
            return
        self._loop = loop
        try:
            ep = pj.Endpoint()
            ep.libCreate()
            ep_cfg = pj.EpConfig()
            ep_cfg.logConfig.level = 4 if settings.sip_debug else 2
            ep_cfg.uaConfig.threadCnt = 1
            ep.libInit(ep_cfg)

            tcfg = pj.TransportConfig()
            tcfg.port = 5060
            if settings.sip_local_ip:
                tcfg.boundAddress = settings.sip_local_ip
                tcfg.publicAddress = settings.sip_local_ip
            ep.transportCreate(pj.PJSIP_TRANSPORT_UDP, tcfg)
            ep.libStart()
            # Контейнер без звуковой карты — используем null-устройство,
            # звук ходит только через кастомный AudioMediaPort.
            try:
                ep.audDevManager().setNullDev()
            except Exception:
                pass
            self._ep = ep

            acc_cfg = pj.AccountConfig()
            acc_cfg.idUri = f"sip:{settings.sip_extension}@{settings.sip_server}"
            acc_cfg.regConfig.registrarUri = f"sip:{settings.sip_server}"
            cred = pj.AuthCredInfo("digest", "*", settings.sip_extension, 0, settings.sip_password)
            acc_cfg.sipConfig.authCreds.append(cred)
            self._acc = _Account(self)
            self._acc.create(acc_cfg)
            self._started = True
            logger.info(
                f"pjsua-агент запущен: {settings.sip_extension}@{settings.sip_server} "
                f"(myIP={settings.sip_local_ip or 'auto'}), ждём регистрацию…"
            )
        except Exception as e:
            logger.error(f"pjsua-агент: ошибка инициализации: {e}")
            self._started = False

    def stop(self):
        self._started = False
        self._registered = False
        try:
            if self._ep is not None:
                self._ep.libDestroy()
        except Exception as e:
            logger.warning(f"pjsua-агент: ошибка остановки: {e}")
        self._ep = None
        self._acc = None

    @property
    def ready(self) -> bool:
        return self._started and self._registered

    def _register_thread(self):
        """Регистрирует текущий (внешний) поток в pjsua2, если нужно."""
        try:
            if not self._ep.libIsThreadRegistered():
                self._ep.libRegisterThread(threading.current_thread().name)
        except Exception:
            pass

    # --- Исходящий звонок ---

    async def originate(self, call_id: str, session, scenario, number: str,
                        greeting: str = "", voice_config: dict | None = None) -> CallResult:
        if not self.ready:
            return CallResult(status="failed")
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            None, self._place_and_run, call_id, session, scenario, number, greeting,
            voice_config or {},
        )

    def _place_and_run(self, call_id, session, scenario, number, greeting, voice_config) -> CallResult:
        self._register_thread()
        call = _Call(self._acc, self, call_id)
        dst = f"sip:{number}@{get_settings().sip_server}"
        try:
            call.makeCall(dst, pj.CallOpParam(True))
        except Exception as e:
            logger.error(f"pjsua makeCall failed for {number}: {e}")
            return CallResult(status="failed")

        # Ждём ответа абонента (CONFIRMED) либо разъединения
        if not call.answered.wait(timeout=_ANSWER_TIMEOUT) or call.disconnected.is_set():
            status = "busy" if call.disconnected.is_set() else "no_answer"
            self._safe_hangup(call)
            logger.info(f"Call {call_id} not answered → {status}")
            return CallResult(status=status)

        logger.info(f"Call {call_id} answered → ведём разговор")
        return self._run_conversation(call_id, session, scenario, call, greeting, voice_config)

    # --- Общий цикл разговора ---

    def _run_conversation(self, call_id, session, scenario, call, greeting, voice_config) -> CallResult:
        loop = self._loop
        started = time.time()
        with self._active_lock:
            self._active += 1

        async def send_audio(chunk: bytes):
            call.out_queue.put(chunk)

        driver = ConversationDriver(
            call_id=call_id, session=session, scenario=scenario, send_audio=send_audio,
        )
        if voice_config:
            driver.set_tts_config(voice_config)

        # Ждём, пока установится медиа-порт (onCallMediaState)
        waited = 0.0
        while call.port is None and waited < 5.0 and not call.disconnected.is_set():
            time.sleep(0.1)
            waited += 0.1

        if greeting:
            self._await(loop, driver.speak(greeting))

        try:
            while True:
                if call.disconnected.is_set() or driver.should_end:
                    break
                if time.time() - started > _MAX_CALL_SECONDS:
                    logger.warning(f"Call {call_id} превысил лимит длительности")
                    break
                try:
                    chunk = call.in_queue.get(timeout=0.5)
                except queue.Empty:
                    continue
                self._await(loop, driver.feed_chunk(chunk))
        finally:
            status, summary = "unknown", ""
            try:
                status, summary = self._await(loop, driver.finalize())
            except Exception as e:
                logger.error(f"finalize failed for {call_id}: {e}")
            self._safe_hangup(call)
            with self._active_lock:
                self._active = max(0, self._active - 1)
            return CallResult(
                status="answered", client_status=status, summary=summary,
                duration=time.time() - started,
            )

    def _safe_hangup(self, call):
        try:
            call.hangup(pj.CallOpParam(True))
        except Exception:
            pass

    @staticmethod
    def _await(loop: asyncio.AbstractEventLoop, coro):
        fut = asyncio.run_coroutine_threadsafe(coro, loop)
        return fut.result()


# Синглтон агента
pjsua_agent = PjsuaAgent()
