"""Scenario engine: загрузка и управление сценариями диалогов."""

import os
import yaml
from pathlib import Path
from dataclasses import dataclass, field
from loguru import logger
from app.core.config import get_settings


@dataclass
class ScenarioStep:
    """Один шаг сценария."""
    id: str
    greeting: str = ""
    prompt: str = ""
    on_positive: str = ""     # ID следующего шага при позитивном ответе
    on_negative: str = ""     # при негативном
    on_objection: str = ""    # при возражении
    on_unknown: str = ""      # при непонятном ответе
    max_retries: int = 2
    is_final: bool = False
    transfer_to_operator: bool = False


@dataclass
class Scenario:
    """Сценарий обзвона."""
    id: str
    name: str
    description: str = ""
    greeting: str = ""
    system_prompt: str = ""
    steps: dict = field(default_factory=dict)  # id -> ScenarioStep
    objection_responses: dict = field(default_factory=dict)
    farewell_positive: str = "Спасибо за уделённое время! Всего доброго."
    farewell_negative: str = "Понял, спасибо за ответ. Хорошего дня!"


class ScenarioManager:
    """Управление сценариями: загрузка из YAML, выбор, навигация."""

    def __init__(self):
        self.settings = get_settings()
        self.scenarios: dict[str, Scenario] = {}
        self._load_scenarios()

    def _load_scenarios(self):
        """Загрузка сценариев из YAML-файлов."""
        scenarios_dir = Path(self.settings.scenarios_dir)
        if not scenarios_dir.exists():
            logger.warning(f"Scenarios directory not found: {scenarios_dir}")
            self._create_default_scenario()
            return

        for yaml_file in scenarios_dir.glob("*.yaml"):
            try:
                with open(yaml_file, "r", encoding="utf-8") as f:
                    data = yaml.safe_load(f)
                scenario = self._parse_scenario(data)
                self.scenarios[scenario.id] = scenario
                logger.info(f"Loaded scenario: {scenario.id} ({scenario.name})")
            except Exception as e:
                logger.error(f"Failed to load scenario {yaml_file}: {e}")

        if not self.scenarios:
            self._create_default_scenario()

    def _parse_scenario(self, data: dict) -> Scenario:
        """Парсинг YAML-данных в объект Scenario."""
        steps = {}
        for step_data in data.get("steps", []):
            step = ScenarioStep(
                id=step_data["id"],
                greeting=step_data.get("greeting", ""),
                prompt=step_data.get("prompt", ""),
                on_positive=step_data.get("on_positive", ""),
                on_negative=step_data.get("on_negative", ""),
                on_objection=step_data.get("on_objection", ""),
                on_unknown=step_data.get("on_unknown", ""),
                max_retries=step_data.get("max_retries", 2),
                is_final=step_data.get("is_final", False),
                transfer_to_operator=step_data.get("transfer_to_operator", False),
            )
            steps[step.id] = step

        return Scenario(
            id=data.get("id", "default"),
            name=data.get("name", "Без названия"),
            description=data.get("description", ""),
            greeting=data.get("greeting", ""),
            system_prompt=data.get("system_prompt", ""),
            steps=steps,
            objection_responses=data.get("objection_responses", {}),
            farewell_positive=data.get("farewell_positive", "Спасибо! Всего доброго."),
            farewell_negative=data.get("farewell_negative", "Понял, спасибо. Хорошего дня!"),
        )

    def _create_default_scenario(self):
        """Создаёт сценарий по умолчанию."""
        default = Scenario(
            id="default",
            name="Тестовый сценарий",
            description="Базовый сценарий для тестирования голосового контура",
            greeting="Здравствуйте! Меня зовут Татьяна, я звоню от компании. Удобно ли вам сейчас говорить?",
            system_prompt=(
                "Ты — AI-ассистент для исходящих звонков. "
                "Ведёшь вежливый деловой диалог на русском языке. "
                "Отвечай кратко, по делу. Если клиент не хочет говорить — вежливо прощайся."
            ),
            steps={
                "start": ScenarioStep(
                    id="start",
                    greeting="Здравствуйте! Удобно ли вам сейчас говорить?",
                    on_positive="pitch",
                    on_negative="farewell",
                    on_unknown="retry_start",
                ),
                "pitch": ScenarioStep(
                    id="pitch",
                    greeting="Отлично! Я хотела рассказать вам о нашем новом предложении. Вам это интересно?",
                    on_positive="details",
                    on_negative="farewell",
                    on_objection="handle_objection",
                ),
                "details": ScenarioStep(
                    id="details",
                    greeting="Замечательно! Могу я уточнить удобное время для более подробного разговора?",
                    on_positive="schedule",
                    on_negative="farewell",
                ),
                "schedule": ScenarioStep(
                    id="schedule",
                    greeting="Отлично, я зафиксирую. Спасибо за ваше время!",
                    is_final=True,
                ),
                "farewell": ScenarioStep(
                    id="farewell",
                    greeting="Понял, спасибо за ваше время. Хорошего дня!",
                    is_final=True,
                ),
            },
        )
        self.scenarios["default"] = default
        logger.info("Created default scenario")

    def get_scenario(self, scenario_id: str | None = None) -> Scenario:
        """Получить сценарий по ID."""
        sid = scenario_id or self.settings.default_scenario
        if sid not in self.scenarios:
            logger.warning(f"Scenario '{sid}' not found, using default")
            sid = "default"
        return self.scenarios[sid]

    def list_scenarios(self) -> list[dict]:
        """Список доступных сценариев."""
        return [
            {"id": s.id, "name": s.name, "description": s.description, "steps": len(s.steps)}
            for s in self.scenarios.values()
        ]
