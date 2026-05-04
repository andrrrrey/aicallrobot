"""API routes for the AI robot."""

import asyncio
import json
import time
import uuid
from pathlib import Path
from fastapi import APIRouter, HTTPException, WebSocket, WebSocketDisconnect, UploadFile, File
from pydantic import BaseModel
from loguru import logger

from app.services.tts import TTSService
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


class FinishSessionRequest(BaseModel):
    transcript: list[dict]   # [{"role": "user"|"assistant", "text": "..."}]
    mode: str = "text"       # "voice" | "text"
    started_at: float | None = None


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
    """Загружает файл (.txt/.md/.pdf/.docx) в базу знаний."""
    ext = Path(file.filename or "").suffix.lower()
    if ext not in (".txt", ".md", ".pdf", ".docx"):
        raise HTTPException(status_code=400, detail="Поддерживаются только .txt, .md, .pdf, .docx")

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


@router.post("/api/v1/ai/scenario-upload")
async def upload_scenario_file(file: UploadFile = File(...)):
    """
    Загружает файл сценария (.txt/.md/.pdf/.docx) и возвращает его текстовое содержимое.
    Используется для заполнения поля «Контекст сценария» в настройках ИИ.
    """
    ext = Path(file.filename or "").suffix.lower()
    if ext not in (".txt", ".md", ".pdf", ".docx"):
        raise HTTPException(status_code=400, detail="Поддерживаются только .txt, .md, .pdf, .docx")
    try:
        content = await file.read()
        text = extract_text(content, file.filename or "file.txt")
        return {"text": text, "filename": file.filename}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"upload_scenario_file error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


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
    """История завершённых звонков и тестовых сессий (из памяти + с диска)."""
    from app.core.config import get_settings
    settings = get_settings()

    # In-memory completed calls
    items = await call_manager.list_completed()

    # Also read persisted JSON files (calls saved to disk + test sessions)
    history_dir = Path(settings.call_history_dir)
    in_memory_ids = {c["call_id"] for c in items}

    if history_dir.exists():
        for jf in sorted(history_dir.glob("*.json"), key=lambda f: f.stat().st_mtime, reverse=True):
            try:
                with open(jf, "r", encoding="utf-8") as f:
                    data = json.load(f)
                item_id = data.get("call_id") or data.get("session_id", "")
                if item_id in in_memory_ids:
                    continue  # already included from memory
                items.append({
                    "call_id": item_id,
                    "type": data.get("type", "call"),
                    "phone": data.get("phone_number", "—"),
                    "scenario_id": data.get("scenario_id", ""),
                    "mode": data.get("mode", ""),
                    "status": data.get("status", "completed"),
                    "client_status": data.get("client_status", "unknown"),
                    "duration": data.get("duration", 0),
                    "messages": len(data.get("transcript", [])),
                    "summary": data.get("summary", ""),
                    "started_at": data.get("started_at", 0),
                })
            except Exception as e:
                logger.warning(f"Failed to read history file {jf}: {e}")

    items.sort(key=lambda x: x.get("started_at", 0), reverse=True)
    return {"calls": items}


