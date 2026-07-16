# DevOps: подключение робота к АТС заказчика через VPN (L2TP/IPsec)

> ℹ️ **Это шаблон без секретов.** Реальные доступы (VPN-ключ/пароль, пароль SIP,
> секрет res24) передаются отдельно по защищённому каналу — см. «лист доступов».
> В репозиторий секреты не коммитятся.

---

## 1. Что уже есть (текущее состояние)

На VPS развёрнут проект **AI-робот для исходящих звонков** (домен
`aicallrobot.futuguru.com`). Стек в Docker Compose (`/opt/ai-robot`):

- **ai-robot** — приложение (Python/FastAPI/Uvicorn), порт 8000;
- **caddy** — обратный прокси + HTTPS (80/443);
- **postgres** — база клиентов/кампаний;
- **redis** — задел на будущее.

Сейчас робот умеет вести голосовой диалог через браузер (`/demo`), но **реальные
телефонные звонки не подключены** — нет доступа к АТС заказчика.

## 2. Что нужно сделать (задача)

АТС заказчика (**Asterisk 13**, адрес `192.168.0.110`) находится в их закрытой
сети. Робот должен:
1. подключиться к их сети по **VPN-туннелю L2TP/IPsec**;
2. зарегистрироваться на их АТС как **внутренний SIP-абонент (экстеншен 114)**;
3. звонить клиентам (АТС сама выбирает транк и подставляет Caller ID).

Кодек разговора — **G.711 alaw, 8 кГц** (совпадает с внутренним форматом робота).

**Твоя часть (DevOps):**
- поднять на VPS **клиент L2TP/IPsec** до сети заказчика (split-tunnel);
- контейнер `ai-robot` уже настроен на **host-режим сети** в `docker-compose.yml`
  (нужно для SIP/RTP) — отдельно ничего менять не надо;
- заполнить `.env` и перезапустить стек;
- проверить регистрацию SIP и тестовый звонок.

---

## 3. Параметры подключения

Значения секретов (помечены `<...>`) — в «листе доступов» (защищённый канал).

**VPN (L2TP/IPsec с общим ключом):**

| Параметр | Значение |
|---|---|
| Сервер | `v.24res.ru` |
| Тип | L2TP/IPsec PSK |
| Общий ключ (PSK) | `<VPN_PSK>` |
| Логин | `<VPN_LOGIN>` |
| Пароль | `<VPN_PASSWORD>` |
| Маршрут в туннель | `192.168.0.0/24` (доступ к `192.168.0.110`) |
| Окно доступа | **ПН–ПТ 08:00–22:00 GMT+7** (Красноярск) |

**SIP-экстеншен робота:**

| Параметр | Значение |
|---|---|
| SIP-сервер | `192.168.0.110` |
| Экстеншен (логин) | `114` |
| Пароль | `<SIP_PASSWORD>` |
| Внешний номер (Caller ID для t2) | `<OUTBOUND_NUMBER>` (GoIP) |

**HTTP-API АТС (res24.php) — инициирование звонка/статус/CDR:**

| Параметр | Значение |
|---|---|
| База | `http://192.168.0.110` |
| Логин | `robott` |
| Секрет | `<RES24_SECRET>` |

### ⚠️ Два ограничения, критичных при настройке
- **Блокировка:** 4 неудачные попытки входа в VPN → блокируется **вся /24
  нашего IP** до ручной разблокировки заказчиком. → сверь логин/PSK/пароль
  ДО запуска; при ошибке аутентификации **не перезапускать в цикле**.
- **Окно:** вне ПН–ПТ 08:00–22:00 GMT+7 туннель не поднимется. Тестировать в это
  время.

---

## 4. Поднятие L2TP/IPsec на VPS (Ubuntu/Debian)

### 4.1. Установка
```bash
sudo apt update
sudo apt install -y strongswan strongswan-starter xl2tpd ppp
```

### 4.2. IPsec — `/etc/ipsec.conf` (добавить блок)
```ini
conn res-l2tp
    keyexchange=ikev1
    authby=secret
    type=transport
    left=%defaultroute
    leftprotoport=17/1701
    right=v.24res.ru
    rightprotoport=17/1701
    # Набор шифров как у встроенного L2TP/IPsec Windows; подстроить, если сервер отвергнет
    ike=aes256-sha1-modp1024,aes128-sha1-modp1024,3des-sha1-modp1024!
    esp=aes256-sha1,aes128-sha1,3des-sha1!
    auto=add
```

### 4.3. IPsec PSK — `/etc/ipsec.secrets`
```
: PSK "<VPN_PSK>"
```
```bash
sudo chmod 600 /etc/ipsec.secrets
```

### 4.4. L2TP — `/etc/xl2tpd/xl2tpd.conf`
```ini
[lac res]
lns = v.24res.ru
ppp debug = yes
pppoptfile = /etc/ppp/options.l2tpd.client
length bit = yes
```

### 4.5. PPP — `/etc/ppp/options.l2tpd.client`
```
ipcp-accept-local
ipcp-accept-remote
refuse-eap
require-mschap-v2
noccp
noauth
mtu 1280
mru 1280
nodefaultroute
connect-delay 5000
name <VPN_LOGIN>
password <VPN_PASSWORD>
```
```bash
sudo chmod 600 /etc/ppp/options.l2tpd.client
```
> `nodefaultroute` — не перехватываем весь трафик, интернет VPS (к Yandex Cloud)
> остаётся. В туннель добавим только их подсеть (шаг 4.7).

