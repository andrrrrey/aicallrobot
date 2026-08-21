"""Audio pipeline: buffer management, VAD (voice activity detection), interruption handling."""

from collections import deque
from dataclasses import dataclass, field
from loguru import logger

from app.core.config import get_settings
from app.services.text_normalize import normalize_for_tts


@dataclass
class AudioBuffer:
    """Буфер аудиоданных с определением пауз и перебиваний.

    Пауза конца реплики адаптивная: после обычной речи используется
    ``end_pause_sec``, после очень короткой реплики (накоплено меньше
    ``short_utterance_ms`` речи) — более длинная ``end_pause_short_sec``,
    чтобы не обрывать короткие ответы вроде «да…»/«алло…».

    ВАЖНО: длительность паузы считается по объёму пришедшего аудио, а НЕ по
    стенным часам. Кадры из телефонии приходят через очередь и во время
    обработки хода (ASR + классификация) накапливаются, а потом разбираются
    пачкой за миллисекунды. При расчёте по ``time.time()`` пауза в такой пачке
    не детектировалась вовсе — несколько реплик склеивались в одну.
    """

    sample_rate: int = 8000
    silence_threshold: int = 500          # амплитуда тишины
    end_pause_sec: float = 0.6            # пауза после обычной речи (сек)
    end_pause_short_sec: float = 0.9      # пауза после короткой реплики (сек)
    short_utterance_ms: int = 700        # граница «короткой» речи (мс)

    _buffer: bytearray = field(default_factory=bytearray)
    _speech_started: bool = False
    _speech_ms: float = 0.0              # накоплено речи в текущей реплике
    _silence_ms: float = 0.0             # накоплено тишины после конца речи

    def chunk_ms(self, chunk: bytes) -> float:
        """Длительность чанка в мс (16 бит на сэмпл, моно)."""
        return len(chunk) * 1000 / (self.sample_rate * 2)

    def add_chunk(self, chunk: bytes) -> dict:
        """
        Добавляет чанк аудио в буфер и анализирует.

        Returns:
            dict с ключами:
                - has_speech: bool — есть ли речь
                - pause_detected: bool — обнаружена пауза (конец реплики)
                - interrupt_detected: bool — обнаружено перебивание
                - buffer_ms: int — длина буфера в мс
        """
        self._buffer.extend(chunk)

        # Простой VAD по амплитуде (2 байта на сэмпл, little-endian)
        is_voice = self._detect_voice(chunk)
        duration_ms = self.chunk_ms(chunk)

        result = {
            "has_speech": False,
            "pause_detected": False,
            "interrupt_detected": False,
            "buffer_ms": len(self._buffer) * 1000 // (self.sample_rate * 2),
        }

        if is_voice:
            if not self._speech_started:
                self._speech_started = True
                logger.debug("Speech started")
            self._silence_ms = 0.0
            self._speech_ms += duration_ms
            result["has_speech"] = True
        elif self._speech_started:
            self._silence_ms += duration_ms
            # Адаптивный порог: короткие реплики требуют более длинной паузы,
            # чтобы не обрубить их на первом же затишье.
            pause_ms = 1000 * (
                self.end_pause_short_sec
                if self._speech_ms < self.short_utterance_ms
                else self.end_pause_sec
            )
            if self._silence_ms >= pause_ms:
                result["pause_detected"] = True
                self._speech_started = False
                logger.debug(
                    f"Pause detected after {self._silence_ms:.0f}ms silence "
                    f"(speech={self._speech_ms:.0f}ms, pause_thr={pause_ms:.0f}ms)"
                )

        return result

    def mean_amplitude(self, chunk: bytes) -> float:
        """Средняя абсолютная амплитуда чанка (для оценки громкости речи)."""
        if len(chunk) < 2:
            return 0.0
        total = 0
        count = 0
        for i in range(0, len(chunk) - 1, 2):
            total += abs(int.from_bytes(chunk[i:i + 2], byteorder="little", signed=True))
            count += 1
        return total / count if count else 0.0

    def _detect_voice(self, chunk: bytes) -> bool:
        """Простой VAD по среднему абсолютному значению амплитуды."""
        return self.mean_amplitude(chunk) > self.silence_threshold

    def get_audio(self) -> bytes:
        """Возвращает буфер и очищает его."""
        audio = bytes(self._buffer)
        self._buffer.clear()
        self._speech_started = False
        self._speech_ms = 0.0
        self._silence_ms = 0.0
        return audio

    def clear(self):
        """Полностью очищает буфер."""
        self._buffer.clear()
        self._speech_started = False
        self._speech_ms = 0.0
        self._silence_ms = 0.0

    def seed(self, audio: bytes):
        """Кладёт в очищенный буфер начало реплики (перехваченное до barge-in)."""
        self.clear()
        if audio:
            self._buffer.extend(audio)
            self._speech_started = True
            self._speech_ms = self.chunk_ms(audio)

    @property
    def duration_ms(self) -> int:
        return len(self._buffer) * 1000 // (self.sample_rate * 2)

    @property
    def is_empty(self) -> bool:
        return len(self._buffer) == 0

