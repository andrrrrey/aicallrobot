from pydantic_settings import BaseSettings
from functools import lru_cache


class Settings(BaseSettings):
    # Yandex Cloud
    yandex_api_key: str = ""
    yandex_folder_id: str = ""

    # SpeechKit
    tts_voice: str = "alena"
    tts_speed: float = 1.0
    tts_emotion: str = "neutral"
    asr_language: str = "ru-RU"
    asr_model: str = "general:rc"

    # Application
    app_name: str = "AI-Robot"
    app_env: str = "production"
    log_level: str = "INFO"
    max_concurrent_calls: int = 3

    # Redis
    redis_url: str = "redis://redis:6379/0"

    # Audio
    audio_sample_rate: int = 8000
    audio_channels: int = 1
    recordings_dir: str = "/app/recordings"

    # Scenarios
    scenarios_dir: str = "/app/scenarios"
    default_scenario: str = "default"

    # SpeechKit endpoints
    speechkit_tts_url: str = "https://tts.api.cloud.yandex.net/speech/v1/tts:synthesize"
    speechkit_stt_streaming_url: str = "stt.api.cloud.yandex.net:443"

    # Yandex GPT
    yandex_gpt_model: str = "yandexgpt-lite/latest"  # lite = 3-5x faster; override to yandexgpt/latest for pro
    yandex_gpt_temperature: float = 0.6
    yandex_gpt_max_tokens: int = 250  # 1-2 предложения ≈ 40-80 токенов; 250 с запасом

    # Knowledge Base (ChromaDB)
    knowledge_base_dir: str = "/app/knowledge_base"

    # AI config and call history persistence
    ai_config_file: str = "/app/data/ai_config.json"
    call_history_dir: str = "/app/data/calls"
    script_corrections_file: str = "/app/data/script_corrections.json"

    # Script corrections (v2 answer-override layer)
    # Максимальная cosine-дистанция, при которой правка считается совпадением.
    script_correction_threshold: float = 0.25

    # SaluteSpeech (Sber SmartSpeech)
    salutespeech_auth_key: str = ""
    salutespeech_scope: str = "SALUTE_SPEECH_PERS"
    salutespeech_voice: str = "Bys"

    # === Телефония (интеграция с Asterisk заказчика) ===

    # PostgreSQL (база клиентов и состояние обзвона).
    # ai-robot в host-режиме сети → БД доступна по 127.0.0.1 (postgres публикует порт).
    database_url: str = "postgresql+asyncpg://robot:robot@127.0.0.1:5432/airobot"

    # SIP-регистрация робота как внутреннего абонента (экстеншена)
    sip_server: str = ""           # адрес Asterisk (через VPN-туннель), напр. 192.168.0.110
    sip_extension: str = ""        # логин экстеншена
    sip_password: str = ""
    sip_context: str = ""          # контекст (если требуется)
    sip_codec: str = "pcma"        # G.711 alaw
    sip_local_ip: str = ""         # IP, который pyVoIP анонсирует для RTP (за Docker/VPN)

    # HTTP-API res24.php (основное инициирование + статус/CDR)
    res24_base_url: str = "http://192.168.0.110"
    res24_login: str = "robott"
    res24_secret: str = ""
    robot_extension: str = ""      # экстеншен робота для параметра from в res24 call

    # AMI (опционально — дополнительные события)
    ami_host: str = "192.168.0.110"
    ami_port: int = 5038
    ami_user: str = "robott"
    ami_secret: str = ""

    # Формат набора: национальный префикс для res24 `to`
    dial_national_prefix: str = "8"

    # Лимиты одновременных звонков по маршруту (t2 = 1 линия, местные — до 30)
    route_limit_t2: int = 1
    route_limit_local: int = 30

    # Диалер
    dialer_enabled: bool = False
    dialer_poll_interval: float = 5.0
    max_retries: int = 3
    retry_backoff_base: float = 300.0   # базовая пауза перед перезвоном (сек)

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"
        # Игнорируем неизвестные переменные из .env/окружения, чтобы лишние или
        # новые ключи не роняли старт приложения (extra_forbidden).
        extra = "ignore"


@lru_cache()
def get_settings() -> Settings:
    return Settings()
