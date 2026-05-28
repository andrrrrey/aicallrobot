"""Движок диалога v2: жёсткий скриптовый алгоритм — ИИ только классифицирует."""

import time
from dataclasses import dataclass, field
from loguru import logger

from app.services.yandex_gpt import YandexGPTService
from app.services.script_v2_data import (
    SCRIPT,
    TRANSFER_SIGNALS,
    LPR_PICKUP_SIGNALS,
    SECRETARY_INTENT_CODES,
    LPR_GREETING_INTENT_CODES,
    LPR_MAIN_INTENT_CODES,
    QUAL_STEP0_CODES,
    QUAL_STEP1_CODES,
    QUAL_STEP2_CODES,
    QUAL_STEP2B_CODES,
    QUAL_STEP3_CODES,
    QUAL_STEP4_CODES,
    QUAL_STEP4P_CODES,
    QUAL_STEP5_CODES,
)

# ── Промпты классификации ──────────────────────────────────────────────────────

_SECRETARY_PROMPT = """Ты — классификатор реплик в телефонном разговоре.
Текущая фаза: Секретарь компании отвечает на звонок.

История разговора (последние реплики):
{history}

Последняя реплика робота: "{last_robot}"
Реплика собеседника: "{user_text}"
{negative_note}
Выбери ОДИН код из списка ниже, который точнее всего описывает смысл реплики собеседника.
ОБЯЗАТЕЛЬНО учитывай контекст всего разговора и тон собеседника.
Ответь строго одним кодом — без пояснений, без знаков препинания.

Коды:
- transfer_to_lpr: секретарь ПРЯМО СЕЙЧАС переводит звонок — "соединяю", "переведу", "передаю трубку", "переключаю", "сейчас переведу". ВАЖНО: если секретарь говорит что соединит В БУДУЩЕМ или НА СВОИХ УСЛОВИЯХ ("я сам соединю", "потом переведу", "я вас переключу когда-нибудь") — это НЕ transfer_to_lpr, это call_back
- what_do_you_want: "что вы хотели?", "по какому вопросу?", "чем могу помочь?", "слушаю вас", "говорите"
- has_responsible: секретарь сообщает что ответственный ЕСТЬ — называет должность ("энергетик", "главный инженер", "есть инженер") без имени, но НЕ переводит звонок прямо сейчас
- no_engineer: "нет энергетика", "нет ответственного", "не знаю кто отвечает", "у нас нет такого специалиста"
- renting: "мы в аренде", "мы арендуем помещение", "это к арендодателю", "мы арендаторы"
- inspecting_body: "вы проверяющий орган?", "вы из Ростехнадзора?", "вы проверяющие?", "вы инспекция?"
- documents: "вы хотите документы?", "нам нужно что-то предоставить?", "какие документы?"
- send_email: "отправьте на почту", "все предложения на почту", "только через почту", "пишите на email"
- all_good: "нам не нужно", "у нас всё хорошо", "всё проверено", "не интересно", "нам не требуется"
- wont_connect: "не соединяем с директором", "не переключаем", "не могу переключить на руководителя"
- cant_connect: "не могу соединить", "не получится соединить", "соединить не могу"
- not_present: "его нет на месте", "отсутствует", "на совещании", "уехал", "его сегодня нет"
- refuses_connect: "не соединяю принципиально", "мы не переводим звонки", "не в нашей политике переключать"
- everything_fine: "у нас всё хорошо с электросетями", "электросети в порядке", "всё нормально с электрикой"
- we_dont_do: "мы такие работы не проводим", "это нам не нужно делать", "мы этим не занимаемся"
- have_contract: "у нас с вами договор?", "вы наш подрядчик?", "мы с вами работаем?"
- dont_understand: "не понимаю", "что вы хотите?", "о чём вы говорите?", "не совсем понятно"
- call_back: "перезвоните", "позвоните позже", "через час", "после обеда перезвоните", "я сам соединю", "я вас сам переключу" (обещание соединить в будущем или на своих условиях)
- wrong_number: "куда вы звоните?", "вы не туда попали", "это не та организация"
- boss_no_connect: "руководитель сказал не соединять", "директор велел всё через меня", "не соединяю по указанию начальства"
- wrong_dept: "вы попали в отдел продаж", "это бухгалтерия", "вы не в тот отдел попали"
- gave_name: секретарь называет имя ответственного человека (только имя, без номера)
- gave_number: секретарь диктует номер телефона (набор цифр)
- ask_our_number: "продиктуйте ваш номер", "давайте запишу ваш номер", "оставьте ваш контакт"
- unknown: ничего из перечисленного не подходит

Реплика собеседника: "{user_text}"
Ответь ТОЛЬКО кодом:"""

