"""Audio pipeline: buffer management, VAD (voice activity detection), interruption handling."""

import asyncio
import time
from dataclasses import dataclass, field
from loguru import logger


@dataclass
class AudioBuffer:
    """Буфер аудиоданных с определением пауз и перебиваний."""

    sample_rate: int = 8000
    silence_threshold: int = 500       # амплитуда тишины
    pause_duration: float = 0.8        # пауза для определения конца реплики (сек)
    interrupt_duration: float = 0.3    # порог для перебивания (сек)

    _buffer: bytearray = field(default_factory=bytearray)
    _last_voice_time: float = 0.0
    _speech_started: bool = False
    _total_speech_ms: int = 0

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
            result["has_speech"] = True
        elif self._speech_started:
            silence_duration = now - self._last_voice_time
            if silence_duration >= self.pause_duration:
                result["pause_detected"] = True
                self._speech_started = False
                logger.debug(f"Pause detected after {silence_duration:.2f}s silence")

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
        return audio

    def clear(self):
        """Полностью очищает буфер."""
        self._buffer.clear()
        self._speech_started = False
        self._last_voice_time = 0.0

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

    def __init__(self, asr_service, tts_service, on_text_recognized=None, on_audio_ready=None):
        self.asr = asr_service
        self.tts = tts_service
        self.buffer = AudioBuffer()
        self.on_text_recognized = on_text_recognized
        self.on_audio_ready = on_audio_ready
        self._is_speaking = False  # робот сейчас говорит
        self._interrupted = False

    async def process_chunk(self, chunk: bytes) -> dict | None:
        """
        Обрабатывает входящий аудиочанк.
        Возвращает результат распознавания при обнаружении паузы.
        """
        result = self.buffer.add_chunk(chunk)

        # Определение перебивания: клиент говорит, пока робот говорит
        if result["has_speech"] and self._is_speaking:
            self._interrupted = True
            logger.info("Interruption detected — client is speaking over the robot")
            return {"type": "interrupt"}

        # Пауза — конец реплики, запускаем распознавание
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
