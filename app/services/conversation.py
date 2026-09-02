"""ConversationDriver — переиспользуемый движок одного разговора.

Инкапсулирует пер-репличную логику диалога, которая раньше жила прямо внутри
WebSocket-обработчика ``audio_websocket`` (``app/api/routes.py``). Транспорт
(браузерный WebSocket или телефонный SIP-канал) абстрагирован двумя callback'ами:

* ``send_audio(chunk: bytes)`` — отправить синтезированный аудио-чанк (TTS)
  собеседнику (в WS — бинарным кадром, в SIP — в RTP-поток);
* ``send_event(event: dict)`` — отправить служебное событие (распознавание,
  intent, смена фазы и т.п.). Для транспорта без обратного канала (телефон) это
  может быть просто логирование.

Благодаря этому и браузер, и телефония используют один и тот же код диалога.
"""

import asyncio
import time
from typing import Awaitable, Callable

from loguru import logger

from app.core.config import get_settings
from app.services.audio_pipeline import AudioPipeline
from app.services import registry
from app.services.text_normalize import normalize_for_tts


# Сигналы передачи трубки секретарём ЛПР (v1)
_TRANSFER_SIGNALS = ("переведу", "соединяю", "передаю трубку", "переключаю")
_PRE_LPR_STEPS = {
    "start", "secretary_objection", "lpr_objection",
    "get_contact_future", "get_contact",
}

# Причины молчаливого завершения звонка — пишем в транскрипт, чтобы в дашборде
# было видно, почему разговор закончился без единой реплики.
_HANGUP_REASONS: dict[str, str] = {
    "answering_machine": "[Завершение: ответил автоответчик / голосовое меню]",
    "no_human": "[Завершение: живой собеседник не отозвался]",
}

# ── Лестница молчания ──────────────────────────────────────────────────────────
# Сколько шагов делаем, прежде чем положить трубку (последний шаг — прощание).
_SILENCE_LADDER_STEPS = 3
# Шаг 1: короткий оклик — не новая информация, а именно «вы на линии?».
_SILENCE_NUDGE = "Алло, вы меня слышите?"
# Шаг 2 (если повторить нечего): мягкое предложение перезвонить.
_SILENCE_STILL_THERE = "Вы меня слышите? Если сейчас неудобно — я перезвоню позже."
# Шаг 3: вежливое завершение.
_SILENCE_GIVE_UP = "Похоже, связь прервалась. Я перезвоню позже, всего доброго!"

# Робота перебили, он замолчал, а собеседник так и не сказал ничего разборчивого
# (перебил и сам замолчал). После короткой паузы просим повторить.
_INTERRUPT_REASK = "Извините, повторите, пожалуйста, не расслышала вас."

SendAudio = Callable[[bytes], Awaitable[None]]
SendEvent = Callable[[dict], Awaitable[None]]


async def _noop_event(_event: dict) -> None:
    return None