_LPR_GREETING_PROMPT = """Ты — классификатор реплик в телефонном разговоре.
Текущая фаза: Робот только что представился ЛПР (лицу принимающему решение) после переключения от секретаря.

История разговора (последние реплики):
{history}

Последняя реплика робота: "{last_robot}"
Реплика собеседника: "{user_text}"

Выбери ОДИН код. Учитывай контекст и тон собеседника.
- confirmed: собеседник подтверждает, что он ответственный за электрохозяйство ("да", "всё верно", "я отвечаю за электрику", "это я", "да, это ко мне")
- wrong_person: собеседник говорит, что это не к нему ("нет, не ко мне", "я не за это отвечаю", "не по адресу", "не тот отдел")
- unknown: непонятный ответ, уклонение, посторонний вопрос

Реплика собеседника: "{user_text}"
Ответь ТОЛЬКО кодом:"""

_LPR_MAIN_PROMPT = """Ты — классификатор реплик в телефонном разговоре.
Текущая фаза: Разговор с ЛПР (ответственным за электрохозяйство).

История разговора (последние реплики):
{history}

Последняя реплика робота: "{last_robot}"
Реплика собеседника: "{user_text}"

Выбери ОДИН код. Учитывай контекст и тон собеседника.
- address_question: ЛПР спрашивает адрес компании ("где вы находитесь?", "ваш адрес?")
- phone_source: ЛПР спрашивает откуда взяли номер ("откуда наш номер?", "кто вам дал телефон?")
- propose_works: ЛПР спрашивает хотим ли мы предложить работы ("вы хотите предложить провести работы?", "что-то предложить?")
- send_kp: ЛПР просит коммерческое предложение на почту ("отправьте КП", "пришлите коммерческое", "на почту скиньте")
- no_works: ЛПР говорит что работы не планируются ("не планируем", "нет, работы не нужны", "всё уже сделали", "нет потребности")
- far_date: ЛПР называет дальний срок 2026 год и позже или "через год", "через полгода", "не скоро", "пока не планируем"
- works_planned: ЛПР говорит что СЕЙЧАС планируют работы — в ближайшие 1-2 месяца ("да, планируем", "собираем КП", "подходят сроки", "в ближайшее время", "скоро нужно", "в этом квартале")
- own_company: у ЛПР есть компания-подрядчик ("у нас есть компания", "мы работаем с другой лабораторией", "есть свой подрядчик")
- own_lab_staff: у ЛПР своя штатная лаборатория ("у нас своя лаборатория", "у нас в штате ЭТЛ", "своя испытательная лаборатория")
- own_lab_contractor: ЛПР уточняет что "своя лаборатория" — это подрядчик/компания на аутсорсе
- ask_our_number: ЛПР просит наш номер телефона ("дайте ваш номер", "продиктуйте телефон")
- says_record: ЛПР собирается продиктовать СВОЙ номер ("запишите", "записывайте", "диктую")
- says_phone: ЛПР диктует номер телефона (набор цифр)
- unknown: ничего из перечисленного

Реплика собеседника: "{user_text}"
Ответь ТОЛЬКО кодом:"""

