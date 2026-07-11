"""Общие синглтоны сервисов.

Вынесены из ``app/api/routes.py``, чтобы одни и те же экземпляры сервисов могли
использоваться и HTTP/WebSocket-слоем, и телефонным модулем (SIP/диалер), без
дублирования состояния (например, активных звонков в ``CallManager`` или сессий
скриптового движка v2).
"""

from app.services.tts import TTSService
from app.services.salutespeech_tts import SaluteSpeechTTSService
from app.services.asr import ASRService
from app.services.call_manager import CallManager
from app.services.scenario_engine import ScenarioManager
from app.services.yandex_gpt import YandexGPTService
from app.services.knowledge_base import KnowledgeBaseService
from app.services.dialogue_engine import DialogueEngine
from app.services.call_analyzer import CallAnalyzer
from app.services.ai_config_manager import AIConfigManager
from app.services.script_dialogue_v2 import ScriptDialogueV2
from app.services.script_corrections import ScriptCorrectionsService

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
corrections_service = ScriptCorrectionsService()
script_v2_engine = ScriptDialogueV2(gpt_service, corrections_service)
