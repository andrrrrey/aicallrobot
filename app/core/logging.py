import sys
from loguru import logger
from app.core.config import get_settings


def setup_logging():
    settings = get_settings()

    logger.remove()

    log_format = (
        "<green>{time:YYYY-MM-DD HH:mm:ss.SSS}</green> | "
        "<level>{level: <8}</level> | "
        "<cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> | "
        "{message}"
    )

    logger.add(
        sys.stdout,
        format=log_format,
        level=settings.log_level,
        colorize=True,
    )

    logger.add(
        "/app/logs/ai-robot.log",
        format=log_format,
        level=settings.log_level,
        rotation="50 MB",
        retention="30 days",
        compression="gz",
    )

    logger.add(
        "/app/logs/calls.log",
        format=log_format,
        level="INFO",
        rotation="50 MB",
        retention="90 days",
        filter=lambda record: "call" in record["extra"],
    )

    return logger
