# Инструкция по развёртыванию AI-робота на VPS

## Сервер: 31.129.97.163

---

## 1. Подключение к серверу

```bash
ssh root@31.129.97.163
```

---

## 2. Обновление системы и установка Docker

```bash
# Обновляем пакеты
apt update && apt upgrade -y

# Устанавливаем необходимые пакеты
apt install -y ca-certificates curl gnupg git

# Устанавливаем Docker
curl -fsSL https://get.docker.com | sh

# Устанавливаем Docker Compose
apt install -y docker-compose-plugin

# Проверяем
docker --version
docker compose version
```

---

## 3. Настройка файрвола

```bash
# Устанавливаем ufw если нет
apt install -y ufw

# Разрешаем SSH, HTTP, и порты приложения
ufw allow 22/tcp
ufw allow 80/tcp
ufw allow 443/tcp
ufw allow 8000/tcp    # API
ufw allow 8001/tcp    # WebSocket

# Включаем файрвол
ufw --force enable
ufw status
```

---

## 4. Клонирование проекта

```bash
# Создаём директорию
mkdir -p /opt/ai-robot
cd /opt/ai-robot

# Если проект на GitHub:
# git clone https://github.com/YOUR_REPO/ai-robot.git .

# Или копируем файлы через scp с локальной машины:
# scp -r ./ai-robot/* root@31.129.97.163:/opt/ai-robot/
```

---

## 5. Настройка переменных окружения

```bash
cd /opt/ai-robot

# Копируем шаблон
cp .env.example .env

# Редактируем .env — вставляем реальные ключи
nano .env
```

**Заполните обязательные поля в `.env`:**

```env
# Получены от Заказчика (см. инструкцию для Заказчика)
YANDEX_API_KEY=AQVN1HHJ...ваш_секретный_ключ
YANDEX_FOLDER_ID=b1g...ваш_folder_id

# Остальное можно оставить по умолчанию
TTS_VOICE=alena
ASR_MODEL=general:rc
MAX_CONCURRENT_CALLS=3
```

---

## 6. Запуск

```bash
cd /opt/ai-robot

# Собираем и запускаем
docker compose up -d --build

# Проверяем статус
docker compose ps

# Смотрим логи
docker compose logs -f ai-robot
```

**Ожидаемый вывод:**
```
ai-robot  | INFO | Starting AI-Robot (production)
ai-robot  | INFO | Max concurrent calls: 3
ai-robot  | INFO | TTS voice: alena
ai-robot  | INFO | Loaded scenario: default (Тестовый сценарий обзвона)
```

---

## 7. Проверка работоспособности

### 7.1. Health check

```bash
curl http://localhost:8000/health
```

Ответ:
```json
{"status": "ok", "calls": {"total_calls": 0, "active_calls": 0, ...}}
```

### 7.2. Тест TTS (синтез речи)

```bash
curl -X POST http://localhost:8000/api/v1/tts \
  -H "Content-Type: application/json" \
  -d '{"text": "Здравствуйте! Это тестовый звонок."}'
```

Ответ содержит `audio_base64` — аудио в формате PCM 8kHz.

### 7.3. Тест ASR (распознавание)

Отправьте base64-аудио из предыдущего шага:

```bash
curl -X POST http://localhost:8000/api/v1/asr \
  -H "Content-Type: application/json" \
  -d '{"audio_base64": "...base64_из_tts...", "format": "lpcm"}'
```

### 7.4. Тест сценариев

```bash
curl http://localhost:8000/api/v1/scenarios
```

### 7.5. Полный автотест

```bash
# Зайти в контейнер
docker compose exec ai-robot python -m tests.test_voice_pipeline
```

---

## 8. Доступ к демо и API (HTTPS)

Для работы микрофона в браузере нужен HTTPS. В проекте настроен Caddy
как обратный прокси с автоматическим SSL.

### Вариант А — Есть домен (рекомендуется)

1. Направьте A-запись домена (например `demo.yourcompany.ru`) на `31.129.97.163`
2. Отредактируйте `Caddyfile`:
```
demo.yourcompany.ru {
    reverse_proxy ai-robot:8000
}
```
3. Перезапустите: `docker compose restart caddy`
4. Caddy автоматически получит SSL-сертификат от Let's Encrypt
5. Откройте: `https://demo.yourcompany.ru/demo`

### Вариант Б — Нет домена (self-signed сертификат)

`Caddyfile` по умолчанию уже настроен на self-signed сертификат. Просто:

1. Откройте: `https://31.129.97.163/demo`
2. Браузер покажет предупреждение — нажмите **«Дополнительно»** → **«Перейти на сайт»**
3. После этого микрофон будет работать

Для Заказчика: отправьте ссылку `https://31.129.97.163/demo` и предупредите,
что нужно один раз принять предупреждение браузера о сертификате.

### API-адреса

```
https://31.129.97.163/health
https://31.129.97.163/api/v1/tts
https://31.129.97.163/api/v1/asr
https://31.129.97.163/api/v1/scenarios
https://31.129.97.163/demo          ← демо-страница для Заказчика
https://31.129.97.163/docs          ← Swagger UI
```

---

## 9. Управление

```bash
# Перезапуск
docker compose restart

# Остановка
docker compose down

# Пересборка после изменений
docker compose up -d --build

# Логи в реальном времени
docker compose logs -f ai-robot

# Логи на диске
cat /opt/ai-robot/logs/ai-robot.log
```

---

## 10. Структура проекта

```
ai-robot/
├── app/
│   ├── main.py              # FastAPI приложение
│   ├── core/
│   │   ├── config.py         # Настройки из .env
│   │   └── logging.py        # Логирование
│   ├── services/
│   │   ├── tts.py            # Yandex SpeechKit TTS
│   │   ├── asr.py            # Yandex SpeechKit ASR
│   │   ├── audio_pipeline.py # Обработка аудио, VAD, перебивания
│   │   ├── call_manager.py   # Управление звонками
│   │   └── scenario_engine.py # Движок сценариев
│   └── api/
│       └── routes.py         # HTTP и WebSocket эндпоинты
├── scenarios/
│   └── default.yaml          # Сценарий по умолчанию
├── tests/
│   └── test_voice_pipeline.py # Автотесты
├── docker-compose.yml
├── Dockerfile
├── requirements.txt
├── .env.example
└── .env                      # ← ваши ключи (не в Git!)
```

---

## Что реализовано в Этапе 1

| Компонент | Статус |
|-----------|--------|
| VPS + Docker | ✅ |
| Yandex SpeechKit TTS (синтез речи) | ✅ |
| Yandex SpeechKit ASR (распознавание) | ✅ |
| Голосовой профиль (alena, настройка скорости/эмоции) | ✅ |
| VAD (определение пауз и перебиваний) | ✅ |
| Движок сценариев (YAML) | ✅ |
| Менеджер звонков (до 3 параллельных) | ✅ |
| WebSocket для потокового аудио | ✅ |
| REST API для управления | ✅ |
| Swagger-документация | ✅ |
| Логирование | ✅ |
| Автотесты | ✅ |
