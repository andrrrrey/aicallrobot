"""API routes for the AI robot."""

import json
from pathlib import Path
from fastapi import APIRouter, HTTPException, WebSocket, WebSocketDisconnect, UploadFile, File
from fastapi.responses import Response
from pydantic import BaseModel
from loguru import logger

from app.services.tts import TTSService
from app.services.salutespeech_tts import SaluteSpeechTTSService
from app.services.knowledge_base import extract_text
from app.services.script_v2_data import SCRIPT as V2_SCRIPT
from app.services.script_corrections import parse_correction_table
from app.services.conversation import ConversationDriver

# Общие синглтоны сервисов (см. app/services/registry.py)
from app.services.registry import (
    tts_service,
    salutespeech_tts_service,
    asr_service,
    call_manager,
    scenario_manager,
    gpt_service,
    kb_service,
    dialogue_engine,
    call_analyzer,
    ai_config_manager,
    corrections_service,
    script_v2_engine,
)

router = APIRouter()


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
    algo_version: str = "v1"


class AIConfigUpdate(BaseModel):
    system_prompt: str
    scenario_context: str = ""


class ChatTestRequest(BaseModel):
    message: str
    history: list[dict] = []


class V2ChatStartRequest(BaseModel):
    session_id: str


class V2ChatTurnRequest(BaseModel):
    session_id: str
    user_text: str


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


# === Script corrections (слой правок ответов v2) ===

class CorrectionRow(BaseModel):
    trigger: str
    current_answer: str = ""
    correct_answer: str
    phase: str = "any"
    enabled: bool = True


class CorrectionTestRequest(BaseModel):
    user_text: str
    phase: str = "secretary"


@router.get("/api/v1/script-corrections")
async def list_corrections():
    """Список правок скрипта."""
    return {"corrections": corrections_service.list()}


@router.post("/api/v1/script-corrections")
async def add_correction(row: CorrectionRow):
    """Добавляет одну правку."""
    return corrections_service.add(row.model_dump())


@router.put("/api/v1/script-corrections/{item_id}")
async def update_correction(item_id: str, row: CorrectionRow):
    """Изменяет правку по id."""
    updated = corrections_service.update(item_id, row.model_dump())
    if not updated:
        raise HTTPException(status_code=404, detail="Правка не найдена")
    return updated


@router.delete("/api/v1/script-corrections/{item_id}")
async def delete_correction(item_id: str):
    """Удаляет правку по id."""
    if not corrections_service.delete(item_id):
        raise HTTPException(status_code=404, detail="Правка не найдена")
    return {"deleted": item_id}


@router.post("/api/v1/script-corrections/upload")
async def upload_corrections(file: UploadFile = File(...), mode: str = "append"):
    """Загружает таблицу правок (.xlsx/.csv). mode=append|replace."""
    ext = Path(file.filename or "").suffix.lower()
    if ext not in (".xlsx", ".csv"):
        raise HTTPException(status_code=400, detail="Поддерживаются только .xlsx и .csv")
    if mode not in ("append", "replace"):
        raise HTTPException(status_code=400, detail="mode должен быть append или replace")
    try:
        content = await file.read()
        rows = parse_correction_table(content, file.filename or "file.csv")
        if not rows:
            raise HTTPException(status_code=400, detail="В файле не найдено ни одной правки")
        imported = corrections_service.import_rows(rows, mode=mode)
        return {"imported": imported, "total": len(corrections_service.list())}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"upload_corrections error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/api/v1/script-corrections/export")