@router.post("/api/v1/ai/finish-session")
async def finish_test_session(request: FinishSessionRequest):
    """
    Завершает тестовую сессию диалога с ИИ:
    генерирует саммари + квалификацию, сохраняет на диск.
    """
    from app.core.config import get_settings
    settings = get_settings()

    if not request.transcript:
        raise HTTPException(status_code=400, detail="Транскрипт пуст")

    # Convert chat history format to call transcript format
    transcript = [
        {"role": "robot" if e.get("role") == "assistant" else "client",
         "text": e.get("text", "")}
        for e in request.transcript
    ]

    # Generate summary and qualification using the same call_analyzer
    class _FakeScenario:
        name = "Тестовый диалог"

    try:
        summary = await call_analyzer.generate_summary(transcript, _FakeScenario())
        qualification = await call_analyzer.qualify_client(transcript)
    except Exception as e:
        logger.error(f"finish_test_session analysis error: {e}")
        summary = "Ошибка анализа"
        qualification = {"status": "unknown", "reasoning": ""}

    session_id = str(uuid.uuid4())
    now = time.time()
    started_at = request.started_at or (now - len(transcript) * 10)

    data = {
        "session_id": session_id,
        "call_id": session_id,
        "type": "test",
        "mode": request.mode,
        "status": "completed",
        "client_status": qualification.get("status", "unknown"),
        "summary": summary,
        "transcript": transcript,
        "started_at": started_at,
        "ended_at": now,
        "duration": int(now - started_at),
        "qualification_reasoning": qualification.get("reasoning", ""),
    }

    try:
        history_dir = Path(settings.call_history_dir)
        history_dir.mkdir(parents=True, exist_ok=True)
        with open(history_dir / f"{session_id}.json", "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.error(f"Failed to save test session: {e}")

    return {
        "session_id": session_id,
        "summary": summary,
        "client_status": qualification.get("status", "unknown"),
        "reasoning": qualification.get("reasoning", ""),
    }


@router.get("/api/v1/calls")
async def list_calls():
    """Список активных звонков."""
    return {"calls": await call_manager.list_active()}


@router.get("/api/v1/calls/{call_id}/summary")
async def get_call_summary(call_id: str):
    """Саммари и квалификация конкретного звонка или тестовой сессии."""
    from app.core.config import get_settings
    settings = get_settings()

    # Try in-memory call first
    session = await call_manager.get_call(call_id)
    if session:
        return {
            "call_id": call_id,
            "type": "call",
            "summary": session.summary,
            "client_status": session.client_status,
            "transcript": session.transcript,
        }

    # Fall back to disk (test sessions and persisted calls)
    history_dir = Path(settings.call_history_dir)
    json_file = history_dir / f"{call_id}.json"
    if json_file.exists():
        try:
            with open(json_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            # Normalize transcript: test sessions use role "user"/"assistant"
            transcript = data.get("transcript", [])
            for entry in transcript:
                if entry.get("role") == "assistant":
                    entry["role"] = "robot"
                elif entry.get("role") == "user":
                    entry["role"] = "client"
            return {
                "call_id": call_id,
                "type": data.get("type", "call"),
                "summary": data.get("summary", ""),
                "client_status": data.get("client_status", "unknown"),
                "transcript": transcript,
            }
        except Exception as e:
            logger.error(f"Failed to read session file {call_id}: {e}")

    raise HTTPException(status_code=404, detail="Call not found")


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

                    # Роутинг по сценарию
                    current_step_id = session.current_step
                    current_step = scenario.steps.get(current_step_id)

                    if current_step and current_step.is_final:
                        break

                    ai_config = ai_config_manager.get()

                    # KB-поиск и GPT (намерение + ответ) параллельно — минимальная задержка
                    try:
                        kb_context, (intent, response_text) = await asyncio.gather(
                            kb_service.search(text),
                            dialogue_engine.classify_and_respond(
                                user_text=text,
                                step=current_step,
                                transcript=session.transcript,
                                knowledge_context=[],
                                ai_config=ai_config,
                            ),
                        )
                    except Exception as e:
                        logger.error(f"AI response generation failed: {e}")
                        intent = "unknown"
                        kb_context = []
                        response_text = (
                            current_step.greeting
                            if current_step and current_step.greeting
                            else "Понял. Могу я уточнить подробности?"
                        )

                    await websocket.send_json({"type": "intent", "intent": intent})

                    # Определяем следующий шаг по намерению
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
                        "type": "response",
                        "text": response_text,
                        "intent": intent,
                        "step": next_step.id if next_step else current_step_id,
                    })

                    audio = await pipeline.speak(response_text)
                    await websocket.send_bytes(audio)

                    # Проверяем финальность следующего шага
                    if next_step and next_step.is_final:
                        break

                elif result["type"] == "interrupt":
                    await websocket.send_json({"type": "interrupt"})

            elif "text" in data:
                msg = json.loads(data["text"])
                if msg.get("action") == "speak":
                    text = msg.get("text", "")
                    audio = await pipeline.speak(text)
                    await call_manager.add_to_transcript(call_id, "robot", text)
                    await websocket.send_bytes(audio)
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
