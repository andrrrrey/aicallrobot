"""Общие типы для SIP-бэкендов (pyVoIP / pjsua2)."""

from dataclasses import dataclass


@dataclass
class CallResult:
    status: str                       # answered / no_answer / busy / failed
    client_status: str = "unknown"    # квалификация после разговора
    summary: str = ""
    duration: float = 0.0
    reason: str = ""                  # причина недозвона (напр. "SIP 403 Forbidden")