_QUAL_PROMPTS: dict[int, str] = {
    0: """Ты — классификатор. Робот спросил разрешения задать несколько вопросов для оформления заявки.
Реплика: "{user_text}"
Коды:
- agreement: согласие ("не против", "давайте", "задавайте", "конечно", "хорошо", "да")
- disagreement: отказ, нежелание отвечать на вопросы
- unknown: непонятно
Ответь ТОЛЬКО кодом:""",

    1: """Ты — классификатор. Робот спросил в каком месяце планируются работы.
Реплика: "{user_text}"
Коды:
- gave_month: называет конкретный месяц или период в пределах 2 месяцев ("в июне", "в июле", "на следующий месяц", "в августе", "скоро", "в этом квартале", "в ближайшее время")
- far_date: называет дальний срок — 2027 год или позже, "через год", "через полгода", "не скоро"
- unknown: уклончивый ответ, не называет конкретный срок
Ответь ТОЛЬКО кодом:""",

    2: """Ты — классификатор. Робот спросил выделен ли бюджет на работы.
Реплика: "{user_text}"
Коды:
- budget_yes: бюджет есть ("да", "бюджет выделен", "есть бюджет", "предусмотрен")
- budget_no: бюджета нет ("нет", "не выделен", "пока нет", "не предусмотрен", "надо уточнить")
- unknown: уклончивый ответ
Ответь ТОЛЬКО кодом:""",

    3: """Ты — классификатор. Робот спросил готов ли ЛПР показать объём работ специалисту.
Реплика: "{user_text}"
Коды:
- show_specialist: готов показать ("приезжайте", "смотрите", "можно посмотреть")
- remote: пришлёт на почту или по телефону ("скину на почту", "по телефону скажу", "отправлю документы")
- by_phone: сориентирует по телефону устно
- unknown: уклончивый ответ
Ответь ТОЛЬКО кодом:""",

    4: """Ты — классификатор. Робот спросил будет ли договор напрямую или через площадку.
Реплика: "{user_text}"
Коды:
- direct: напрямую ("напрямую", "прямой договор", "без тендера", "без площадки")
- platform: через торговую площадку / тендер / закупку ("через площадку", "тендер", "закупка", "торги", "44-ФЗ", "223-ФЗ")
- unknown: непонятно
Ответь ТОЛЬКО кодом:""",

    41: """Ты — классификатор. Робот спросил можно ли заключить прямой договор до определённой суммы.
Реплика: "{user_text}"
Коды:
- platform_direct_yes: да, можно напрямую до определённой суммы
- platform_direct_no: нет, только через площадку
- unknown: непонятно
Ответь ТОЛЬКО кодом:""",

    5: """Ты — классификатор. Робот просит прямой номер телефона ЛПР.
Реплика: "{user_text}"
Коды:
- gave_phone: диктует цифры телефона или говорит "звоните на этот же", "по этому номеру"
- says_record: говорит "запишите", "записывайте" — собирается продиктовать номер
- ask_our_number: просит наш номер ("давайте лучше запишу ваш", "продиктуйте свой")
- unknown: уклончивый ответ, отказывается давать номер
Ответь ТОЛЬКО кодом:""",
}


@dataclass
class V2SessionState:
    """Состояние сессии диалога v2."""
    session_id: str
    phase: str = "secretary"           # secretary | lpr_greeting | lpr_main | qualification | closed
    # Секретарь
    secretary_greeted: bool = False
    secretary_cant_connect_asked: bool = False   # был задан уточняющий вопрос про "не могу соединить"
    secretary_all_good_asked: bool = False        # был задан вопрос про должность при "всё хорошо"
    secretary_own_company_attempt: int = 0        # счётчик попыток при "своя компания"
    # ЛПР
    lpr_greeted: bool = False
    lpr_topic_asked: bool = False
    current_lpr_node: str = ""
    lpr_own_company_attempt: int = 0
    lpr_own_etl_asked: bool = False
    # Квалификация
    qual_step: int = 0
    qual_step2b_pending: bool = False
    qual_step4p_pending: bool = False
    qual_data: dict = field(default_factory=dict)
    # Диагностика
    unknown_streak: int = 0
    last_robot_text: str = ""
    created_at: float = field(default_factory=time.time)
    # Контекст диалога для улучшенной классификации
    recent_exchanges: list = field(default_factory=list)
    # Формат: {"role": "user"|"robot", "text": str, "intent": str (только для user)}
    negative_turn_count: int = 0  # кол-во уклончивых/отказных реплик секретаря


