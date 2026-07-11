"""AI Robot for outbound calls — main application."""

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from contextlib import asynccontextmanager
from loguru import logger
from pathlib import Path

from app.core.config import get_settings
from app.core.logging import setup_logging
from app.api.routes import router, kb_service


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    setup_logging()
    settings = get_settings()
    logger.info(f"Starting {settings.app_name} ({settings.app_env})")
    logger.info(f"Max concurrent calls: {settings.max_concurrent_calls}")
    logger.info(f"TTS voice: {settings.tts_voice}")
    logger.info(f"ASR model: {settings.asr_model}")
    logger.info(f"Demo UI: http://0.0.0.0:8000/demo")

    if not settings.yandex_api_key or settings.yandex_api_key == "your_secret_api_key_here":
        logger.warning("YANDEX_API_KEY is not configured! Set it in .env")

    # Инициализация директорий для базы знаний и истории звонков
    for d in [settings.knowledge_base_dir, settings.call_history_dir]:
        Path(d).mkdir(parents=True, exist_ok=True)
    Path(settings.ai_config_file).parent.mkdir(parents=True, exist_ok=True)

    logger.info(f"Admin UI: http://0.0.0.0:8000/admin")
    logger.info(f"Knowledge base: {settings.knowledge_base_dir}")

    import asyncio
    asyncio.create_task(kb_service.warmup())

    # База клиентов (PostgreSQL): создаём схему, если БД доступна
    try:
        from app.services.db import init_db
        await init_db()
    except Exception as e:
        logger.warning(f"PostgreSQL недоступен на старте (телефония/кампании отключены): {e}")

    # SIP-агент: регистрируем робота как экстеншен на АТС заказчика
    try:
        from app.services.telephony.sip_agent import sip_agent
        sip_agent.start(asyncio.get_running_loop())
    except Exception as e:
        logger.warning(f"SIP-агент не запущен: {e}")

    yield

    # Shutdown
    logger.info("Shutting down AI Robot")
    try:
        from app.services.dialer import dialer
        await dialer.shutdown()
    except Exception:
        pass
    try:
        from app.services.telephony.sip_agent import sip_agent
        sip_agent.stop()
    except Exception:
        pass
    try:
        from app.services.db import dispose_db
        await dispose_db()
    except Exception:
        pass


app = FastAPI(
    title="AI Robot — Исходящие обзвоны",
    description="Облачный AI-робот для автоматизации исходящих звонков",
    version="0.1.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(router)

# Serve demo UI
static_dir = Path(__file__).parent / "static"
if static_dir.exists():
    app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")


@app.get("/demo")
async def demo_page():
    return FileResponse(str(static_dir / "demo.html"))


@app.get("/admin")
async def admin_page():
    return FileResponse(str(static_dir / "admin.html"))


@app.get("/test")
async def test_page():
    # Та же страница, что и /admin, но в режиме «только Тест диалога»
    # (вкладки и навигация скрываются на стороне клиента по пути /test).
    return FileResponse(str(static_dir / "admin.html"))