class ConversationDriver:
    """Ведёт один разговор поверх абстрактного транспорта."""

    def __init__(
        self,
        call_id: str,
        session,
        scenario,
        send_audio: SendAudio,
        send_event: SendEvent | None = None,
        flush_audio=None,
        audio_pending=None,
        flush_input=None,
    ):
        self.call_id = call_id
        self.session = session
        self.scenario = scenario
        self._send_audio = send_audio
        self._send_event = send_event or _noop_event
        # Транспортные хуки для barge-in в телефонии:
        #  * flush_audio()   — сбросить уже сгенерированный, но ещё не проигранный
        #                      исходящий звук (очередь воспроизведения);
        #  * audio_pending() — есть ли ещё непроигранный исходящий звук (робот
        #                      фактически «говорит», пока очередь не опустела).
        self._flush_audio = flush_audio
        self._audio_pending = audio_pending
        #  * flush_input()  — выбросить входящее аудио, накопленное транспортом,
        #                     пока робот говорил (там наше собственное эхо).
        self._flush_input = flush_input

        self.pipeline = AudioPipeline(
            asr_service=registry.asr_service,
            tts_service=registry.tts_service,
        )
        # Робот считается говорящим, пока проигрывается исходящий звук — чтобы
        # перебивание ловилось и после того, как генерация TTS уже завершилась.
        self.pipeline._audio_pending_cb = audio_pending
        # Конфиг голоса TTS (устанавливается клиентом через config-сообщение)
        self.tts_voice_config: dict = {}
        # Флаг: разговор дошёл до финального шага и должен завершиться
        self.should_end = False
        # Текущая (отменяемая) задача синтеза речи — для barge-in
        self._tts_task: asyncio.Task | None = None
        # Обработка реплики (ASR + классификация + ответ) идёт в фоне, чтобы
        # приём аудио не простаивал: транспорт продолжает читать кадры, пока
        # считается предыдущий ход.
        self._turn_task: asyncio.Task | None = None
        # Реплика, пришедшая, пока считался предыдущий ход (одна ячейка —
        # склеиваем, чтобы не плодить параллельные process_turn на сессию).
        self._pending_audio: bytes = b""
        # Текст прерванной реплики: если перебивание оказалось ложным (эхо/шум,
        # ASR ничего не распознал) — договариваем её, а не молчим.
        self._interrupted_text: str = ""
        self._speaking_text: str = ""
        # Момент последнего barge-in: если после него собеседник так и не сказал
        # ничего разборчивого и на линии тишина — переспрашиваем «повторите».
        self._interrupted_at: float | None = None
        # Сторож тишины: сколько раз подряд собеседник ничего не сказал.
        self._silence_prompts = 0
        self._last_input_at = time.monotonic()
        self._watchdog_task: asyncio.Task | None = None

    # --- Конфигурация голоса ---

    def set_tts_config(self, config: dict):
        self.tts_voice_config.update(config)
        logger.info(f"TTS config updated: {self.tts_voice_config}")

    # --- TTS ---

    async def _provider_audio_stream(self, text: str):
        """Синтез одной фразы активным провайдером → PCM-чанки (async generator)."""
        # Телефонные номера («8 800 775 96 31») переводим в словесную форму,
        # иначе TTS читает их как единое огромное число.
        text = normalize_for_tts(text)
        provider = self.tts_voice_config.get("provider", "yandex")
        voice = self.tts_voice_config.get("voice") or None
        if provider == "salutespeech":
            # SaluteSpeech не поддерживает стриминг — отдаём одним куском
            sr = self.tts_voice_config.get("sample_rate")
            audio = await registry.salutespeech_tts_service.synthesize(
                text=text, voice=voice, sample_rate=int(sr) if sr else None,
            )
            yield audio
        elif provider == "fishaudio":
            # fish.audio: голос задаётся через reference_id (лежит в voice)
            async for chunk in registry.fishaudio_tts_service.synthesize_stream(
                text=text, reference_id=voice,
                speed=float(self.tts_voice_config.get("speed") or 1.0) or None,
            ):
                yield chunk
        else:
            async for chunk in registry.tts_service.synthesize_stream(
                text=text, voice=voice,
                role=self.tts_voice_config.get("role") or None,
                speed=float(self.tts_voice_config.get("speed") or 1.0) or None,
            ):
                yield chunk

    async def _run_tts(self, text: str):
        """Тело стриминга TTS одной фразы. Отменяется при перебивании (barge-in)."""
        self.pipeline._is_speaking = True
        self._speaking_text = text
        interrupted = False
        try:
            async for chunk in self._provider_audio_stream(text):
                await self._send_audio(chunk)
        except asyncio.CancelledError:
            # Робота перебили — прекращаем синтез, речь дальше не отправляем
            interrupted = True
            logger.info(f"TTS cancelled by barge-in: call_id={self.call_id}")
            raise
        except Exception as tts_err:
            logger.warning(f"TTS stream failed, session continues: {tts_err}")
            await self._send_event({"type": "interrupt"})
        finally:
            self.pipeline._is_speaking = False
            self._speaking_text = ""
            self._after_speech(interrupted=interrupted)

    async def _run_tts_sentences(self, sentences, spoken: list[str]):
        """Озвучивает поток предложений (потоковый GPT→TTS). Отменяемо (barge-in).

        Собранный текст складывает в ``spoken`` (для транскрипта), т.к. при
        стриминге полный ответ заранее неизвестен.
        """
        self.pipeline._is_speaking = True
        interrupted = False
        try:
            async for sentence in sentences:
                if not sentence.strip():
                    continue
                spoken.append(sentence.strip())
                async for chunk in self._provider_audio_stream(sentence):
                    await self._send_audio(chunk)
        except asyncio.CancelledError:
            interrupted = True
            logger.info(f"TTS(stream) cancelled by barge-in: call_id={self.call_id}")
            raise
        except Exception as tts_err:
            logger.warning(f"TTS stream(sentences) failed, session continues: {tts_err}")
            await self._send_event({"type": "interrupt"})
        finally:
            self.pipeline._is_speaking = False
            self._after_speech(interrupted=interrupted)

    def _after_speech(self, interrupted: bool = False):
        """Вызывается, когда робот закончил свою реплику.

        Выбрасывает входящее аудио, накопленное транспортом за время речи — там
        наше собственное эхо (эхоподавления на линии нет), и взводит эхо-хвост
        в пайплайне.

        При перебивании (``interrupted``) не делает НИЧЕГО: собеседник говорит
        прямо сейчас, и чистка входа выбросила бы начало его реплики.
        """
        if interrupted:
            return
        if self._flush_input is not None:
            try:
                self._flush_input()
            except Exception as e:
                logger.warning(f"flush_input failed: {e}")
        self.pipeline.arm_echo_guard()

    async def stream_tts(self, text: str):
        """Проигрывает TTS как отменяемую задачу и дожидается её завершения.

        Блокирующий вариант (для приветствия/финальной реплики). Для реплик,
        которые должны быть прерываемыми на лету, используйте ``start_tts``.
        """
        # Помечаем «робот говорит» синхронно: между create_task и первым шагом
        # планировщика робот иначе считается молчащим, и входящий кадр успевает
        # открыть новый ход поверх начинающейся реплики.
        self.pipeline._is_speaking = True
        self._tts_task = asyncio.create_task(self._run_tts(text))
        try:
            await self._tts_task
        except asyncio.CancelledError:
            pass
        finally:
            self._tts_task = None

    def start_tts(self, text: str):
        """Запускает TTS в фоне (не блокируя приёмный цикл), чтобы во время речи
        робота можно было принимать аудио клиента и обнаружить перебивание."""
        self.pipeline._is_speaking = True
        self._tts_task = asyncio.create_task(self._run_tts(text))

    async def interrupt(self):
        """Barge-in: прерывает текущую речь робота и просит клиента остановить
        воспроизведение уже отправленного аудио."""
        # 1) Сбрасываем уже сгенерированный, но ещё не проигранный звук — иначе
        #    робот договорит фразу до конца из очереди воспроизведения.
        if self._flush_audio is not None:
            try:
                self._flush_audio()
            except Exception as e:
                logger.warning(f"flush_audio failed: {e}")
        # 2) Останавливаем задачу синтеза (генерацию оставшегося текста).
        #    Текст запоминаем: если перебивание окажется ложным (эхо/шум, ASR
        #    ничего не распознал) — договорим реплику, а не замолчим навсегда.
        self._interrupted_text = self._speaking_text
        self._interrupted_at = time.monotonic()
        task = self._tts_task
        self._tts_task = None
        if task and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        self.pipeline._is_speaking = False
        await self._send_event({"type": "stop_audio"})

    # --- Приём аудио ---

    async def feed_chunk(self, chunk: bytes):
        """Обрабатывает входящий аудиочанк собеседника.

        Метод намеренно «дешёвый»: он только считает VAD. Распознавание и ход
        диалога уходят в фоновую задачу — иначе поток чтения кадров в телефонии
        блокируется на 2–5 с (ASR + классификация + старт TTS), кадры копятся в
        очереди транспорта, часть речи собеседника теряется, а VAD разбирает
        остаток пачкой уже с неверными таймингами.
        """
        result = await self.pipeline.process_chunk(chunk)
        if not result:
            return

        if result["type"] == "utterance":
            self._submit_utterance(result["audio"], result.get("asr_session"))
        elif result["type"] == "interrupt":
            # Клиент заговорил поверх робота — прерываем речь (barge-in)
            await self.interrupt()

    def _submit_utterance(self, audio: bytes, asr_session=None):
        """Ставит реплику собеседника в обработку (не блокируя приём аудио)."""
        self._last_input_at = time.monotonic()
        if self._turn_task is not None and not self._turn_task.done():
            # Предыдущий ход ещё считается — склеиваем реплики в одну ячейку.
            self._pending_audio += audio
            if asr_session is not None:
                # Потоковую сессию отложенной реплики не используем: финал по ней
                # уже не соберём корректно — распознаем накопленное через REST.
                asyncio.create_task(self.pipeline._close_session(asr_session))
            return
        self._turn_task = asyncio.create_task(self._process_utterance(audio, asr_session))

    async def _process_utterance(self, audio: bytes, asr_session=None):
        """Распознаёт реплику и ведёт ход диалога (фоновая задача)."""
        try:
            text = (await self.pipeline.recognize_utterance(audio, asr_session)).strip()
            if text:
                self._silence_prompts = 0
                self._interrupted_text = ""
                self._interrupted_at = None
                await self.handle_recognition(text)
            elif self._interrupted_text:
                # Перебивание было ложным (эхо/шум): договариваем прерванное.
                text_to_finish, self._interrupted_text = self._interrupted_text, ""
                self._interrupted_at = None
                logger.info(f"Resuming interrupted phrase: call_id={self.call_id}")
                self.start_tts(text_to_finish)
        except Exception as e:
            logger.error(f"Utterance processing failed: {e}")
        finally:
            self._last_input_at = time.monotonic()
            pending, self._pending_audio = self._pending_audio, b""
            if pending:
                self._turn_task = asyncio.create_task(self._process_utterance(pending))

    # --- Сторож тишины ---

    def start_watchdog(self):
        """Запускает наблюдение за молчанием собеседника."""
        if self._watchdog_task is None:
            self._last_input_at = time.monotonic()
            self._watchdog_task = asyncio.create_task(self._watch_silence())

    def _line_busy(self) -> bool:
        """Идёт ли прямо сейчас что-то, что запрещает роботу заговорить.

        Робот имеет право подать голос, только когда на линии действительно
        тихо. «Тихо» — это одновременно:

        * робот договорил — включая ещё не проигранную очередь воспроизведения
          (генерация TTS заканчивается в разы быстрее, чем звучит реплика:
          15-секундная фраза синтезируется за пару секунд, и по одному лишь
          ``_is_speaking`` робот считался молчащим почти всю свою же реплику);
        * не считается предыдущий ход (ASR → классификация → ответ);
        * собеседник не говорит прямо сейчас (его реплика началась, но пауза
          конца ещё не наступила).
        """
        if self.pipeline._robot_speaking():
            return True
        if self._turn_task is not None and not self._turn_task.done():
            return True
        return self.pipeline.buffer.is_speech_active

    async def _watch_silence(self):
        """Переспрашивает при затянувшемся молчании, затем вежливо завершает.

        Лестница намеренно медленная и «немногословная»: собеседнику нужно
        время, чтобы осмыслить вопрос и ответить. Раньше отсчёт шёл от конца
        генерации TTS (а не от конца звучания), паузы были по 8 с, и каждый шаг
        вываливал целый скриптовый вопрос — робот успевал произнести три реплики
        подряд, ни разу не дав человеку вставить слово.
        """
        settings = get_settings()
        timeout = settings.no_input_timeout_sec
        if timeout <= 0:
            return
        repeat_timeout = settings.no_input_repeat_timeout_sec or timeout
        reask_after = getattr(settings, "interrupt_reask_sec", 3.0)
        try:
            while not self.should_end:
                await asyncio.sleep(0.5)
                if self._line_busy():
                    self._last_input_at = time.monotonic()
                    continue
                # Робота перебили, он замолчал — и собеседник тоже замолчал, так
                # и не сказав ничего разборчивого. После короткой паузы мягко
                # просим повторить (а не договариваем прежнюю реплику и не молчим).
                if (
                    self._interrupted_text
                    and self._interrupted_at is not None
                    and reask_after > 0
                    and time.monotonic() - self._interrupted_at >= reask_after
                ):
                    self._interrupted_text = ""
                    self._interrupted_at = None
                    self._last_input_at = time.monotonic()
                    logger.info(
                        f"Barge-in + тишина {reask_after:.0f}s — просим повторить: "
                        f"call_id={self.call_id}"
                    )
                    await registry.call_manager.add_to_transcript(
                        self.call_id, "robot", _INTERRUPT_REASK,
                    )
                    self.start_tts(_INTERRUPT_REASK)
                    continue
                # Первый переспрос — через timeout, последующие — реже.
                wait = timeout if self._silence_prompts == 0 else repeat_timeout
                if time.monotonic() - self._last_input_at < wait:
                    continue
                self._last_input_at = time.monotonic()
                self._silence_prompts += 1
                if self._silence_prompts >= _SILENCE_LADDER_STEPS:
                    logger.info(f"No input — завершаем звонок: call_id={self.call_id}")
                    text = _SILENCE_GIVE_UP
                    await registry.call_manager.add_to_transcript(self.call_id, "robot", text)
                    await self.stream_tts(text)
                    self.should_end = True
                    return
                text = self._silence_prompt_text(self._silence_prompts)
                if not text:
                    continue
                logger.info(
                    f"No input {wait:.0f}s — переспрашиваем "
                    f"(#{self._silence_prompts}): call_id={self.call_id}"
                )
                await registry.call_manager.add_to_transcript(self.call_id, "robot", text)
                self.start_tts(text)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning(f"silence watchdog stopped: {e}")

    def _silence_prompt_text(self, level: int) -> str:
        """Чем переспросить при молчании на шаге ``level`` лестницы.

        Шаг 1 — короткий оклик: молчание чаще всего значит «отошёл» или «не
        расслышал», а не «не понял вопрос». Новую информацию здесь давать
        нельзя — это и превращало разговор в монолог робота.

        Шаг 2 — ровно тот вопрос, который робот уже задал (не следующий по
        сценарию!). Раньше сюда подставлялся ``current_question`` — вопрос
        ТЕКУЩЕЙ фазы, то есть робот в ответ на молчание задавал новый вопрос,
        так и не получив ответа на предыдущий.
        """
        if level <= 1:
            return _SILENCE_NUDGE
        if self.session.algo_version == "v2":
            try:
                question = registry.script_v2_engine.pending_question(self.call_id)
            except Exception:
                question = ""
            if question:
                return question
        return _SILENCE_STILL_THERE

    # --- Одна реплика собеседника ---

    async def handle_recognition(self, text: str):
        """Обрабатывает распознанную реплику: роутинг v1/v2, ответ, TTS."""
        call_id = self.call_id
        session = self.session
        scenario = self.scenario

        await registry.call_manager.add_to_transcript(call_id, "client", text)
        await self._send_event({"type": "recognition", "text": text})

        current_step_id = session.current_step
        current_step = scenario.steps.get(current_step_id)

        if current_step and current_step.is_final:
            self.should_end = True
            return

        ai_config = registry.ai_config_manager.get()
        if scenario.system_prompt and len(ai_config.get("system_prompt", "")) < 200:
            ai_config = {**ai_config, "system_prompt": scenario.system_prompt}

        v2_should_end = False
        if session.algo_version == "v2":
            # v2: строгий скриптовый алгоритм
            try:
                v2_result = await registry.script_v2_engine.process_turn(call_id, text)
                response_text = v2_result["robot_text"]
                intent = v2_result["node"]
                # Фаза closed (в т.ч. детект автоответчика/IVR на рукопожатии,
                # прощание, финал квалификации) — завершаем звонок.
                v2_should_end = v2_result["phase"] == "closed"
                await self._send_event({
                    "type": "phase",
                    "phase": v2_result["phase"],
                    "phase_label": v2_result["phase_label"],
                    "node": v2_result["node"],
                    "qual_step": v2_result["qual_step"],
                })
            except Exception as e:
                logger.error(f"V2 script engine failed: {e}")
                intent = "unknown"
                response_text = "Понял. Продолжайте, пожалуйста."
            next_step = current_step
        else:
            # v1: KB-поиск нужен только здесь (в v2 не используется).
            kb_context = await registry.kb_service.search(text)

            # Потоковый режим GPT→TTS: озвучка стартует с первого предложения
            # ответа, не дожидаясь полной генерации. Обрабатывает свой хвост сам.
            if get_settings().gpt_stream_tts:
                await self._handle_v1_streaming(
                    text, current_step, current_step_id, scenario, ai_config, kb_context,
                )
                return

            # Не потоковый путь (kill-switch GPT_STREAM_TTS=false): intent+ответ параллельно
            try:
                intent, response_text = await registry.dialogue_engine.generate_with_intent(
                    step=current_step,
                    transcript=session.transcript,
                    knowledge_context=kb_context,
                    ai_config=ai_config,
                )
            except Exception as e:
                logger.error(f"AI generation failed: {e}")
                intent = "unknown"
                response_text = (
                    current_step.greeting
                    if current_step and current_step.greeting
                    else "Понял. Продолжайте, пожалуйста."
                )
            next_step = await self._route_v1(intent, text, current_step, current_step_id, scenario)

        # --- Общий хвост: v2 и не потоковый v1 ---
        # Пустой ответ — либо детект автоответчика на рукопожатии (кладём трубку),
        # либо разговор собеседника «в сторону» (молчим и ждём).
        if not response_text.strip():
            if v2_should_end:
                await registry.call_manager.add_to_transcript(
                    call_id, "system", _HANGUP_REASONS.get(intent, f"[Завершение: {intent}]"),
                )
                self.should_end = True
            return

        await registry.call_manager.add_to_transcript(call_id, "robot", response_text)
        await self._send_event({"type": "intent", "intent": intent})
        await self._send_event({
            "type": "response",
            "text": response_text,
            "intent": intent,
            "step": next_step.id if next_step else current_step_id,
        })

        # Стриминг TTS. Финальную реплику проигрываем целиком (перебивать нечего),
        # остальные — в фоне (start_tts), чтобы приёмный цикл продолжал читать
        # аудио клиента и мог обнаружить перебивание (barge-in).
        if (next_step and next_step.is_final) or v2_should_end:
            await self.stream_tts(response_text)
            self.should_end = True
        else:
            self.start_tts(response_text)

    async def _route_v1(self, intent, text, current_step, current_step_id, scenario):
        """Маршрутизация v1 по intent + сигналу передачи трубки. Возвращает next_step."""
        is_transfer = (
            current_step_id in _PRE_LPR_STEPS
            and any(sig in text.lower() for sig in _TRANSFER_SIGNALS)
            and "lpr_greeting" in scenario.steps
        )
        next_step_id = None
        if is_transfer:
            next_step_id = "lpr_greeting"
            logger.info(f"Transfer signal detected, routing to lpr_greeting (from step={current_step_id})")
        elif current_step:
            if intent == "positive":
                next_step_id = current_step.on_positive
            elif intent == "negative":
                next_step_id = current_step.on_negative
            elif intent == "objection":
                next_step_id = current_step.on_objection or current_step.on_unknown
            else:
                next_step_id = current_step.on_unknown or current_step_id

        if next_step_id:
            await registry.call_manager.update_step(self.call_id, next_step_id)
            return scenario.steps.get(next_step_id, current_step)
        return current_step

    async def _handle_v1_streaming(self, text, current_step, current_step_id, scenario, ai_config, kb_context):
        """Потоковый v1: intent (для маршрутизации) + пофразовая озвучка ответа GPT."""
        intent_task, sent_stream = registry.dialogue_engine.generate_with_intent_stream(
            step=current_step,
            transcript=self.session.transcript,
            knowledge_context=kb_context,
            ai_config=ai_config,
        )
        try:
            intent = await intent_task
        except Exception as e:
            logger.error(f"intent classify failed: {e}")
            intent = "unknown"

        next_step = await self._route_v1(intent, text, current_step, current_step_id, scenario)
        await self._send_event({"type": "intent", "intent": intent})

        if next_step and next_step.is_final:
            # Финальную реплику проигрываем целиком (не в фоне) — перебивать нечего.
            spoken: list[str] = []
            self._tts_task = asyncio.create_task(self._run_tts_sentences(sent_stream, spoken))
            try:
                await self._tts_task
            except asyncio.CancelledError:
                pass
            finally:
                self._tts_task = None
            await self._record_stream_reply(spoken, current_step, next_step, current_step_id, intent)
            self.should_end = True
        else:
            # Не финал — озвучка в фоне, чтобы приёмный цикл ловил barge-in.
            self._tts_task = asyncio.create_task(
                self._speak_and_record(sent_stream, current_step, next_step, current_step_id, intent)
            )

    async def _speak_and_record(self, sent_stream, current_step, next_step, current_step_id, intent):
        """Фоновая пофразовая озвучка потока GPT + запись реплики в транскрипт."""
        spoken: list[str] = []
        try:
            await self._run_tts_sentences(sent_stream, spoken)
        except asyncio.CancelledError:
            # Перебили — фиксируем то, что успели произнести, и выходим.
            if spoken:
                await self._record_stream_reply(spoken, current_step, next_step, current_step_id, intent)
            return
        await self._record_stream_reply(spoken, current_step, next_step, current_step_id, intent)

    async def _record_stream_reply(self, spoken, current_step, next_step, current_step_id, intent):
        """Записывает произнесённый (потоково) ответ в транскрипт и шлёт событие."""
        response_text = " ".join(spoken).strip() or (
            current_step.greeting if current_step and current_step.greeting
            else "Понял. Продолжайте, пожалуйста."
        )
        await registry.call_manager.add_to_transcript(self.call_id, "robot", response_text)
        await self._send_event({
            "type": "response",
            "text": response_text,
            "intent": intent,
            "step": next_step.id if next_step else current_step_id,
        })

    # --- Прямые действия (используются транспортом) ---

    async def speak(self, text: str):
        """Произнести произвольный текст (например, приветствие)."""
        await registry.call_manager.add_to_transcript(self.call_id, "robot", text)
        await self.stream_tts(text)

    async def switch_to_lpr(self):
        """Ручная смена собеседника: секретарь передала трубку ЛПР."""
        call_id = self.call_id
        session = self.session
        await registry.call_manager.add_to_transcript(
            call_id, "system", "[Смена собеседника: секретарь передала трубку ЛПР]"
        )
        if session.algo_version == "v2":
            try:
                v2_result = await registry.script_v2_engine.process_turn(call_id, "соединяю")
                lpr_text = v2_result["robot_text"]
                await registry.call_manager.add_to_transcript(call_id, "robot", lpr_text)
                await self._send_event({
                    "type": "phase",
                    "phase": v2_result["phase"],
                    "phase_label": v2_result["phase_label"],
                    "node": v2_result["node"],
                    "qual_step": v2_result["qual_step"],
                })
                await self._send_event({
                    "type": "response",
                    "text": lpr_text,
                    "intent": "transfer",
                    "step": "lpr_greeting",
                })
                await self.stream_tts(lpr_text)
            except Exception as e:
                logger.error(f"V2 switch_to_lpr failed: {e}")
        else:
            await registry.call_manager.update_step(call_id, "lpr_greeting")
            await self._send_event({"type": "step_changed", "step": "lpr_greeting"})
        logger.info(f"Manual switch to lpr_greeting: call_id={call_id}")

    # --- Исход звонка ---

    #: Исход движка v2 → статус клиента в карточке звонка/кампании
    _OUTCOME_TO_STATUS: dict[str, str] = {
        "application": "interested",
        "contact_obtained": "interested",
        "callback_later": "callback",
        "refused": "not_interested",
        "machine": "unknown",
        "no_human": "unknown",
    }

    def _v2_outcome(self) -> tuple[str, str]:
        """Статус клиента и заметка по данным движка v2 (или пустые строки)."""
        if self.session.algo_version != "v2":
            return "", ""
        try:
            result = registry.script_v2_engine.get_outcome(self.call_id)
        except Exception as e:
            logger.warning(f"v2 outcome unavailable: {e}")
            return "", ""
        outcome = result.get("outcome") or ""
        status = self._OUTCOME_TO_STATUS.get(outcome, "")
        if not status:
            return "", ""
        data = result.get("data") or {}
        details = ", ".join(f"{k}={v}" for k, v in data.items()) or "—"
        return status, f"ДАННЫЕ ДВИЖКА: исход={outcome}; {details}"

    # --- Завершение ---

    async def finalize(self):
        """Генерирует саммари и квалификацию клиента, завершает звонок."""
        # Останавливаем фоновые задачи (речь, ход диалога, сторож тишины),
        # чтобы не осталось «висячих» корутин после завершения звонка.
        self.should_end = True
        for task in (self._tts_task, self._turn_task, self._watchdog_task):
            if task and not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
                except Exception:
                    pass
        self._tts_task = None
        self._turn_task = None
        self._watchdog_task = None
        session = await registry.call_manager.get_call(self.call_id)
        if session and session.transcript:
            try:
                summary = await registry.call_analyzer.generate_summary(
                    session.transcript, self.scenario
                )
                # Исход, зафиксированный самим движком v2, надёжнее разбора
                # транскрипта ИИ: движок точно знает, дошли ли мы до заявки,
                # получили ли контакт и договорились ли о перезвоне.
                engine_status, engine_note = self._v2_outcome()
                if engine_status:
                    status = engine_status
                    summary = f"{summary}\n{engine_note}".strip()
                else:
                    qualification = await registry.call_analyzer.qualify_client(
                        session.transcript
                    )
                    status = qualification.get("status", "unknown")
                await registry.call_manager.end_call(
                    self.call_id, client_status=status, summary=summary,
                )
                logger.info(
                    f"Call analyzed: {self.call_id} | status={status} | "
                    f"summary_len={len(summary)}"
                )
                return status, summary
            except Exception as e:
                logger.error(f"Post-call analysis failed for {self.call_id}: {e}")
                await registry.call_manager.end_call(self.call_id)
        else:
            await registry.call_manager.end_call(self.call_id)
        return "unknown", ""