_NEGATIVE_NODES: frozenset[str] = frozenset({
    "call_back", "cant_connect", "wont_connect", "refuses_connect",
    "all_good", "not_present", "boss_no_connect", "send_email",
    "everything_fine", "we_dont_do", "dont_understand",
})

_SELF_CONNECT_PATTERNS: tuple[str, ...] = (
    "я сам соединю", "я вас сам соединю", "я сам переведу",
    "я сам переключу", "я вас сам переключу", "сам переключу",
    "сам соединю", "сам переведу",
)


def _format_history(exchanges: list) -> str:
    """Форматирует список обменов в читаемый текст для промпта."""
    if not exchanges:
        return "(начало разговора)"
    parts = []
    for e in exchanges:
        role = "Робот" if e["role"] == "robot" else "Собеседник"
        parts.append(f"{role}: {e['text']}")
    return "\n".join(parts)


class ScriptDialogueV2:
    """Скриптовый движок диалога v2: классификация через ИИ, ответы из скрипта."""

    MAX_SESSIONS = 500

    def __init__(self, gpt_service: YandexGPTService):
        self.gpt = gpt_service
        self._sessions: dict[str, V2SessionState] = {}

    # ── Управление сессиями ────────────────────────────────────────────────────

    def create_session(self, session_id: str) -> V2SessionState:
        if len(self._sessions) >= self.MAX_SESSIONS:
            oldest = min(self._sessions.values(), key=lambda s: s.created_at)
            del self._sessions[oldest.session_id]
        state = V2SessionState(session_id=session_id)
        self._sessions[session_id] = state
        return state

    def get_session(self, session_id: str) -> V2SessionState | None:
        return self._sessions.get(session_id)

    def delete_session(self, session_id: str) -> None:
        self._sessions.pop(session_id, None)

    def greeting(self, session_id: str) -> dict:
        """Возвращает первую реплику (приветствие секретаря) и отмечает что оно произнесено."""
        state = self.get_session(session_id)
        if not state:
            state = self.create_session(session_id)
        text = SCRIPT["greeting"]
        state.secretary_greeted = True
        state.last_robot_text = text
        return self._response(text, "secretary", "greeting", state)

    # ── Основной метод обработки реплики ──────────────────────────────────────

    async def process_turn(self, session_id: str, user_text: str) -> dict:
        """Обрабатывает одну реплику пользователя, возвращает ответ из скрипта."""
        state = self.get_session(session_id)
        if not state:
            state = self.create_session(session_id)

        user_text = user_text.strip()
        if not user_text:
            return self._response(SCRIPT["fallback_secretary"], state.phase, "empty", state)

        if state.phase == "closed":
            return self._response(SCRIPT["closed"], "closed", "closed", state)

        robot_text, node = await self._dispatch(state, user_text)
        state.last_robot_text = robot_text

        if robot_text:
            state.unknown_streak = 0
        else:
            state.unknown_streak += 1

        # Обновляем историю диалога (последние 10 сообщений = 5 обменов)
        state.recent_exchanges.append({"role": "user", "text": user_text, "intent": node})
        state.recent_exchanges.append({"role": "robot", "text": robot_text})
        if len(state.recent_exchanges) > 10:
            state.recent_exchanges = state.recent_exchanges[-10:]

        # Трекаем сопротивление секретаря для контекста классификации
        if node in _NEGATIVE_NODES and state.phase == "secretary":
            state.negative_turn_count += 1

        logger.info(
            f"[v2] session={session_id} phase={state.phase} node={node} "
            f"neg={state.negative_turn_count} text='{user_text[:60]}' → '{robot_text[:60]}'"
        )
        return self._response(robot_text, state.phase, node, state)

    # ── Диспетчер по фазам ────────────────────────────────────────────────────

    async def _dispatch(self, state: V2SessionState, user_text: str) -> tuple[str, str]:
        if state.phase == "secretary":
            return await self._handle_secretary(state, user_text)
        elif state.phase == "lpr_greeting":
            return await self._handle_lpr_greeting(state, user_text)
        elif state.phase == "lpr_main":
            return await self._handle_lpr_main(state, user_text)
        elif state.phase == "qualification":
            return await self._handle_qualification(state, user_text)
        return SCRIPT["fallback_secretary"], "unknown"

    # ── Фаза: Секретарь ───────────────────────────────────────────────────────

    async def _handle_secretary(self, state: V2SessionState, user_text: str) -> tuple[str, str]:
        lower = user_text.lower()

        # Приоритетная проверка сигналов передачи трубки (без вызова ИИ)
        if any(sig in lower for sig in TRANSFER_SIGNALS):
            return self._do_transfer(state, "transfer_signal")

        if any(sig in lower for sig in LPR_PICKUP_SIGNALS):
            return self._do_transfer(state, "lpr_pickup_signal")

        # Эвристика: "я сам соединю/переведу" — обещание на будущее, не реальный перевод
        if any(p in lower for p in _SELF_CONNECT_PATTERNS):
            return SCRIPT["secretary_call_back"], "call_back"

        # Контекст под-вопроса "не могу соединить"
        if state.secretary_cant_connect_asked:
            state.secretary_cant_connect_asked = False
            # Любой ответ — обрабатываем как уточнение
            if any(w in lower for w in ("нет", "не будет", "отсутствует", "уехал", "совещан")):
                return SCRIPT["secretary_not_present"], "secretary_not_present"
            return SCRIPT["secretary_refuses_connect"], "secretary_refuses_connect"

        # Контекст под-вопроса "всё хорошо" — спросили должность
        if state.secretary_all_good_asked:
            state.secretary_all_good_asked = False
            if any(w in lower for w in ("секретарь", "администратор", "офис-менеджер", "помощник")):
                # Секретарь — ищем ответственного
                return SCRIPT["secretary_not_responsible"], "secretary_not_responsible"
            # Иначе считаем что он может быть ответственным
            return SCRIPT["secretary_responsible_confirmed"], "secretary_responsible_confirmed"

        code = await self._classify_secretary(
            user_text, state.last_robot_text,
            state.recent_exchanges, state.negative_turn_count,
        )

        if code == "transfer_to_lpr":
            return self._do_transfer(state, code)

        if code == "what_do_you_want":
            return SCRIPT["secretary_what_do_you_want"], code

        if code == "has_responsible":
            return SCRIPT["secretary_connect_responsible"], code

        if code == "no_engineer":
            return SCRIPT["secretary_no_engineer"], code

        if code == "renting":
            return SCRIPT["secretary_renting"], code

        if code == "inspecting_body":
            return SCRIPT["secretary_inspecting_body"], code

        if code == "documents":
            return SCRIPT["secretary_documents"], code

        if code == "send_email":
            return SCRIPT["secretary_send_email"], code

        if code == "all_good":
            state.secretary_all_good_asked = True
            return SCRIPT["secretary_all_good"], code

        if code == "wont_connect":
            return SCRIPT["secretary_wont_connect"], code

        if code == "cant_connect":
            state.secretary_cant_connect_asked = True
            return SCRIPT["secretary_cant_connect"], code

        if code == "not_present":
            return SCRIPT["secretary_not_present"], code

        if code == "refuses_connect":
            return SCRIPT["secretary_refuses_connect"], code

        if code == "everything_fine":
            return SCRIPT["secretary_everything_fine"], code

        if code == "we_dont_do":
            return SCRIPT["secretary_we_dont_do"], code

        if code == "have_contract":
            return SCRIPT["secretary_have_contract"], code

        if code == "dont_understand":
            return SCRIPT["secretary_dont_understand"], code

        if code == "call_back":
            return SCRIPT["secretary_call_back"], code

        if code == "wrong_number":
            return SCRIPT["secretary_wrong_number"], code

        if code == "boss_no_connect":
            return SCRIPT["secretary_boss_no_connect"], code

        if code == "wrong_dept":
            return SCRIPT["secretary_wrong_dept"], code

        if code == "gave_name":
            return SCRIPT["secretary_gave_name"], code

        if code == "gave_number":
            return SCRIPT["secretary_gave_both"], code

        if code == "ask_our_number":
            return SCRIPT["secretary_give_our_number"], code

        # Фоллбэк
        if state.unknown_streak >= 2:
            return SCRIPT["secretary_dont_understand"], "unknown_limit"
        return SCRIPT["fallback_secretary"], "unknown"

    def _do_transfer(self, state: V2SessionState, code: str) -> tuple[str, str]:
        state.phase = "lpr_greeting"
        state.lpr_greeted = False
        return SCRIPT["lpr_greeting"], code

    # ── Фаза: Приветствие ЛПР ─────────────────────────────────────────────────

    async def _handle_lpr_greeting(self, state: V2SessionState, user_text: str) -> tuple[str, str]:
        code = await self._classify_lpr_greeting(
            user_text, state.last_robot_text, state.recent_exchanges,
        )

        if code == "confirmed":
            state.phase = "lpr_main"
            state.lpr_topic_asked = False
            return SCRIPT["lpr_confirmed_q"], "lpr_confirmed"

        if code == "wrong_person":
            return SCRIPT["lpr_wrong_person"], "lpr_wrong_person"

        # unknown — повторяем приветствие один раз
        if state.unknown_streak >= 1:
            return SCRIPT["lpr_greeting_retry"], "lpr_greeting_retry"
        return SCRIPT["lpr_greeting"], "lpr_greeting_repeat"

    # ── Фаза: ЛПР основная ────────────────────────────────────────────────────

    async def _handle_lpr_main(self, state: V2SessionState, user_text: str) -> tuple[str, str]:
        lower = user_text.lower()

        # Проверка на запрос нашего номера
        if any(w in lower for w in ("ваш номер", "продиктуйте", "оставьте контакт", "ваш телефон")):
            return SCRIPT["our_phone"], "ask_our_number"

        # Проверка на "запишите" — ЛПР диктует свой номер
        if any(w in lower for w in ("запишите", "записывайте", "записываю", "диктую")):
            return SCRIPT["lpr_far_date_get_number"], "says_record"

        code = await self._classify_lpr_main(
            user_text, state.last_robot_text, state.recent_exchanges,
        )

        if code == "address_question":
            return SCRIPT["lpr_address"], code

        if code == "phone_source":
            return SCRIPT["lpr_phone_source"], code

        if code == "propose_works":
            return SCRIPT["lpr_propose_works"], code

        if code == "send_kp":
            return SCRIPT["lpr_send_kp_clarify"], code

        if code == "no_works":
            return SCRIPT["lpr_no_works"], code

        if code == "far_date":
            return SCRIPT["lpr_far_date"], code

        if code == "works_planned":
            state.phase = "qualification"
            state.qual_step = 0
            return SCRIPT["qual_step0"], "works_planned→qual0"

        if code == "own_company":
            attempt = state.lpr_own_company_attempt
            state.lpr_own_company_attempt += 1
            if attempt == 0:
                return SCRIPT["lpr_own_company_1"], code
            elif attempt == 1:
                return SCRIPT["lpr_own_company_2"], code
            else:
                return SCRIPT["lpr_own_company_3"], code

        if code == "own_lab_staff":
            state.lpr_own_etl_asked = True
            return SCRIPT["lpr_own_etl_license"], code

        if code == "own_lab_contractor":
            # На самом деле это подрядчик — как "своя компания"
            return SCRIPT["lpr_own_company_1"], "own_lab_contractor"

        if code == "says_record":
            return SCRIPT["lpr_far_date_get_number"], code

        if code == "says_phone":
            return SCRIPT["qual_step6"], "phone_received"  # получили номер — закрываем

        if code == "ask_our_number":
            return SCRIPT["our_phone"], code

        # ЛПР упомянул лицензию (ответ на наш вопрос про ЭТЛ)
        if state.lpr_own_etl_asked:
            state.lpr_own_etl_asked = False
            return SCRIPT["lpr_our_license"], "lpr_license_response"

        if state.unknown_streak >= 2:
            return SCRIPT["fallback_lpr"], "unknown_limit"
        return SCRIPT["fallback_lpr"], "unknown"

    # ── Фаза: Квалификация ────────────────────────────────────────────────────

    async def _handle_qualification(self, state: V2SessionState, user_text: str) -> tuple[str, str]:
        lower = user_text.lower()
        step = state.qual_step

        # Проверка на запрос нашего номера в любом шаге
        if any(w in lower for w in ("ваш номер", "продиктуйте свой", "ваш телефон")):
            return (
                SCRIPT["our_phone"] + " И подскажите всё таки, когда у вас подходят сроки технического отчёта?",
                "ask_our_number",
            )

        code = await self._classify_qualification(
            user_text, step, state.qual_step2b_pending, state.qual_step4p_pending
        )

        if step == 0:
            if code == "agreement":
                state.qual_step = 1
                return SCRIPT["qual_step1"], "qual0→qual1"
            if code == "disagreement":
                return SCRIPT["fallback_lpr"], "qual0_disagreement"
            return SCRIPT["qual_step0"], "qual0_repeat"

        elif step == 1:
            if code == "gave_month":
                state.qual_step = 2
                return SCRIPT["qual_step2"], "qual1→qual2"
            if code == "far_date":
                state.phase = "lpr_main"
                state.qual_step = 0
                return SCRIPT["lpr_far_date"], "qual1_far_date"
            return SCRIPT["qual_step1"], "qual1_repeat"

        elif step == 2:
            if state.qual_step2b_pending:
                # Это ответ на вопрос про рассрочку
                state.qual_step2b_pending = False
                state.qual_step = 3
                return SCRIPT["qual_step3"], "qual2b→qual3"
            if code == "budget_yes":
                state.qual_step = 3
                return SCRIPT["qual_step3"], "qual2→qual3"
            if code == "budget_no":
                state.qual_step2b_pending = True
                return SCRIPT["qual_step2b"], "qual2→qual2b"
            return SCRIPT["qual_step2"], "qual2_repeat"

        elif step == 3:
            # Любой внятный ответ — двигаемся дальше
            if code in ("show_specialist", "remote", "by_phone"):
                state.qual_step = 4
                return SCRIPT["qual_step4"], "qual3→qual4"
            return SCRIPT["qual_step3"], "qual3_repeat"

        elif step == 4:
            if state.qual_step4p_pending:
                # Это ответ на вопрос "можно ли напрямую до суммы?"
                state.qual_step4p_pending = False
                if code == "platform_direct_yes":
                    state.qual_step = 5
                    return SCRIPT["qual_step5"], "qual4p→qual5"
                else:
                    # Площадка без прямого договора — берём детали и закрываем
                    state.phase = "closed"
                    return SCRIPT["qual_step4_platform_details"], "qual4p_no→closed"
            if code == "direct":
                state.qual_step = 5
                return SCRIPT["qual_step5"], "qual4→qual5"
            if code == "platform":
                state.qual_step4p_pending = True
                return SCRIPT["qual_step4_platform"], "qual4→qual4p"
            return SCRIPT["qual_step4"], "qual4_repeat"

        elif step == 5:
            if code == "says_record":
                return SCRIPT["qual_step5_recording"], "qual5_record"
            if code in ("gave_phone", "says_phone"):
                state.qual_step = 6
                state.phase = "closed"
                return SCRIPT["qual_step6"], "qual5→qual6→closed"
            if code == "ask_our_number":
                return (
                    SCRIPT["our_phone"] + " И подскажите всё таки, ваш прямой номер?",
                    "ask_our_number",
                )
            # Если диктуют цифры — считаем что номер дан
            if any(ch.isdigit() for ch in user_text):
                state.qual_step = 6
                state.phase = "closed"
                return SCRIPT["qual_step6"], "qual5_digits→closed"
            return SCRIPT["qual_step5"], "qual5_repeat"

        elif step == 6:
            state.phase = "closed"
            return SCRIPT["qual_step6"], "qual6_closed"

        return SCRIPT["fallback_lpr"], "qual_unknown"

    # ── Классификация через ИИ ────────────────────────────────────────────────

    async def _classify_secretary(
        self, user_text: str, last_robot: str,
        recent_exchanges: list, negative_count: int,
    ) -> str:
        history = _format_history(recent_exchanges[-8:])
        neg_note = (
            f"\nВАЖНО: в этом разговоре собеседник уже {negative_count} раз(а) уклонялся "
            f"или отказывал. При малейшей двусмысленности выбирай отказной код "
            f"(call_back, cant_connect и т.д.), а НЕ transfer_to_lpr."
        ) if negative_count > 0 else ""
        prompt = _SECRETARY_PROMPT.format(
            user_text=user_text,
            last_robot=last_robot or "—",
            history=history,
            negative_note=neg_note,
        )
        return await self._classify(prompt, SECRETARY_INTENT_CODES)

    async def _classify_lpr_greeting(
        self, user_text: str, last_robot: str, recent_exchanges: list,
    ) -> str:
        history = _format_history(recent_exchanges[-6:])
        prompt = _LPR_GREETING_PROMPT.format(
            user_text=user_text,
            last_robot=last_robot or "—",
            history=history,
        )
        return await self._classify(prompt, LPR_GREETING_INTENT_CODES)

    async def _classify_lpr_main(
        self, user_text: str, last_robot: str, recent_exchanges: list,
    ) -> str:
        history = _format_history(recent_exchanges[-8:])
        prompt = _LPR_MAIN_PROMPT.format(
            user_text=user_text,
            last_robot=last_robot or "—",
            history=history,
        )
        return await self._classify(prompt, LPR_MAIN_INTENT_CODES)

    async def _classify_qualification(
        self, user_text: str, step: int, step2b_pending: bool, step4p_pending: bool = False
    ) -> str:
        # Для под-вопроса шага 4 (площадка → можно ли напрямую?) используем промпт 41
        if step == 4 and step4p_pending:
            template = _QUAL_PROMPTS.get(41)
            valid = QUAL_STEP4P_CODES
        else:
            template = _QUAL_PROMPTS.get(step)
            valid = {
                0: QUAL_STEP0_CODES,
                1: QUAL_STEP1_CODES,
                2: QUAL_STEP2_CODES if not step2b_pending else QUAL_STEP2B_CODES,
                3: QUAL_STEP3_CODES,
                4: QUAL_STEP4_CODES,
                5: QUAL_STEP5_CODES,
            }.get(step, ("unknown",))
        if not template:
            return "unknown"
        prompt = template.format(user_text=user_text)
        return await self._classify(prompt, valid)

    async def _classify(self, prompt: str, valid_codes: tuple | set) -> str:
        try:
            messages = [{"role": "user", "text": prompt}]
            result = await self.gpt.complete(messages, temperature=0.05, max_tokens=12)
            code = result.strip().lower().rstrip(".,!?").strip()
            if code in valid_codes:
                return code
            # Попытка найти код как подстроку
            for c in valid_codes:
                if c in code:
                    return c
            return "unknown"
        except Exception as e:
            logger.error(f"[v2] classify error: {e}")
            return "unknown"

    # ── Вспомогательные ───────────────────────────────────────────────────────

    @staticmethod
    def _response(text: str, phase: str, node: str, state: V2SessionState) -> dict:
        phase_labels = {
            "secretary": "Секретарь",
            "lpr_greeting": "ЛПР (приветствие)",
            "lpr_main": "ЛПР",
            "qualification": "Квалификация",
            "closed": "Завершён",
        }
        return {
            "robot_text": text,
            "phase": phase,
            "phase_label": phase_labels.get(phase, phase),
            "node": node,
            "qual_step": state.qual_step,
            "debug": {
                "classified_as": node,
                "phase_before": phase,
            },
        }
