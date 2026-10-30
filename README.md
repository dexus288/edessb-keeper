# ЄДЕССБ keeper

Тримає вашу сесію ЄДЕССБ «живою» та стежить за нею через Telegram-бот.

- періодично звертається до захищеного URL з вашими кукі (keep-alive);
- кукі ви надсилаєте/оновлюєте прямо в Telegram (`/setcookies`);
- якщо сесія впала — бот одразу надсилає сповіщення; коли відновиться — теж.

Залежність одна — `requests`. Стан (кукі, chat_id) зберігається між перезапусками.

---

## 1. Бот у Telegram

1. Напишіть [@BotFather](https://t.me/BotFather) → `/newbot` → отримаєте **токен**.
2. Напишіть [@userinfobot](https://t.me/userinfobot) → отримаєте свій **chat_id**.

## 2. Розгортання на DigitalOcean (Ubuntu droplet)

Найдешевшого дроплета ($4–6/міс) достатньо. Під root:

```bash
# користувач і каталог
adduser --system --group --home /opt/edessb-keeper edessb
mkdir -p /opt/edessb-keeper

# код (скопіюйте файли проєкту сюди: edessb_keeper.py, requirements.txt, ...)
cd /opt/edessb-keeper

# venv + залежності
apt update && apt install -y python3-venv
python3 -m venv venv
./venv/bin/pip install -r requirements.txt

# конфіг
cp .env.example .env
nano .env          # впишіть TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, EDESSB_KEEPALIVE_URL
chmod 600 .env
chown -R edessb:edessb /opt/edessb-keeper
```

Служба systemd (автозапуск + авторестарт):

```bash
cp edessb-keeper.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now edessb-keeper
```

Логи:

```bash
journalctl -u edessb-keeper -f
```

## 3. Передати кукі

У Telegram напишіть боту:

```
/start
/setcookies <ваш рядок кукі>
```

Звідки взяти кукі: у браузері, де ви залогінені в ЄДЕССБ → DevTools (F12) →
Network → оберіть будь-який запит до сайту → Request Headers → скопіюйте
значення заголовка `Cookie` цілком і вставте після `/setcookies`.

Підтримуються два формати:

- сирий рядок: `/setcookies sessionid=abc; csrftoken=xyz; ...`
- JSON: `/setcookies {"sessionid":"abc","csrftoken":"xyz"}`

Коли сесія впаде, бот напише про це — просто надішліть свіжі кукі тією ж
командою `/setcookies`.

## 4. Команди бота

| Команда | Дія |
|---|---|
| `/start` | привʼязати чат |
| `/setcookies <...>` | задати/оновити кукі |
| `/check` | перевірити сесію зараз |
| `/status` | поточний стан, остання перевірка |
| `/pause` / `/resume` | вимкнути/увімкнути сповіщення |
| `/help` | довідка |

## 5. Налаштування точності

ЄДЕССБ може віддавати 200 навіть на сторінці логіну. Щоб визначення стану було
надійним, у `.env` задайте один з маркерів:

- `EDESSB_OK_MARKER` — текст, що є **лише** коли ви залогінені (ваше ПІБ, ЄДРПОУ,
  «Вийти» тощо);
- `EDESSB_LOGIN_MARKER` — текст зі сторінки входу (напр. `Увійти через id.gov.ua`).

І вкажіть `EDESSB_KEEPALIVE_URL` саме на кабінетну сторінку, що вимагає входу,
а не на публічну головну.

> Примітка: якщо ЄДЕССБ використовує короткоживучі токени або звʼязує сесію з
> IP/User-Agent, керуйтесь тим самим `EDESSB_USER_AGENT`, що й у вашому браузері.
> Скрипт лише надсилає ваші власні кукі — це подовжує життя сесії, але не
> обходить саму авторизацію.

---

## Шлюз (gateway.py) — проксі для ваших сайтів

Якщо кілька ваших сайтів/скриптів тягнуть дані з порталу, їм не треба кожному
тримати власну сесію. Шлюз тримає **одну** авторизовану сесію (ті самі кукі, що
підтримує бот) і проксує запити:

```
ваші сайти  ->  gateway (дроплет, тримає сесію)  ->  admin.e-construction.gov.ua
```

Кукі читаються з `cookies.txt` при кожному запиті, тож щойно ви оновите їх через
Telegram (`/setcookies`), шлюз одразу ходить із новими.

### Запуск

1. Згенеруйте ключ і впишіть у `.env`:

   ```bash
   openssl rand -hex 32
   # -> GATEWAY_API_KEY=...
   ```

2. Встановіть службу:

   ```bash
   cp /opt/edessb-keeper/edessb-gateway.service /etc/systemd/system/
   systemctl daemon-reload
   systemctl enable --now edessb-gateway
   journalctl -u edessb-gateway -f
   ```

### Виклик із вашого сайту

Шлях і query просто повторюють портал, лише додається ключ:

```bash
curl -H "X-API-Key: ВАШ_КЛЮЧ" \
     "http://127.0.0.1:8080/project_documentation_filter?param=value"
```

Перевірка стану (без ключа):

```bash
curl http://127.0.0.1:8080/healthz      # -> ok cookies=N
```

### Доступ з інших серверів

За замовчуванням шлюз слухає `127.0.0.1` — лише застосунки на цьому ж дроплеті.
Якщо ваші сайти на інших серверах:

- `GATEWAY_BIND=0.0.0.0` у `.env`;
- відкрийте порт лише для потрібних IP (`ufw allow from <IP> to any port 8080`);
- краще поставте попереду TLS (nginx/Caddy з Let's Encrypt), щоб ключ і дані не
  йшли відкритим HTTP.

### Безпека

- без `GATEWAY_API_KEY` сервіс не стартує;
- за замовчуванням дозволені лише `GET,HEAD` (читання) — запис вмикається через
  `GATEWAY_ALLOW_METHODS`;
- можна обмежити шляхи через `GATEWAY_ALLOW_PREFIXES`.
