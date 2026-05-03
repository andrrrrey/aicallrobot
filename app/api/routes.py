"""API routes for the AI robot."""

from fastapi import APIRouter, HTTPException, WebSocket, WebSocketDisconnect
from pydantic import BaseModel
from loguru import logger

from app.services.tts import TTSService
from app.services.asr import ASRService
from app.services.call_manager import CallManager
from app.services.scenario_engine import ScenarioManager
from app.services.audio_pipeline import AudioPipeline

router = APIRouter()

# Singletons
tts_service = TTSService()
asr_service = ASRService()
call_manager = CallManager()
scenario_manager = ScenarioManager()


# === Models ===

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


@router.get("/api/v1/calls")
async def list_calls():
    """Список активных звонков."""
    return {"calls": await call_manager.list_active()}


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


# === WebSocket: real-time audio stream ===

@router.websocket("/ws/audio/{call_id}")
async def audio_websocket(websocket: WebSocket, call_id: str):
    """
    WebSocket для потоковой передачи аудио.

    Клиент отправляет бинарные чанки аудио (PCM 8kHz 16bit mono).
    Сервер отвечает JSON-сообщениями с результатами распознавания
    и бинарными чанками синтезированной речи.
    """
    await websocket.accept()
    logger.info(f"WebSocket connected: call_id={call_id}")

    session = await call_manager.get_call(call_id)
    if not session:
        await websocket.send_json({"error": "Call not found"})
        await websocket.close()
        return

    pipeline = AudioPipeline(
        asr_service=asr_service,
        tts_service=tts_service,
    )

    try:
        while True:
            data = await websocket.receive()

            if "bytes" in data:
                # Входящий аудиочанк от клиента
                result = await pipeline.process_chunk(data["bytes"])
                if result:
                    if result["type"] == "recognition":
                        text = result.get("text", "")
                        await call_manager.add_to_transcript(call_id, "client", text)
                        await websocket.send_json({
                            "type": "recognition",
                            "text": text,
                        })

                    elif result["type"] == "interrupt":
                        await websocket.send_json({"type": "interrupt"})

            elif "text" in data:
                # Команда от клиента (JSON)
                import json
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
        await call_manager.end_call(call_id)