### 4.6. Подключение
```bash
sudo systemctl restart strongswan-starter
sudo systemctl restart xl2tpd

sudo ipsec up res-l2tp           # ожидаем "established successfully"
sudo sh -c 'echo "c res" > /var/run/xl2tpd/l2tp-control'
sleep 6
ip -4 addr show ppp0             # должен появиться интерфейс ppp0 с адресом
```

### 4.7. Маршрут в сеть заказчика (split-tunnel)
```bash
sudo ip route replace 192.168.0.0/24 dev ppp0
```

### 4.8. Проверка туннеля
```bash
ping -c3 192.168.0.110
curl "http://192.168.0.110/api/res24.php?_login=robott&_secret=<RES24_SECRET>&_action=status"
```
Идёт `ping` и приходит JSON от `res24` → туннель поднят.
**Запиши адрес `ppp0`** (`ip -4 addr show ppp0`) — это `SIP_LOCAL_IP` для робота.

### 4.9. Автоподъём (скрипт) — `/usr/local/sbin/res-vpn-up.sh`
```bash
#!/bin/bash
set -e
ipsec up res-l2tp || { ipsec restart; sleep 2; ipsec up res-l2tp; }
echo "c res" > /var/run/xl2tpd/l2tp-control
sleep 6
ip route replace 192.168.0.0/24 dev ppp0
ip -4 addr show ppp0 | grep inet
```
```bash
sudo chmod +x /usr/local/sbin/res-vpn-up.sh
```
(При желании — systemd-юнит/таймер, держащий туннель поднятым в рабочее окно.)

---

## 5. Сетевой режим контейнера (уже настроен в репозитории)

Для SIP/RTP `ai-robot` работает в **host-режиме** сети (`network_mode: host` в
`docker-compose.yml`) — иначе pyVoIP анонсирует внутренний IP контейнера и голос
(RTP) не доходит. Сопутствующие правки уже внесены:

- `ai-robot`: `network_mode: host` (без `networks`/`expose`);
- `postgres`: порт опубликован на `127.0.0.1:5432` (ai-robot ходит по 127.0.0.1);
- `caddy`: `extra_hosts: host.docker.internal:host-gateway`, а в `Caddyfile`
  `reverse_proxy host.docker.internal:8000`.

DevOps'у эти файлы менять не нужно — только `.env` (ниже).

---

## 6. Настройка `.env` и запуск

В `/opt/ai-robot/.env` (создать из `.env.example`, если ещё нет) прописать:
```
# Yandex Cloud (без них робот не говорит/не слышит) — взять у владельца проекта
YANDEX_API_KEY=...
YANDEX_FOLDER_ID=...

# База (host-режим → 127.0.0.1)
DATABASE_URL=postgresql+asyncpg://robot:robot@127.0.0.1:5432/airobot

# SIP-экстеншен робота
SIP_SERVER=192.168.0.110
SIP_EXTENSION=114
SIP_PASSWORD=<SIP_PASSWORD>
SIP_LOCAL_IP=<адрес ppp0 из шага 4.8>

# HTTP-API АТС
RES24_BASE_URL=http://192.168.0.110
RES24_LOGIN=robott
RES24_SECRET=<RES24_SECRET>
ROBOT_EXTENSION=114

# Лимиты транков (t2 = 1 линия, местные = 30)
ROUTE_LIMIT_T2=1
ROUTE_LIMIT_LOCAL=30
```
Запуск:
```bash
cd /opt/ai-robot
docker compose up -d --build
docker compose logs -f ai-robot
```
В логах ожидаем: `Database schema ensured` и **`SIP-агент зарегистрирован: 114@192.168.0.110`**.

---

## 7. Проверка (тестовый звонок)

1. Убедиться, что VPN поднят (шаг 4.8) и в логах есть `SIP-агент зарегистрирован`.
2. Открыть `https://aicallrobot.futuguru.com/testcall`.
3. Ввести свой мобильный (формат `+79…`), нажать «Позвонить роботом».
4. Ответить на звонок — на странице идёт live-расшифровка речи и ответов робота,
   по завершении — саммари и квалификация.

Если телефония не настроена/VPN не поднят — страница вернёт понятную ошибку (503).

---

## 8. Диагностика
```bash
sudo ipsec statusall
sudo journalctl -u strongswan-starter -n 50
sudo journalctl -u xl2tpd -n 50
sudo tail -n 80 /var/log/syslog | grep -Ei 'pppd|l2tp|charon'
docker compose logs --tail=100 ai-robot
```
Типовое:
- **IPsec «no proposal chosen»** → сервер хочет другой набор шифров: поправить
  `ike=`/`esp=` в `ipsec.conf` (добавить `modp2048`, `sha256`).
- **IPsec встал, нет `ppp0`** → проверить логин/пароль/`require-mschap-v2` в
  `options.l2tpd.client`; смотреть `syslog` по `pppd`. **Не гонять в цикле**
  (блокировка /24).
- **Есть туннель, нет `ping`** → не добавлен маршрут (4.7) или вне окна
  ПН–ПТ 08:00–22:00 GMT+7.
- **Звонок соединяется, но нет звука** → неверный `SIP_LOCAL_IP` (должен быть
  адрес `ppp0`) или туннель не пропускает RTP/UDP.

---

## 9. Эксплуатация
- Туннель работает только в окне **ПН–ПТ 08:00–22:00 GMT+7**; вне окна звонки
  невозможны.
- После успешного подключения наш IP заносится заказчиком в доверенные —
  повторные блокировки прекращаются.
- Перезапуск робота: `docker compose restart ai-robot` (VPN на хосте при этом не
  трогается).
