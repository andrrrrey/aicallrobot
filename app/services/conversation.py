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
from typing import Awaitable, Callable

from loguru import logger

from app.services.audio_pipeline import AudioPipeline
from app.services import registry


# Сигналы передачи трубки секретарём ЛПР (v1)
_TRANSFER_SIGNALS = ("переведу", "соединяю", "передаю трубку", "переключаю")
_PRE_LPR_STEPS = {
    "start", "secretary_objection", "lpr_objection",
    "get_contact_future", "get_contact",
}

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
    ):
        self.call_id = call_id
        self.session = session
        self.scenario = scenario
        self._send_audio = send_audio
        self._send_event = send_event or _noop_event

        self.pipeline = AudioPipeline(
            asr_service=registry.asr_service,
            tts_service=registry.tts_service,
        )
        # Конфиг голоса TTS (устанавливается клиентом через config-сообщение)
        self.tts_voice_config: dict = {}
        # Флаг: разговор дошёл до финального шага и должен завершиться
        self.should_end = False

    # --- Конфигурация голоса ---

    def set_tts_config(self, config: dict):
        self.tts_voice_config.update(config)
        logger.info(f"TTS config updated: {self.tts_voice_config}")

    # --- TTS ---

    async def stream_tts(self, text: str):
        """Стриминг TTS: отправляем аудиочанки по мере поступления от API."""
        provider = self.tts_voice_config.get("provider", "yandex")
        voice = self.tts_voice_config.get("voice") or None
        self.pipeline._is_speaking = True
        try:
            if provider == "salutespeech":
                # SaluteSpeech не поддерживает стриминг — отдаём одним куском
                sr = self.tts_voice_config.get("sample_rate")
                audio = await registry.salutespeech_tts_service.synthesize(
                    text=text, voice=voice,
                    sample_rate=int(sr) if sr else None,
                )
                await self._send_audio(audio)
            else:
                async for chunk in registry.tts_service.synthesize_stream(
                    text=text,
                    voice=voice,
                    role=self.tts_voice_config.get("role") or None,
                    speed=float(self.tts_voice_config.get("speed") or 1.0) or None,
                ):
                    await self._send_audio(chunk)
        except Exception as tts_err:
            logger.warning(f"TTS stream failed, session continues: {tts_err}")
            await self._send_event({"type": "interrupt"})
        finally:
            self.pipeline._is_speaking = False

    # --- Приём аудио ---

    async def feed_chunk(self, chunk: bytes):
        """Обрабатывает входящий аудиочанк собеседника."""
        result = await self.pipeline.process_chunk(chunk)
        if not result:
            return

        if result["type"] == "recognition":
            text = result.get("text", "").strip()
            if text:
                await self.handle_recognition(text)
        elif result["type"] == "interrupt":
            await self._send_event({"type": "interrupt"})

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

        # KB-поиск запускаем сразу, параллельно с подготовкой промпта
        kb_task = asyncio.create_task(registry.kb_service.search(text))

        ai_config = registry.ai_config_manager.get()
        if scenario.system_prompt and len(ai_config.get("system_prompt", "")) < 200:
            ai_config = {**ai_config, "system_prompt": scenario.system_prompt}

        kb_context = await kb_task

        if session.algo_version == "v2":
            # v2: строгий скриптовый алгоритм
            try:
                v2_result = await registry.script_v2_engine.process_turn(call_id, text)
                response_text = v2_result["robot_text"]
                intent = v2_result["node"]
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
            # v1: один GPT-вызов: intent + ответ одновременно
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

            # Определяем сигнал передачи трубки — переключаем на ЛПР вне зависимости от шага
            is_transfer = (
                current_step_id in _PRE_LPR_STEPS
                and any(sig in text.lower() for sig in _TRANSFER_SIGNALS)
                and "lpr_greeting" in scenario.steps
            )

            # Роутинг по возвращённому intent
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
                await registry.call_manager.update_step(call_id, next_step_id)
                next_step = scenario.steps.get(next_step_id, current_step)
            else:
                next_step = current_step

        await registry.call_manager.add_to_transcript(call_id, "robot", response_text)
        await self._send_event({"type": "intent", "intent": intent})
        await self._send_event({
            "type": "response",
            "text": response_text,
            "intent": intent,
            "step": next_step.id if next_step else current_step_id,
        })

        # Стриминг TTS: первый аудиочанк собеседнику сразу как он придёт от API
        await self.stream_tts(response_text)

        # Проверяем финальность следующего шага
        if next_step and next_step.is_final:
            self.should_end = True

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

    # --- Завершение ---

    async def finalize(self):
        """Генерирует саммари и квалификацию клиента, завершает звонок."""
        session = await registry.call_manager.get_call(self.call_id)
        if session and session.transcript:
            try:
                summary = await registry.call_analyzer.generate_summary(
                    session.transcript, self.scenario
                )
                qualification = await registry.call_analyzer.qualify_client(session.transcript)
                await registry.call_manager.end_call(
                    self.call_id,
                    client_status=qualification.get("status", "unknown"),
                    summary=summary,
                )
                logger.info(
                    f"Call analyzed: {self.call_id} | status={qualification.get('status')} | "
                    f"summary_len={len(summary)}"
                )
                return qualification.get("status", "unknown"), summary
            except Exception as e:
                logger.error(f"Post-call analysis failed for {self.call_id}: {e}")
                await registry.call_manager.end_call(self.call_id)
        else:
            await registry.call_manager.end_call(self.call_id)
        return "unknown", ""
