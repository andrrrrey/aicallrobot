"""Audio pipeline: buffer management, VAD (voice activity detection), interruption handling."""

import asyncio
import time
from dataclasses import dataclass, field
from loguru import logger

from app.core.config import get_settings


@dataclass
class AudioBuffer:
    """Буфер аудиоданных с определением пауз и перебиваний.

    Пауза конца реплики адаптивная: после обычной речи используется
    ``end_pause_sec``, после очень короткой реплики (накоплено меньше
    ``short_utterance_ms`` речи) — более длинная ``end_pause_short_sec``,
    чтобы не обрывать короткие ответы вроде «да…»/«алло…».
    """

    sample_rate: int = 8000
    silence_threshold: int = 500          # амплитуда тишины
    end_pause_sec: float = 0.6            # пауза после обычной речи (сек)
    end_pause_short_sec: float = 0.9      # пауза после короткой реплики (сек)
    short_utterance_ms: int = 700        # граница «короткой» речи (мс)
    interrupt_duration: float = 0.15      # порог для перебивания (сек)

    _buffer: bytearray = field(default_factory=bytearray)
    _last_voice_time: float = 0.0
    _speech_started: bool = False
    _speech_ms: float = 0.0              # накоплено речи в текущей реплике

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
        now = time.time()

        # Простой VAD по амплитуде (2 байта на сэмпл, little-endian)
        is_voice = self._detect_voice(chunk)

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
            self._last_voice_time = now
            self._speech_ms += len(chunk) * 1000 / (self.sample_rate * 2)
            result["has_speech"] = True
        elif self._speech_started:
            silence_duration = now - self._last_voice_time
            # Адаптивный порог: короткие реплики требуют более длинной паузы,
            # чтобы не обрубить их на первом же затишье.
            pause = (
                self.end_pause_short_sec
                if self._speech_ms < self.short_utterance_ms
                else self.end_pause_sec
            )
            if silence_duration >= pause:
                result["pause_detected"] = True
                self._speech_started = False
                logger.debug(
                    f"Pause detected after {silence_duration:.2f}s silence "
                    f"(speech={self._speech_ms:.0f}ms, pause_thr={pause:.2f}s)"
                )

        return result

    def _detect_voice(self, chunk: bytes) -> bool:
        """Простой VAD по среднему абсолютному значению амплитуды."""
        if len(chunk) < 2:
            return False
        samples = []
        for i in range(0, len(chunk) - 1, 2):
            sample = int.from_bytes(chunk[i:i+2], byteorder="little", signed=True)
            samples.append(abs(sample))
        avg_amplitude = sum(samples) / len(samples) if samples else 0
        return avg_amplitude > self.silence_threshold

    def get_audio(self) -> bytes:
        """Возвращает буфер и очищает его."""
        audio = bytes(self._buffer)
        self._buffer.clear()
        self._speech_started = False
        self._speech_ms = 0.0
        return audio

    def clear(self):
        """Полностью очищает буфер."""
        self._buffer.clear()
        self._speech_started = False
        self._last_voice_time = 0.0
        self._speech_ms = 0.0

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

    async def process_chunk(self, chunk: bytes) -> dict | None:
        """
        Обрабатывает входящий аудиочанк.
        Возвращает результат распознавания при обнаружении паузы или сигнал
        перебивания (``{"type": "interrupt"}``), когда клиент заговорил поверх робота.
        """
        result = self.buffer.add_chunk(chunk)

        # --- Перебивание (barge-in): клиент говорит, пока говорит робот ---
        # Требуем непрерывную речь длительностью interrupt_threshold_ms, иначе
        # одиночный шумовой чанк или эхо ложно прервёт робота.
        if self._is_speaking:
            chunk_ms = len(chunk) * 1000 / (self.buffer.sample_rate * 2)
            if result["has_speech"]:
                self._interrupt_speech_ms += chunk_ms
            else:
                self._interrupt_speech_ms = 0.0
            if self._interrupt_speech_ms >= self._interrupt_threshold_ms:
                self._interrupted = True
                self._interrupt_speech_ms = 0.0
                logger.info("Interruption detected — client is speaking over the robot")
                return {"type": "interrupt"}
            # Пока робот говорит — реплику не распознаём (ждём паузы/перебивания)
            return None

        # --- Робот молчит: по паузе — конец реплики, запускаем распознавание ---
        self._interrupt_speech_ms = 0.0
        if result["pause_detected"] and not self.buffer.is_empty:
            audio_data = self.buffer.get_audio()
            if len(audio_data) > 1600:  # минимум 100ms аудио
                try:
                    text = await self.asr.recognize_short(audio_data)
                    if text and self.on_text_recognized:
                        await self.on_text_recognized(text)
                    return {"type": "recognition", "text": text}
                except Exception as e:
                    logger.error(f"ASR error: {e}")
                    return {"type": "error", "error": str(e)}

        return None

    async def speak(self, text: str) -> bytes:
        """Синтезирует речь и помечает, что робот говорит."""
        self._is_speaking = True
        self._interrupted = False
        try:
            audio = await self.tts.synthesize(text)
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
