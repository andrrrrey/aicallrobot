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
    yandex_gpt_model: str = "yandexgpt/latest"
    yandex_gpt_temperature: float = 0.6
    yandex_gpt_max_tokens: int = 500

    # Knowledge Base (ChromaDB)
    knowledge_base_dir: str = "/app/knowledge_base"

    # AI config and call history persistence
    ai_config_file: str = "/app/data/ai_config.json"
    call_history_dir: str = "/app/data/calls"

    # SaluteSpeech (Sber SmartSpeech)
    salutespeech_auth_key: str = ""
    salutespeech_scope: str = "SALUTE_SPEECH_PERS"
    salutespeech_voice: str = "Neyra"

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"


@lru_cache()
def get_settings() -> Settings:
    return Settings()
