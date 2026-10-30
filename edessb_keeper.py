#!/usr/bin/env python3
"""
ЄДЕССБ session keeper + Telegram bot.

Що робить:
  * тримає вашу сесію ЄДЕССБ "живою", періодично звертаючись до захищеного URL
    з вашими кукі (keep-alive ping);
  * приймає / оновлює кукі через Telegram (команда /setcookies);
  * якщо сесія впала (редірект на логін / 401 / 403 / зник маркер) — надсилає
    сповіщення у Telegram; коли відновиться — теж повідомляє.

Залежності: лише `requests`.
Конфігурація — через змінні середовища (див. .env.example).
"""

import json
import os
import threading
import time
import traceback
from datetime import datetime, timezone

import requests

# --------------------------------------------------------------------------- #
# Конфігурація
# --------------------------------------------------------------------------- #
BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
# Якщо задано — бот слухає тільки цей chat_id. Якщо порожньо — "прив'яжеться"
# до першого, хто надішле /start, і запише його у STATE_FILE.
ADMIN_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "").strip()

# URL, до якого ходимо, щоб і підтримати сесію, і перевірити, чи вона жива.
# Має бути сторінка/ендпоінт, що вимагає авторизації.
KEEPALIVE_URL = os.environ.get(
    "EDESSB_KEEPALIVE_URL",
    "https://e-construction.gov.ua/",
).strip()

# Як часто пінгувати сесію, секунд (за замовчуванням 5 хв).
CHECK_INTERVAL = int(os.environ.get("CHECK_INTERVAL", "300"))

# Таймаут HTTP-запиту до ЄДЕССБ.
HTTP_TIMEOUT = int(os.environ.get("HTTP_TIMEOUT", "30"))

# Якщо у відповіді є цей рядок — сесія жива (напр. ваше ПІБ/ЄДРПОУ). Необов'язково.
OK_MARKER = os.environ.get("EDESSB_OK_MARKER", "").strip()
# Якщо у відповіді є цей рядок — сесія впала (напр. "Увійти"/"Вхід"). Необов'язково.
LOGIN_MARKER = os.environ.get("EDESSB_LOGIN_MARKER", "").strip()

USER_AGENT = os.environ.get(
    "EDESSB_USER_AGENT",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
).strip()

# Файли стану (поряд зі скриптом за замовчуванням).
BASE_DIR = os.environ.get("EDESSB_DATA_DIR", os.path.dirname(os.path.abspath(__file__)))
COOKIE_FILE = os.path.join(BASE_DIR, "cookies.txt")
STATE_FILE = os.path.join(BASE_DIR, "state.json")

TG_API = f"https://api.telegram.org/bot{BOT_TOKEN}"


# --------------------------------------------------------------------------- #
# Невеличкий потокобезпечний стан
# --------------------------------------------------------------------------- #
class State:
    def __init__(self):
        self.lock = threading.Lock()
        self.chat_id = ADMIN_CHAT_ID or None
        self.cookies = ""          # сирий заголовок Cookie або JSON-рядок
        self.alive = None          # None/True/False — останній відомий стан сесії
        self.last_check = None     # datetime останньої перевірки
        self.last_detail = ""      # текст останнього результату
        self.paused = False        # призупинити сповіщення
        self._load()

    def _load(self):
        try:
            with open(COOKIE_FILE, "r", encoding="utf-8") as fh:
                self.cookies = fh.read().strip()
        except FileNotFoundError:
            pass
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            self.chat_id = self.chat_id or data.get("chat_id")
            self.paused = data.get("paused", False)
        except (FileNotFoundError, json.JSONDecodeError):
            pass

    def save_cookies(self, raw):
        with self.lock:
            self.cookies = raw.strip()
        with open(COOKIE_FILE, "w", encoding="utf-8") as fh:
            fh.write(self.cookies)
        os.chmod(COOKIE_FILE, 0o600)

    def save_state(self):
        data = {"chat_id": self.chat_id, "paused": self.paused}
        with open(STATE_FILE, "w", encoding="utf-8") as fh:
            json.dump(data, fh)


STATE = State()


# --------------------------------------------------------------------------- #
# Утиліти
# --------------------------------------------------------------------------- #
def now():
    return datetime.now(timezone.utc).astimezone()


def ts():
    return now().strftime("%Y-%m-%d %H:%M:%S %Z")


def log(msg):
    print(f"[{ts()}] {msg}", flush=True)


def cookies_to_dict(raw):
    """Підтримує як JSON {"name":"value"}, так і сирий заголовок 'a=1; b=2'."""
    raw = (raw or "").strip()
    if not raw:
        return {}
    if raw.startswith("{"):
        try:
            return {str(k): str(v) for k, v in json.loads(raw).items()}
        except json.JSONDecodeError:
            pass
    jar = {}
    for part in raw.split(";"):
        part = part.strip()
        if "=" in part:
            k, v = part.split("=", 1)
            jar[k.strip()] = v.strip()
    return jar