class AudioPipeline:
    """
    Пайплайн обработки аудио в реальном времени.
    Координирует буфер, ASR и TTS.
    """

    def __init__(self, asr_service, tts_service, on_text_recognized=None, on_audio_ready=None,
                 interrupt_threshold_ms: int | None = None):
        settings = get_settings()
        self.asr = asr_service
        self.tts = tts_service
        # Пороги VAD/эндпоинтинга берём из конфига (настраиваются из .env).
        self.buffer = AudioBuffer(
            sample_rate=settings.audio_sample_rate,
            silence_threshold=settings.vad_silence_threshold,
            end_pause_sec=settings.vad_end_pause_sec,
            end_pause_short_sec=settings.vad_end_pause_short_sec,
            short_utterance_ms=settings.vad_short_utterance_ms,
        )
        self.on_text_recognized = on_text_recognized
        self.on_audio_ready = on_audio_ready
        self._is_speaking = False  # робот сейчас говорит
        self._interrupted = False
        # Barge-in: сколько мс непрерывной речи клиента должно накопиться, пока
        # говорит робот, чтобы считать это перебиванием (защита от шума/эха).
        self._interrupt_threshold_ms = (
            interrupt_threshold_ms
            if interrupt_threshold_ms is not None
            else settings.vad_interrupt_duration_ms
        )
        self._interrupt_speech_ms = 0.0
        # Речь должна быть заметно громче порога тишины, чтобы считаться
        # перебиванием: эхо собственной речи робота тише живого голоса.
        self._interrupt_amplitude = (
            settings.vad_silence_threshold * settings.vad_interrupt_gain
        )
        # Кольцевой буфер начала перебивающей реплики: пока робот говорит, входящее
        # аудио в основной буфер не копится (там было бы эхо), но последние
        # interrupt_threshold_ms речи держим здесь — чтобы после подтверждённого
        # barge-in не потерять начало фразы собеседника.
        self._bargein_ring: deque[bytes] = deque()
        self._bargein_ring_ms = 0.0
        # Эхо-хвост: на телефонной линии нет эхоподавления, и остаток собственной
        # речи возвращается к нам уже после того, как робот замолчал. Первые
        # echo_guard_ms входящего аудио после своей реплики отбрасываем — иначе
        # робот распознаёт сам себя и «отвечает сам себе».
        self._echo_guard_ms = float(settings.vad_echo_guard_ms)
        self._echo_guard_left = 0.0
        # Потоковая ASR-сессия текущей реплики (если включён ASR_STREAMING).
        self._asr_session = None
        # Колбэк транспорта: есть ли ещё непроигранный исходящий звук. Робот
        # «говорит», пока очередь воспроизведения не опустела (даже если генерация
        # TTS уже завершилась) — нужно, чтобы barge-in ловился всю реплику.
        self._audio_pending_cb = None

    def _robot_speaking(self) -> bool:
        if self._is_speaking:
            return True
        cb = self._audio_pending_cb
        if cb is not None:
            try:
                return bool(cb())
            except Exception:
                return False
        return False

    async def process_chunk(self, chunk: bytes) -> dict | None:
        """
        Обрабатывает входящий аудиочанк.

        Возвращает:
        * ``{"type": "utterance", "audio": bytes, "asr_session": …}`` — собеседник
          договорил (обнаружена пауза), аудио готово к распознаванию. Само
          распознавание вызывающая сторона запускает отдельно
          (:meth:`recognize_utterance`), чтобы не блокировать приём аудио;
        * ``{"type": "interrupt"}`` — собеседник заговорил поверх робота;
        * ``None`` — ничего не произошло.
        """
        chunk_ms = self.buffer.chunk_ms(chunk)

        # --- Робот говорит: входящее аудио — это почти наверняка наше эхо ---
        # «Робот говорит» = идёт генерация TTS ИЛИ ещё не проигран исходящий звук.
        if self._robot_speaking():
            # Пока робот говорит, потоковую сессию не ведём — закрываем, если была.
            await self._close_asr_session()
            # Взводим эхо-хвост: он отработает сразу после того, как робот замолчит.
            self._echo_guard_left = self._echo_guard_ms
            # Ничего не копим в основном буфере: иначе эхо собственной речи уйдёт
            # в ASR и робот обработает свою же реплику как слова собеседника.
            self.buffer.clear()

            loud = self.buffer.mean_amplitude(chunk) > self._interrupt_amplitude
            if loud:
                self._interrupt_speech_ms += chunk_ms
                self._ring_push(chunk, chunk_ms)
            else:
                self._interrupt_speech_ms = 0.0
                self._ring_clear()

            if self._interrupt_speech_ms >= self._interrupt_threshold_ms:
                self._interrupted = True
                self._interrupt_speech_ms = 0.0
                # Начало перебивающей реплики не теряем — кладём его в буфер.
                self.buffer.seed(self._ring_bytes())
                self._ring_clear()
                self._echo_guard_left = 0.0
                logger.info("Interruption detected — client is speaking over the robot")
                return {"type": "interrupt"}
            # Пока робот говорит — реплику не распознаём (ждём паузы/перебивания)
            return None

        # --- Робот молчит ---
        self._interrupt_speech_ms = 0.0
        self._ring_clear()

        # Эхо-хвост после собственной реплики — выбрасываем.
        if self._echo_guard_left > 0:
            self._echo_guard_left -= chunk_ms
            self.buffer.clear()
            return None

        result = self.buffer.add_chunk(chunk)

        # Потоковый ASR: стримим аудио во время речи, чтобы к паузе текст был готов.
        await self._feed_streaming(chunk, result)

        # По паузе — конец реплики: отдаём накопленное аудио вызывающей стороне.
        if result["pause_detected"] and not self.buffer.is_empty:
            audio_data = self.buffer.get_audio()
            if len(audio_data) > 1600:  # минимум 100ms аудио
                session, self._asr_session = self._asr_session, None
                return {"type": "utterance", "audio": audio_data, "asr_session": session}
            # Слишком короткий фрагмент — закрываем сессию без распознавания.
            await self._close_asr_session()

        return None

    def _ring_push(self, chunk: bytes, chunk_ms: float):
        """Кладёт чанк в кольцо начала перебивающей реплики (не длиннее порога)."""
        self._bargein_ring.append(chunk)
        self._bargein_ring_ms += chunk_ms
        while self._bargein_ring_ms > self._interrupt_threshold_ms * 2 and self._bargein_ring:
            dropped = self._bargein_ring.popleft()
            self._bargein_ring_ms -= self.buffer.chunk_ms(dropped)

    def _ring_clear(self):
        self._bargein_ring.clear()
        self._bargein_ring_ms = 0.0

    def _ring_bytes(self) -> bytes:
        return b"".join(self._bargein_ring)

    async def recognize_utterance(self, audio: bytes, asr_session=None) -> str:
        """Распознаёт реплику, отданную :meth:`process_chunk`.

        Вынесено из ``process_chunk``, чтобы приём аудио не простаивал на время
        похода в ASR (иначе кадры собеседника копятся в очереди транспорта и
        теряются, а VAD разбирает их пачкой уже с неверными таймингами).
        """
        try:
            text = await self._recognize(audio, asr_session)
        except Exception as e:
            logger.error(f"ASR error: {e}")
            await self._close_session(asr_session)
            return ""
        if text and self.on_text_recognized:
            await self.on_text_recognized(text)
        return text

    def arm_echo_guard(self):
        """Взводит эхо-хвост вручную (после блокирующей реплики робота)."""
        self._echo_guard_left = self._echo_guard_ms
        self.buffer.clear()

    async def _feed_streaming(self, chunk: bytes, result: dict):
        """Открывает потоковую сессию на старте речи и кормит её аудио-чанками."""
        if self._asr_session is None:
            if not result["has_speech"]:
                return  # ждём начала речи, сессию зря не открываем
            start_stream = getattr(self.asr, "start_stream", None)
            if start_stream is None:
                return  # ASR без потокового режима — распознаем на паузе через REST
            session = await start_stream()
            if session is None:
                return  # стриминг выключен/недоступен — пойдём через REST на паузе
            self._asr_session = session
            # «Досылаем» уже накопленный буфер реплики (включая текущий чанк).
            self._asr_session.feed(bytes(self.buffer._buffer))
        else:
            self._asr_session.feed(chunk)

    async def _recognize(self, audio_data: bytes, session=None) -> str:
        """Распознавание на паузе: потоковый финал, иначе фолбэк на REST v1."""
        if session is None:
            session, self._asr_session = self._asr_session, None
        if session is not None:
            try:
                text = await session.finish()
                if text:
                    logger.info(f"ASR (streaming) result: '{text}'")
                    return text
                logger.info("ASR (streaming) пусто — фолбэк на REST")
            except Exception as e:
                logger.warning(f"Streaming ASR finish failed, REST fallback: {e}")
        return await self.asr.recognize_short(audio_data)

    async def _close_asr_session(self):
        """Закрывает активную потоковую сессию без ожидания финала."""
        session, self._asr_session = self._asr_session, None
        await self._close_session(session)

    @staticmethod
    async def _close_session(session):
        if session is None:
            return
        try:
            await session.cancel()
        except Exception:
            pass

    async def speak(self, text: str) -> bytes:
        """Синтезирует речь и помечает, что робот говорит."""
        self._is_speaking = True
        self._interrupted = False
        try:
            audio = await self.tts.synthesize(normalize_for_tts(text))
            if self.on_audio_ready:
                await self.on_audio_ready(audio)
            return audio
        finally:
            self._is_speaking = False

    @property
    def was_interrupted(self) -> bool:
        return self._interrupted

    def reset_interrupt(self):
        self._interrupted = False
