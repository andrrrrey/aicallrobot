"""API routes for the AI robot."""

import asyncio
import json
from pathlib import Path
from fastapi import APIRouter, HTTPException, WebSocket, WebSocketDisconnect, UploadFile, File
from pydantic import BaseModel
from loguru import logger

from app.services.tts import TTSService
from app.services.salutespeech_tts import SaluteSpeechTTSService
from app.services.asr import ASRService
from app.services.call_manager import CallManager
from app.services.scenario_engine import ScenarioManager
from app.services.audio_pipeline import AudioPipeline
from app.services.yandex_gpt import YandexGPTService
from app.services.knowledge_base import KnowledgeBaseService, extract_text
from app.services.dialogue_engine import DialogueEngine
from app.services.call_analyzer import CallAnalyzer
from app.services.ai_config_manager import AIConfigManager

router = APIRouter()

# Singletons
tts_service = TTSService()
salutespeech_tts_service = SaluteSpeechTTSService()
asr_service = ASRService()
call_manager = CallManager()
scenario_manager = ScenarioManager()
gpt_service = YandexGPTService()
kb_service = KnowledgeBaseService()
dialogue_engine = DialogueEngine(gpt_service, kb_service)
call_analyzer = CallAnalyzer(gpt_service)
ai_config_manager = AIConfigManager()


# === Pydantic models ===

class TTSRequest(BaseModel):
    text: str
    voice: str | None = None
    speed: float | None = None
    role: str | None = None


class SaluteSpeechTTSRequest(BaseModel):
    text: str
    voice: str | None = None
    sample_rate: int | None = None


class ASRRequest(BaseModel):
    audio_base64: str
    format: str = "lpcm"


class StartCallRequest(BaseModel):
    phone_number: str
    scenario_id: str = "default"


class AIConfigUpdate(BaseModel):
    system_prompt: str
    scenario_context: str = ""


class ChatTestRequest(BaseModel):
    message: str
    history: list[dict] = []


# === Health ===

@router.get("/health")
async def health():
    stats = await call_manager.get_stats()
    return {"status": "ok", "calls": stats}


# === TTS ===

@router.post("/api/v1/tts")
async def synthesize_speech(request: TTSRequest):
    """Синтез речи из текста."""
    try:
        audio = await tts_service.synthesize(
            text=request.text,
            voice=request.voice,
            speed=request.speed,
            role=request.role,
        )
        import base64
        return {
            "audio_base64": base64.b64encode(audio).decode(),
            "format": "lpcm",
            "sample_rate": 8000,
            "size_bytes": len(audio),
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/api/v1/voices")
async def list_voices():
    """Список доступных голосов и амплуа."""
    return {"voices": TTSService.get_voices_info()}


# === SaluteSpeech TTS ===

@router.post("/api/v1/salutespeech/tts")
async def salutespeech_synthesize(request: SaluteSpeechTTSRequest):
    """Синтез речи через SaluteSpeech (Sber)."""
    try:
        audio = await salutespeech_tts_service.synthesize(
            text=request.text,
            voice=request.voice,
            sample_rate=request.sample_rate,
        )
        import base64
        return {
            "audio_base64": base64.b64encode(audio).decode(),
            "format": "wav",
            "size_bytes": len(audio),
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/api/v1/salutespeech/voices")
async def list_salutespeech_voices():
    """Список голосов SaluteSpeech."""
    return {"voices": SaluteSpeechTTSService.get_voices_info()}


# === ASR ===

@router.post("/api/v1/asr")
async def recognize_speech(request: ASRRequest):
    """Распознавание речи из аудио."""
    import base64
    try:
        audio_data = base64.b64decode(request.audio_base64)
        text = await asr_service.recognize_short(audio_data, format=request.format)
        return {"text": text, "audio_size": len(audio_data)}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# === Scenarios ===

@router.get("/api/v1/scenarios")
async def list_scenarios():
    """Список доступных сценариев."""
    return {"scenarios": scenario_manager.list_scenarios()}


@router.get("/api/v1/scenarios/{scenario_id}")
async def get_scenario(scenario_id: str):
    """Детали сценария."""
    scenario = scenario_manager.get_scenario(scenario_id)
    return {
        "id": scenario.id,
        "name": scenario.name,
        "description": scenario.description,
        "greeting": scenario.greeting,
        "steps": [
            {"id": s.id, "greeting": s.greeting, "is_final": s.is_final}
            for s in scenario.steps.values()
        ],
    }


# === Knowledge Base ===

@router.post("/api/v1/knowledge/upload")
async def upload_document(file: UploadFile = File(...)):
    """Загружает файл (.txt/.pdf/.docx) в базу знаний."""
    ext = Path(file.filename or "").suffix.lower()
    if ext not in (".txt", ".pdf", ".docx"):
        raise HTTPException(status_code=400, detail="Поддерживаются только .txt, .pdf, .docx")

    try:
        content = await file.read()
        text = extract_text(content, file.filename or "file.txt")
        result = await kb_service.add_document(filename=file.filename or "unknown", content=text)
        return result
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e))
    except Exception as e:
        logger.error(f"upload_document error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/api/v1/knowledge/documents")