# --------------------------------------------------------------------------- #
# Telegram
# --------------------------------------------------------------------------- #
def tg_send(chat_id, text):
    if not chat_id:
        return
    try:
        requests.post(
            f"{TG_API}/sendMessage",
            json={"chat_id": chat_id, "text": text, "disable_web_page_preview": True},
            timeout=HTTP_TIMEOUT,
        )
    except requests.RequestException as exc:
        log(f"Telegram send error: {exc}")


def notify(text):
    """Сповіщення адміну (з урахуванням паузи)."""
    if STATE.paused:
        return
    tg_send(STATE.chat_id, text)


# --------------------------------------------------------------------------- #
# Перевірка сесії ЄДЕССБ
# --------------------------------------------------------------------------- #
def check_session():
    """
    Повертає (alive: bool, detail: str).
    Логіка:
      * запит без автоматичних редіректів;
      * 3xx на логін → сесія впала;
      * 401/403 → впала;
      * якщо задано LOGIN_MARKER і він у тілі → впала;
      * якщо задано OK_MARKER і його немає у тілі → впала;
      * інакше 200 → жива.
    """
    jar = cookies_to_dict(STATE.cookies)
    if not jar:
        return False, "Кукі не задано. Надішліть /setcookies <cookie>."

    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "uk-UA,uk;q=0.9,en;q=0.8",
    }
    try:
        resp = requests.get(
            KEEPALIVE_URL,
            headers=headers,
            cookies=jar,
            timeout=HTTP_TIMEOUT,
            allow_redirects=False,
        )
    except requests.RequestException as exc:
        return False, f"Помилка мережі: {exc}"

    code = resp.status_code

    if code in (301, 302, 303, 307, 308):
        location = resp.headers.get("Location", "")
        low = location.lower()
        if any(w in low for w in ("login", "signin", "auth", "logout", "id.gov.ua")):
            return False, f"Редірект на авторизацію ({code} → {location})"
        return True, f"Редірект {code} → {location} (не схоже на логін)"

    if code in (401, 403):
        return False, f"HTTP {code} — немає доступу (сесія недійсна)"

    if code >= 500:
        return False, f"HTTP {code} — помилка на сервері ЄДЕССБ"

    if code == 200:
        body = resp.text or ""
        if LOGIN_MARKER and LOGIN_MARKER in body:
            return False, f"У відповіді знайдено маркер логіну: «{LOGIN_MARKER}»"
        if OK_MARKER and OK_MARKER not in body:
            return False, f"У відповіді відсутній маркер сесії: «{OK_MARKER}»"
        return True, f"HTTP 200, {len(body)} байт — сесія жива"

    return False, f"Неочікуваний HTTP {code}"


# --------------------------------------------------------------------------- #
# Фоновий цикл keep-alive
# --------------------------------------------------------------------------- #
def keepalive_loop():
    log(f"Keep-alive loop запущено. URL={KEEPALIVE_URL}, інтервал={CHECK_INTERVAL}с")
    while True:
        try:
            alive, detail = check_session()
            STATE.last_check = now()
            STATE.last_detail = detail
            log(f"check: alive={alive} :: {detail}")

            prev = STATE.alive
            STATE.alive = alive

            if prev is None:
                # Перший запуск — повідомимо лише якщо впала.
                if not alive:
                    notify(f"⚠️ ЄДЕССБ: сесія НЕ активна.\n{detail}\n\n{ts()}")
            elif prev and not alive:
                notify(f"🔴 ЄДЕССБ: сесія ВПАЛА!\n{detail}\n\n"
                       f"Надішліть нові кукі: /setcookies <cookie>\n{ts()}")
            elif not prev and alive:
                notify(f"🟢 ЄДЕССБ: сесія відновлена.\n{detail}\n\n{ts()}")

        except Exception:
            log("keepalive_loop error:\n" + traceback.format_exc())
        time.sleep(CHECK_INTERVAL)


# --------------------------------------------------------------------------- #
# Обробка команд Telegram (long polling)
# --------------------------------------------------------------------------- #
HELP = (
    "ЄДЕССБ keeper — команди:\n"
    "/start — привʼязати цей чат\n"
    "/setcookies <cookie> — задати/оновити кукі\n"
    "   (сирий рядок «a=1; b=2» або JSON {\"a\":\"1\"})\n"
    "/check — перевірити сесію зараз\n"
    "/status — поточний стан\n"
    "/pause — призупинити сповіщення\n"
    "/resume — відновити сповіщення\n"
    "/help — ця довідка"
)