async def export_corrections(fmt: str = "xlsx"):
    """Выгружает текущие правки файлом для редактирования (xlsx|csv)."""
    if fmt not in ("xlsx", "csv"):
        raise HTTPException(status_code=400, detail="fmt должен быть xlsx или csv")
    data, content_type, filename = corrections_service.export_rows(fmt)
    return Response(
        content=data,
        media_type=content_type,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.post("/api/v1/script-corrections/test")
async def test_correction(req: CorrectionTestRequest):
    """Отладка: какие правки сработали бы на реплику (с дистанциями)."""
    return {"matches": corrections_service.preview(req.user_text, req.phase)}


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


# === AI Chat v2 (Script-based) ===

@router.post("/api/v1/ai/chat_v2/start")
async def chat_v2_start(request: V2ChatStartRequest):
    """Создаёт сессию скриптового диалога v2 и возвращает первую реплику."""
    try:
        script_v2_engine.create_session(request.session_id)
        result = script_v2_engine.greeting(request.session_id)
        return result
    except Exception as e:
        logger.error(f"chat_v2_start error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/api/v1/ai/chat_v2/turn")
async def chat_v2_turn(request: V2ChatTurnRequest):
    """Обрабатывает одну реплику пользователя в скриптовом диалоге v2."""
    try:
        result = await script_v2_engine.process_turn(request.session_id, request.user_text)
        return result
    except Exception as e:
        logger.error(f"chat_v2_turn error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.delete("/api/v1/ai/chat_v2/session/{session_id}")
async def chat_v2_delete_session(session_id: str):
    """Удаляет сессию скриптового диалога v2."""
    script_v2_engine.delete_session(session_id)
    return {"ok": True}


# === Calls ===

@router.post("/api/v1/calls/start")
async def start_call(request: StartCallRequest):
    """Начать звонок."""
    try:
        session = await call_manager.start_call(
            phone_number=request.phone_number,
            scenario_id=request.scenario_id,
            algo_version=request.algo_version,
        )
        if request.algo_version == "v2":
            greeting_text = V2_SCRIPT["greeting"]
            v2_greeting = script_v2_engine.greeting(session.call_id)
            greeting_text = v2_greeting.get("robot_text", greeting_text)
            await call_manager.add_to_transcript(session.call_id, "robot", greeting_text)
        else:
            scenario = scenario_manager.get_scenario(request.scenario_id)
            greeting_text = scenario.greeting
            if greeting_text:
                await call_manager.add_to_transcript(session.call_id, "robot", greeting_text)
        return {
            "call_id": session.call_id,
            "status": session.status.value,
            "greeting": greeting_text,
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


# === Кампании обзвона (телефония через Asterisk заказчика) ===

class CampaignCreate(BaseModel):
    name: str
    scenario_id: str = "default"
    algo_version: str = "v2"
    voice_config: dict = {}
    call_window_start: int = 0
    call_window_end: int = 24


@router.post("/api/v1/campaigns")
async def create_campaign(req: CampaignCreate):
    """Создать кампанию обзвона."""
    from app.services import campaign_service
    campaign_id = await campaign_service.create_campaign(
        name=req.name,
        scenario_id=req.scenario_id,
        algo_version=req.algo_version,
        voice_config=req.voice_config,
        call_window_start=req.call_window_start,
        call_window_end=req.call_window_end,
    )
    return {"campaign_id": campaign_id}


@router.get("/api/v1/campaigns")
async def list_campaigns():
    """Список кампаний."""
    from app.services import campaign_service
    return {"campaigns": await campaign_service.list_campaigns()}


@router.get("/api/v1/campaigns/{campaign_id}/progress")
async def campaign_progress(campaign_id: int):
    """Прогресс кампании: всего / по статусам / заинтересованы."""
    from app.services import campaign_service
    return await campaign_service.campaign_progress(campaign_id)


@router.post("/api/v1/campaigns/{campaign_id}/import")
async def import_clients(campaign_id: int, file: UploadFile = File(...)):
    """Загрузить базу клиентов (.csv/.xlsx) в кампанию."""
    from app.services import campaign_service
    ext = Path(file.filename or "").suffix.lower()
    if ext not in (".csv", ".xlsx"):
        raise HTTPException(status_code=400, detail="Поддерживаются только .csv и .xlsx")
    try:
        content = await file.read()
        rows = campaign_service.parse_clients_table(content, file.filename or "clients.csv")
        if not rows:
            raise HTTPException(status_code=400, detail="В файле не найдено ни одного клиента")
        imported = await campaign_service.import_clients(campaign_id, rows)
        return {"imported": imported}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"import_clients error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/api/v1/campaigns/{campaign_id}/start")
async def start_campaign(campaign_id: int):
    """Запустить обзвон кампании."""
    from app.services.dialer import dialer
    await dialer.start_campaign(campaign_id)
    return {"campaign_id": campaign_id, "status": "running"}


@router.post("/api/v1/campaigns/{campaign_id}/stop")
async def stop_campaign(campaign_id: int):
    """Остановить обзвон кампании."""
    from app.services.dialer import dialer
    await dialer.stop_campaign(campaign_id)
    return {"campaign_id": campaign_id, "status": "paused"}


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

    # Транспорт: браузерный WebSocket. Диалоговую логику ведёт ConversationDriver.
    async def send_audio(chunk: bytes):
        await websocket.send_bytes(chunk)

    async def send_event(event: dict):
        await websocket.send_json(event)

    driver = ConversationDriver(
        call_id=call_id,
        session=session,
        scenario=scenario,
        send_audio=send_audio,
        send_event=send_event,
    )

    try:
        while True:
            data = await websocket.receive()

            if "bytes" in data:
                await driver.feed_chunk(data["bytes"])
                if driver.should_end:
                    break

            elif "text" in data:
                msg = json.loads(data["text"])
                action = msg.get("action")
                if action == "config":
                    driver.set_tts_config(msg)
                elif action == "speak":
                    await driver.speak(msg.get("text", ""))
                elif action == "switch_to_lpr":
                    await driver.switch_to_lpr()
                elif action == "end":
                    break

    except WebSocketDisconnect:
        logger.info(f"WebSocket disconnected: call_id={call_id}")
    except Exception as e:
        logger.error(f"WebSocket error: {e}")
    finally:
        await driver.finalize()
