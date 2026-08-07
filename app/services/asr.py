"""Yandex SpeechKit ASR (Speech-to-Text) service.

Два режима:
* короткое распознавание REST v1 (``recognize_short``) — весь буфер одним
  запросом уже после паузы;
* потоковое распознавание gRPC v3 (``start_stream`` → :class:`StreamingASRSession`)
  — аудио стримится во время речи, и к моменту паузы текст уже почти готов;
  завершение (EOU) форсируется по нашему VAD. Существенно снижает задержку.
"""

import asyncio
import httpx
from loguru import logger
from app.core.config import get_settings

# gRPC-стабы vendored (см. app/services/stt_v3/). Импорт мягкий: если grpc или
# protobuf недоступны — потоковый режим просто выключается, REST продолжает жить.
try:
    import grpc
    from app.services.stt_v3 import stt_pb2, recognizer_pb2_grpc
    STREAMING_AVAILABLE = True
except Exception as _e:  # pragma: no cover
    STREAMING_AVAILABLE = False
    _STREAMING_IMPORT_ERROR = str(_e)


def _alt_text(update) -> str:
    """Достаёт текст из AlternativeUpdate (первая гипотеза)."""
    alts = getattr(update, "alternatives", None)
    if not alts:
        return ""
    return (alts[0].text or "").strip()


class StreamingASRSession:
    """Одна потоковая gRPC-сессия распознавания (на одну реплику).

    Использование из пайплайна:
        session = await asr.start_stream()
        session.feed(pcm_chunk)   # много раз, по мере поступления аудио
        text = await session.finish()   # на паузе VAD: форсируем EOU и берём финал
    """

    def __init__(self, stub, options, metadata, final_wait_sec: float = 1.2):
        self._stub = stub
        self._options = options
        self._metadata = metadata
        self._final_wait_sec = final_wait_sec
        self._queue: asyncio.Queue = asyncio.Queue()
        self._final_parts: list[str] = []
        self._partial: str = ""
        self._final_ready = asyncio.Event()
        self._reader: asyncio.Task | None = None
        self._closed = False

    async def start(self):
        async def _requests():
            # Первым сообщением — параметры сессии.
            yield stt_pb2.StreamingRequest(session_options=self._options)
            while True:
                kind, payload = await self._queue.get()
                if kind == "audio":
                    yield stt_pb2.StreamingRequest(chunk=stt_pb2.AudioChunk(data=payload))
                elif kind == "eou":
                    yield stt_pb2.StreamingRequest(eou=stt_pb2.Eou())
                elif kind == "close":
                    return

        self._call = self._stub.RecognizeStreaming(_requests(), metadata=self._metadata)
        self._reader = asyncio.create_task(self._read_loop())

    async def _read_loop(self):
        try:
            async for resp in self._call:
                event = resp.WhichOneof("Event")
                if event == "partial":
                    self._partial = _alt_text(resp.partial) or self._partial
                elif event == "final":
                    txt = _alt_text(resp.final)
                    if txt:
                        self._final_parts.append(txt)
                elif event == "eou_update":
                    self._final_ready.set()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning(f"Streaming ASR read loop error: {e}")
        finally:
            self._final_ready.set()

    def feed(self, chunk: bytes):
        if not self._closed and chunk:
            self._queue.put_nowait(("audio", chunk))

    async def finish(self) -> str:
        """Форсирует конец реплики (EOU), ждёт финал и закрывает сессию."""
        if self._closed:
            return self._best_text()
        self._closed = True
        # Просим сервер финализировать распознавание накопленного аудио.
        self._queue.put_nowait(("eou", None))
        try:
            await asyncio.wait_for(self._final_ready.wait(), timeout=self._final_wait_sec)
        except asyncio.TimeoutError:
            logger.info("Streaming ASR: финал не пришёл вовремя — берём партиал")
        self._queue.put_nowait(("close", None))
        if self._reader:
            try:
                await asyncio.wait_for(self._reader, timeout=1.0)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                self._reader.cancel()
            except Exception:
                pass
        return self._best_text()

    def _best_text(self) -> str:
        if self._final_parts:
            return " ".join(self._final_parts).strip()
        return self._partial.strip()

    async def cancel(self):
        """Прерывает сессию без ожидания финала (например, при barge-in/сбросе)."""
        self._closed = True
        self._queue.put_nowait(("close", None))
        if self._reader and not self._reader.done():
            self._reader.cancel()
        try:
            self._call.cancel()
        except Exception:
            pass