def authorized(chat_id):
    return STATE.chat_id is not None and str(chat_id) == str(STATE.chat_id)


def handle_command(chat_id, text):
    text = (text or "").strip()
    cmd, _, arg = text.partition(" ")
    cmd = cmd.lower().split("@", 1)[0]  # прибрати @botname у групах
    arg = arg.strip()

    # /start — привʼязка чату (якщо адмін не зафіксований у env).
    if cmd == "/start":
        if STATE.chat_id is None:
            STATE.chat_id = str(chat_id)
            STATE.save_state()
            tg_send(chat_id, "✅ Чат привʼязано. " + HELP)
        elif authorized(chat_id):
            tg_send(chat_id, HELP)
        else:
            tg_send(chat_id, "Цей бот уже привʼязаний до іншого чату.")
        return

    if not authorized(chat_id):
        tg_send(chat_id, "⛔ Не авторизовано.")
        return

    if cmd in ("/help", "/menu"):
        tg_send(chat_id, HELP)

    elif cmd == "/setcookies":
        if not arg:
            tg_send(chat_id, "Використання: /setcookies <cookie рядок або JSON>")
            return
        STATE.save_cookies(arg)
        jar = cookies_to_dict(arg)
        alive, detail = check_session()
        STATE.alive = alive
        STATE.last_check = now()
        STATE.last_detail = detail
        state_txt = "🟢 жива" if alive else "🔴 не активна"
        tg_send(chat_id, f"✅ Кукі збережено ({len(jar)} шт.).\n"
                         f"Перевірка: {state_txt}\n{detail}")

    elif cmd == "/check":
        alive, detail = check_session()
        STATE.alive = alive
        STATE.last_check = now()
        STATE.last_detail = detail
        state_txt = "🟢 жива" if alive else "🔴 не активна"
        tg_send(chat_id, f"Сесія: {state_txt}\n{detail}\n{ts()}")

    elif cmd == "/status":
        jar = cookies_to_dict(STATE.cookies)
        if STATE.alive is None:
            state_txt = "❓ ще не перевірялась"
        else:
            state_txt = "🟢 жива" if STATE.alive else "🔴 не активна"
        last = STATE.last_check.strftime("%Y-%m-%d %H:%M:%S %Z") if STATE.last_check else "—"
        tg_send(chat_id,
                f"Стан сесії: {state_txt}\n"
                f"Кукі: {len(jar)} шт.\n"
                f"Остання перевірка: {last}\n"
                f"Деталі: {STATE.last_detail or '—'}\n"
                f"Сповіщення: {'⏸ пауза' if STATE.paused else '▶️ увімкнені'}\n"
                f"Інтервал: {CHECK_INTERVAL}с\n"
                f"URL: {KEEPALIVE_URL}")

    elif cmd == "/pause":
        STATE.paused = True
        STATE.save_state()
        tg_send(chat_id, "⏸ Сповіщення призупинено.")

    elif cmd == "/resume":
        STATE.paused = False
        STATE.save_state()
        tg_send(chat_id, "▶️ Сповіщення увімкнено.")

    else:
        tg_send(chat_id, "Невідома команда.\n" + HELP)


def telegram_loop():
    log("Telegram long-polling запущено.")
    offset = None
    while True:
        try:
            params = {"timeout": 30}
            if offset is not None:
                params["offset"] = offset
            resp = requests.get(f"{TG_API}/getUpdates", params=params, timeout=40)
            data = resp.json()
            if not data.get("ok"):
                log(f"getUpdates not ok: {data}")
                time.sleep(5)
                continue
            for upd in data.get("result", []):
                offset = upd["update_id"] + 1
                msg = upd.get("message") or upd.get("edited_message")
                if not msg:
                    continue
                chat_id = msg["chat"]["id"]
                text = msg.get("text", "")
                if text.startswith("/"):
                    handle_command(chat_id, text)
        except requests.RequestException as exc:
            log(f"telegram_loop network error: {exc}")
            time.sleep(5)
        except Exception:
            log("telegram_loop error:\n" + traceback.format_exc())
            time.sleep(5)


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #
def main():
    if not BOT_TOKEN:
        raise SystemExit("Не задано TELEGRAM_BOT_TOKEN.")
    log(f"ЄДЕССБ keeper старт. data_dir={BASE_DIR}")
    if STATE.chat_id:
        notify(f"🤖 ЄДЕССБ keeper запущено.\n{ts()}")

    t = threading.Thread(target=keepalive_loop, daemon=True)
    t.start()
    telegram_loop()  # блокує головний потік


if __name__ == "__main__":
    main()
