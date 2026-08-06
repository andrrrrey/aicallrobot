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
    # Потоковый ASR (gRPC v3): аудио распознаётся во время речи, финал форсируется
    # по нашему VAD → нет пост-паузового раунд-трипа. false = REST v1 (как было).
    asr_streaming: bool = False

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

    # === VAD / эндпоинтинг (определение конца реплики собеседника) ===
    # Ключевые параметры «живости» диалога. Пауза адаптивная: после обычной
    # речи ждём vad_end_pause_sec, после очень короткой реплики (< порога
    # vad_short_utterance_ms) — vad_end_pause_short_sec, чтобы не обрывать
    # «да…», «алло…». Меньше значения = быстрее ответ, но выше риск перебить
    # человека на естественной паузе.
    vad_end_pause_sec: float = 0.6         # пауза после обычной речи (сек)
    vad_end_pause_short_sec: float = 0.9   # пауза после короткой реплики (сек)
    vad_short_utterance_ms: int = 700      # граница «короткой» речи (мс)
    vad_silence_threshold: int = 500       # порог амплитуды тишины
    vad_interrupt_duration_ms: int = 200   # мс речи клиента для barge-in

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
    # Потоковый GPT→TTS для v1: озвучка начинается с первого готового предложения,
    # не дожидаясь полного ответа. Kill-switch: GPT_STREAM_TTS=false → старое поведение.
    gpt_stream_tts: bool = True

    # Knowledge Base (ChromaDB)
    knowledge_base_dir: str = "/app/knowledge_base"

    # AI config and call history persistence
    ai_config_file: str = "/app/data/ai_config.json"
    call_history_dir: str = "/app/data/calls"
    script_corrections_file: str = "/app/data/script_corrections.json"
    # Переопределяемые из админки настройки диалера/антиспама (JSON)
    dialer_settings_file: str = "/app/data/dialer_settings.json"

    # Script corrections (v2 answer-override layer)
    # Максимальная cosine-дистанция, при которой правка считается совпадением.
    script_correction_threshold: float = 0.25

    # SaluteSpeech (Sber SmartSpeech)
    salutespeech_auth_key: str = ""
    salutespeech_scope: str = "SALUTE_SPEECH_PERS"
    salutespeech_voice: str = "Bys"

    # fish.audio (TTS + собственные голосовые модели)
    fishaudio_api_key: str = ""
    fishaudio_model: str = ""   # reference_id голоса по умолчанию

    # === Телефония (интеграция с Asterisk заказчика) ===

    # PostgreSQL (база клиентов и состояние обзвона).
    # ai-robot в host-режиме сети → БД доступна по 127.0.0.1 (postgres публикует порт).
    database_url: str = "postgresql+asyncpg://robot:robot@127.0.0.1:5432/airobot"

    # SIP-бэкенд: pjsua2 (устойчивый, продакшн) | pyvoip (лёгкий, запасной)
    sip_backend: str = "pjsua2"

    # SIP-регистрация робота как внутреннего абонента (экстеншена)
    sip_server: str = ""           # адрес Asterisk (через VPN-туннель), напр. 192.168.0.110
    sip_extension: str = ""        # логин экстеншена
    sip_password: str = ""
    sip_context: str = ""          # контекст (если требуется)
    sip_codec: str = "pcma"        # G.711 alaw
    sip_local_ip: str = ""         # IP, который pyVoIP анонсирует для RTP (за Docker/VPN)
    sip_debug: bool = False        # подробный лог SIP-сообщений pyVoIP (для диагностики)

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

    # === Антиспам-темп набора (защита от блокировки линии оператором) ===
    # Эти значения — только дефолты; фактические берутся из dialer_settings.json,
    # который редактируется в админке (вкладка «Диалер»).
    #
    # Минимальная пауза между инициациями звонков на одном маршруте. При > 0 диалер
    # набирает по одному номеру за раз на маршрут, выдерживая паузу + случайный
    # разброс (jitter) — чтобы оператор не принял поток за спам. 0 = без паузы.
    dial_min_interval_sec: float = 12.0
    # Случайная добавка к паузе (0..jitter), рандомизирует темп набора.
    dial_jitter_sec: float = 8.0
    # Дневной лимит инициированных звонков на маршрут (по МСК-суткам). 0 = без лимита.
    dial_daily_limit_per_route: int = 0
    # Cooldown на конкретный номер: не набирать один и тот же номер чаще, чем раз
    # в N часов (страховка от повторного «долбления»). 0 = выключено.
    dial_number_cooldown_hours: float = 24.0

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"
        # Игнорируем неизвестные переменные из .env/окружения, чтобы лишние или
        # новые ключи не роняли старт приложения (extra_forbidden).
        extra = "ignore"


@lru_cache()
def get_settings() -> Settings:
    return Settings()
