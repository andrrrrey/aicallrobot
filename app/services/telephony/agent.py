"""Выбор SIP-бэкенда: pjsua2 (по умолчанию) или pyVoIP (запасной).

Управляется настройкой ``SIP_BACKEND`` (``pjsua2`` | ``pyvoip``). Экспортирует
единый синглтон ``sip_agent`` с одинаковым интерфейсом (``start/stop/ready/
originate`` + ``CallResult``), чтобы вызывающий код (main, dialer, routes) не
зависел от конкретной библиотеки.
"""

from loguru import logger

from app.core.config import get_settings


def _select_agent():
    backend = (get_settings().sip_backend or "pjsua2").lower()
    if backend == "pyvoip":
        from app.services.telephony.sip_agent import sip_agent as agent
        logger.info("SIP-бэкенд: pyVoIP")
        return agent
    from app.services.telephony.pjsua_agent import pjsua_agent as agent
    logger.info("SIP-бэкенд: pjsua2")
    return agent


sip_agent = _select_agent()