class ASRService:
    """Распознавание речи через Yandex SpeechKit (REST v1 short + gRPC v3 streaming)."""

    def __init__(self):
        self.settings = get_settings()
        self.headers = {
            "Authorization": f"Api-Key {self.settings.yandex_api_key}",
        }
        # Персистентный клиент: переиспользует TCP/TLS-соединения
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(15.0, connect=5.0),
            limits=httpx.Limits(max_keepalive_connections=5, max_connections=10),
        )
        self._grpc_channel = None
        self._stt_stub = None

    def _ensure_stub(self):
        """Ленивая инициализация gRPC-канала и стаба (в текущем event-loop)."""
        if self._stt_stub is not None:
            return self._stt_stub
        if not STREAMING_AVAILABLE:
            return None
        target = self.settings.speechkit_stt_streaming_url
        self._grpc_channel = grpc.aio.secure_channel(
            target, grpc.ssl_channel_credentials()
        )
        self._stt_stub = recognizer_pb2_grpc.RecognizerStub(self._grpc_channel)
        logger.info(f"Streaming ASR gRPC channel opened: {target}")
        return self._stt_stub

    def _streaming_options(self):
        """Собирает StreamingOptions: raw PCM 8k, REAL_TIME, внешний EOU (наш VAD)."""
        s = self.settings
        raw = stt_pb2.RawAudio(
            audio_encoding=stt_pb2.RawAudio.LINEAR16_PCM,
            sample_rate_hertz=s.audio_sample_rate,
            audio_channel_count=1,
        )
        model_opts = stt_pb2.RecognitionModelOptions(
            model=s.asr_model,
            audio_format=stt_pb2.AudioFormatOptions(raw_audio=raw),
            audio_processing_type=stt_pb2.RecognitionModelOptions.REAL_TIME,
        )
        # Внешний EOU: концом реплики управляем мы (посылаем Eou по паузе VAD).
        eou = stt_pb2.EouClassifierOptions(
            external_classifier=stt_pb2.ExternalEouClassifier()
        )
        return stt_pb2.StreamingOptions(recognition_model=model_opts, eou_classifier=eou)

    async def start_stream(self) -> "StreamingASRSession | None":
        """Открывает новую потоковую сессию распознавания. None — если недоступно."""
        if not (self.settings.asr_streaming and STREAMING_AVAILABLE):
            return None
        try:
            stub = self._ensure_stub()
            if stub is None:
                return None
            metadata = (("authorization", f"Api-Key {self.settings.yandex_api_key}"),)
            session = StreamingASRSession(stub, self._streaming_options(), metadata)
            await session.start()
            return session
        except Exception as e:
            logger.warning(f"Streaming ASR start failed, fallback to REST: {e}")
            return None

    async def recognize_short(
        self,
        audio_data: bytes,
        format: str = "lpcm",
        sample_rate: int | None = None,
        language: str | None = None,
    ) -> str:
        """
        Распознавание короткого аудио (до 30 сек).

        Args:
            audio_data: Аудиоданные в raw PCM или OGG
            format: Формат (lpcm, oggopus)
            sample_rate: Частота дискретизации
            language: Язык распознавания

        Returns:
            str: Распознанный текст
        """
        sample_rate = sample_rate or self.settings.audio_sample_rate
        language = language or self.settings.asr_language

        url = "https://stt.api.cloud.yandex.net/speech/v1/stt:recognize"
        params = {
            "folderId": self.settings.yandex_folder_id,
            "lang": language,
            "format": format,
            "sampleRateHertz": str(sample_rate),
            "model": self.settings.asr_model,
        }

        logger.info(f"ASR short request: {len(audio_data)} bytes, lang={language}")

        response = await self._client.post(
            url,
            headers=self.headers,
            params=params,
            content=audio_data,
        )

        if response.status_code != 200:
            logger.error(f"ASR error {response.status_code}: {response.text}")
            raise Exception(f"ASR recognition failed: {response.status_code}")

        result = response.json()
        text = result.get("result", "")
        logger.info(f"ASR result: '{text}'")
        return text

    async def recognize_long(
        self,
        audio_url: str,
        format: str = "lpcm",
        sample_rate: int | None = None,
    ) -> str:
        """
        Асинхронное распознавание длинного аудио.
        Отправляет задачу и возвращает operation_id.
        """
        sample_rate = sample_rate or self.settings.audio_sample_rate

        url = "https://transcribe.api.cloud.yandex.net/speech/stt/v2/longRunningRecognize"
        body = {
            "config": {
                "specification": {
                    "languageCode": self.settings.asr_language,
                    "model": self.settings.asr_model,
                    "audioEncoding": "LINEAR16_PCM" if format == "lpcm" else "OGG_OPUS",
                    "sampleRateHertz": sample_rate,
                    "audioChannelCount": self.settings.audio_channels,
                },
                "folderId": self.settings.yandex_folder_id,
            },
            "audio": {"uri": audio_url},
        }

        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(url, headers=self.headers, json=body)
            if response.status_code != 200:
                raise Exception(f"ASR long recognition failed: {response.status_code}")
            return response.json().get("id", "")