async def list_documents():
    """Список документов в базе знаний."""
    return {"documents": kb_service.list_documents()}


@router.delete("/api/v1/knowledge/documents/{doc_id}")
async def delete_document(doc_id: str):
    """Удаляет документ из базы знаний."""
    ok = kb_service.delete_document(doc_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Документ не найден")
    return {"deleted": doc_id}


# === AI Config ===

@router.get("/api/v1/ai/config")
async def get_ai_config():
    """Получить текущие инструкции и контекст сценария для ИИ."""
    return ai_config_manager.get()


@router.put("/api/v1/ai/config")
async def update_ai_config(request: AIConfigUpdate):
    """Обновить инструкции и контекст сценария для ИИ."""
    return ai_config_manager.save(request.system_prompt, request.scenario_context)


# === AI Chat Test ===

@router.post("/api/v1/ai/chat")
async def chat_test(request: ChatTestRequest):
    """Тестовый чат с ИИ без голоса."""
    ai_config = ai_config_manager.get()

    messages: list[dict] = []
    system_prompt = ai_config.get("system_prompt", "").strip()
    scenario_context = ai_config.get("scenario_context", "").strip()

    # Обогащаем системный промпт контекстом из базы знаний
    kb_context = await kb_service.search(request.message)

    system_parts = []
    if system_prompt:
        system_parts.append(system_prompt)
    if scenario_context:
        system_parts.append(f"Контекст сценария:\n{scenario_context}")
    if kb_context:
        system_parts.append("Релевантная информация из базы знаний:\n" + "\n---\n".join(kb_context))

    if system_parts:
        messages.append({"role": "system", "text": "\n\n".join(system_parts)})

    for h in request.history[-10:]:
        role = h.get("role", "user")
        if role in ("user", "assistant"):
            messages.append({"role": role, "text": h.get("text", "")})

    messages.append({"role": "user", "text": request.message})

    try:
        response = await gpt_service.complete(messages)
        intent = await dialogue_engine.classify_intent(request.message)
        return {
            "response": response,
            "intent": intent,
            "kb_context": kb_context,
        }
    except Exception as e:
        logger.error(f"chat_test error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# === Calls ===

@router.post("/api/v1/calls/start")
async def start_call(request: StartCallRequest):
    """Начать звонок."""
    try:
        session = await call_manager.start_call(
            phone_number=request.phone_number,
            scenario_id=request.scenario_id,
        )
        scenario = scenario_manager.get_scenario(request.scenario_id)
        return {
            "call_id": session.call_id,
            "status": session.status.value,
            "greeting": scenario.greeting,
        }
    except Exception as e:
        raise HTTPException(status_code=429, detail=str(e))


# IMPORTANT: /calls/history must be declared BEFORE /calls/{call_id}
@router.get("/api/v1/calls/history")
async def get_call_history():
    """История завершённых звонков."""
    return {"calls": await call_manager.list_completed()}


@router.get("/api/v1/calls")
async def list_calls():
    """Список активных звонков."""
    return {"calls": await call_manager.list_active()}


@router.get("/api/v1/calls/{call_id}/summary")
async def get_call_summary(call_id: str):
    """Саммари и квалификация конкретного звонка."""
    session = await call_manager.get_call(call_id)
    if not session:
        raise HTTPException(status_code=404, detail="Call not found")
    return {
        "call_id": call_id,
        "summary": session.summary,
        "client_status": session.client_status,
        "transcript": session.transcript,
    }


@router.get("/api/v1/calls/{call_id}")
async def get_call(call_id: str):
    """Детали звонка."""
    session = await call_manager.get_call(call_id)
    if not session:
        raise HTTPException(status_code=404, detail="Call not found")
    return {
        "call_id": session.call_id,
        "phone": session.phone_number,
        "status": session.status.value,
        "step": session.current_step,
        "transcript": session.transcript,
        "client_status": session.client_status,
        "summary": session.summary,
    }


@router.post("/api/v1/calls/{call_id}/end")
async def end_call(call_id: str):
    """Завершить звонок."""
    session = await call_manager.end_call(call_id)
    if not session:
        raise HTTPException(status_code=404, detail="Call not found")
    return {"call_id": session.call_id, "status": "completed", "summary": session.summary}


@router.get("/api/v1/stats")
async def get_stats():
    """Статистика системы."""
    return await call_manager.get_stats()


# === WebSocket: real-time audio stream с AI-обработкой ===

@router.websocket("/ws/audio/{call_id}")
async def audio_websocket(websocket: WebSocket, call_id: str):
    """
    WebSocket для потоковой передачи аудио с AI-диалогом.

    Клиент отправляет бинарные чанки аудио (PCM 8kHz 16bit mono).
    Сервер:
    - распознаёт речь (ASR)
    - классифицирует намерение через Yandex GPT
    - генерирует AI-ответ с учётом базы знаний
    - синтезирует ответ (TTS) и отправляет обратно
    - после завершения генерирует саммари и квалифицирует клиента
    """
    await websocket.accept()
    logger.info(f"WebSocket connected: call_id={call_id}")

    session = await call_manager.get_call(call_id)
    if not session:
        await websocket.send_json({"error": "Call not found"})
        await websocket.close()
        return

    scenario = scenario_manager.get_scenario(session.scenario_id)

    pipeline = AudioPipeline(
        asr_service=asr_service,
        tts_service=tts_service,
    )

    # Voice config set by client via {action:"config"} message
    tts_voice_config: dict = {}

    async def synthesize_response(text: str) -> bytes:
        provider = tts_voice_config.get("provider", "yandex")
        voice = tts_voice_config.get("voice") or None
        if provider == "salutespeech":
            return await salutespeech_tts_service.synthesize(text=text, voice=voice)
        return await tts_service.synthesize(
            text=text,
            voice=voice,
            role=tts_voice_config.get("role") or None,
            speed=float(tts_voice_config.get("speed") or 1.0) or None,
        )

    async def stream_tts_to_ws(text: str):
        """Стриминг TTS: шлём аудиочанки клиенту по мере поступления от API."""
        provider = tts_voice_config.get("provider", "yandex")
        voice = tts_voice_config.get("voice") or None
        pipeline._is_speaking = True
        try:
            if provider == "salutespeech":
                # SaluteSpeech не поддерживает стриминг — отдаём одним куском
                audio = await salutespeech_tts_service.synthesize(text=text, voice=voice)
                await websocket.send_bytes(audio)
            else:
                async for chunk in tts_service.synthesize_stream(
                    text=text,
                    voice=voice,
                    role=tts_voice_config.get("role") or None,
                    speed=float(tts_voice_config.get("speed") or 1.0) or None,
                ):
                    await websocket.send_bytes(chunk)
        except Exception as tts_err:
            logger.warning(f"TTS stream failed, session continues: {tts_err}")
            await websocket.send_json({"type": "interrupt"})
        finally:
            pipeline._is_speaking = False

    try:
        while True:
            data = await websocket.receive()

            if "bytes" in data:
                result = await pipeline.process_chunk(data["bytes"])
                if not result:
                    continue

                if result["type"] == "recognition":
                    text = result.get("text", "").strip()
                    if not text:
                        continue

                    await call_manager.add_to_transcript(call_id, "client", text)
                    await websocket.send_json({"type": "recognition", "text": text})

                    # Роутинг: текущий шаг
                    current_step_id = session.current_step
                    current_step = scenario.steps.get(current_step_id)

                    if current_step and current_step.is_final:
                        break

                    # KB-поиск запускаем сразу, параллельно с подготовкой промпта
                    kb_task = asyncio.create_task(kb_service.search(text))

                    ai_config = ai_config_manager.get()
                    if scenario.system_prompt and len(ai_config.get("system_prompt", "")) < 200:
                        ai_config = {**ai_config, "system_prompt": scenario.system_prompt}

                    kb_context = await kb_task

                    # Один GPT-вызов: intent + ответ одновременно
                    try:
                        intent, response_text = await dialogue_engine.generate_with_intent(
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

                    # Роутинг по возвращённому intent
                    next_step_id = None
                    if current_step:
                        if intent == "positive":
                            next_step_id = current_step.on_positive
                        elif intent == "negative":
                            next_step_id = current_step.on_negative
                        elif intent == "objection":
                            next_step_id = current_step.on_objection or current_step.on_unknown
                        else:
                            next_step_id = current_step.on_unknown or current_step_id

                    if next_step_id:
                        await call_manager.update_step(call_id, next_step_id)
                        next_step = scenario.steps.get(next_step_id, current_step)
                    else:
                        next_step = current_step

                    await call_manager.add_to_transcript(call_id, "robot", response_text)
                    await websocket.send_json({
                        "type": "intent", "intent": intent,
                    })
                    await websocket.send_json({
                        "type": "response",
                        "text": response_text,
                        "intent": intent,
                        "step": next_step.id if next_step else current_step_id,
                    })

                    # Стриминг TTS: первый аудиочанк клиенту сразу как он придёт от API
                    await stream_tts_to_ws(response_text)

                    # Проверяем финальность следующего шага
                    if next_step and next_step.is_final:
                        break

                elif result["type"] == "interrupt":
                    await websocket.send_json({"type": "interrupt"})

            elif "text" in data:
                msg = json.loads(data["text"])
                if msg.get("action") == "config":
                    tts_voice_config.update(msg)
                    logger.info(f"TTS config updated: {tts_voice_config}")
                elif msg.get("action") == "speak":
                    text = msg.get("text", "")
                    await call_manager.add_to_transcript(call_id, "robot", text)
                    await stream_tts_to_ws(text)
                elif msg.get("action") == "end":
                    break

    except WebSocketDisconnect:
        logger.info(f"WebSocket disconnected: call_id={call_id}")
    except Exception as e:
        logger.error(f"WebSocket error: {e}")
    finally:
        # Генерируем саммари и квалификацию клиента
        session = await call_manager.get_call(call_id)
        if session and session.transcript:
            try:
                summary = await call_analyzer.generate_summary(session.transcript, scenario)
                qualification = await call_analyzer.qualify_client(session.transcript)
                await call_manager.end_call(
                    call_id,
                    client_status=qualification.get("status", "unknown"),
                    summary=summary,
                )
                logger.info(
                    f"Call analyzed: {call_id} | status={qualification.get('status')} | "
                    f"summary_len={len(summary)}"
                )
            except Exception as e:
                logger.error(f"Post-call analysis failed for {call_id}: {e}")
                await call_manager.end_call(call_id)
        else:
            await call_manager.end_call(call_id)
