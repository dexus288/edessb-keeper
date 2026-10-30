#!/usr/bin/env python3
"""
EDESSB gateway — authenticated reverse proxy.

Тримає вашу авторизовану сесію ЄДЕССБ (ті самі cookies.txt, що підтримує
edessb_keeper.py) і проксує запити ваших сайтів на цільовий портал, підставляючи
кукі + User-Agent. Клієнти авторизуються API-ключем.

Потік: ваші сайти -> gateway (цей сервіс) -> admin.e-construction.gov.ua

Приклад виклику з вашого сайту:
    curl -H "X-API-Key: <GATEWAY_API_KEY>" \\
         "http://127.0.0.1:8080/project_documentation_filter?foo=bar"

Залежність: лише `requests` (та сама, що й у кіпера).
"""

import os
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urljoin

import requests

# --------------------------------------------------------------------------- #
# Конфіг (спільний .env з кіпером)
# --------------------------------------------------------------------------- #
API_KEY = os.environ.get("GATEWAY_API_KEY", "").strip()
BIND = os.environ.get("GATEWAY_BIND", "127.0.0.1").strip()
PORT = int(os.environ.get("GATEWAY_PORT", "8080"))

# База, куди проксуємо. Без завершального шляху — додаємо шлях запиту клієнта.
TARGET_BASE = os.environ.get(
    "GATEWAY_TARGET_BASE",
    "https://admin.e-construction.gov.ua",
).strip().rstrip("/") + "/"

# Необовʼязковий білий список префіксів шляху (через кому). Порожньо = усе дозволено.
_prefixes = os.environ.get("GATEWAY_ALLOW_PREFIXES", "").strip()
ALLOW_PREFIXES = [p.strip() for p in _prefixes.split(",") if p.strip()]

# Дозволені HTTP-методи клієнта.
ALLOW_METHODS = {
    m.strip().upper()
    for m in os.environ.get("GATEWAY_ALLOW_METHODS", "GET,HEAD").split(",")
    if m.strip()
}

HTTP_TIMEOUT = int(os.environ.get("HTTP_TIMEOUT", "30"))

USER_AGENT = os.environ.get(
    "EDESSB_USER_AGENT",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
).strip()

BASE_DIR = os.environ.get("EDESSB_DATA_DIR", os.path.dirname(os.path.abspath(__file__)))
COOKIE_FILE = os.path.join(BASE_DIR, "cookies.txt")

# Заголовки відповіді upstream, які НЕ пробрасуємо назад (керуються транспортом).
HOP_BY_HOP = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailers", "transfer-encoding", "upgrade", "content-encoding",
    "content-length",
}


def log(msg):
    print(msg, flush=True)


def load_cookies():
    """Читаємо cookies.txt при КОЖНОМУ запиті, щоб підхоплювати оновлення з бота."""
    try:
        with open(COOKIE_FILE, "r", encoding="utf-8") as fh:
            raw = fh.read().strip()
    except FileNotFoundError:
        return {}
    if not raw:
        return {}
    if raw.startswith("{"):
        try:
            import json
            return {str(k): str(v) for k, v in json.loads(raw).items()}
        except ValueError:
            pass
    jar = {}
    for part in raw.split(";"):
        part = part.strip()
        if "=" in part:
            k, v = part.split("=", 1)
            jar[k.strip()] = v.strip()
    return jar


class Handler(BaseHTTPRequestHandler):
    server_version = "edessb-gateway"

    # Прибираємо шумне дефолтне логування, лишаємо компактне своє.
    def log_message(self, fmt, *args):
        log(f"{self.address_string()} {fmt % args}")

    # --- helpers ---------------------------------------------------------- #
    def _send_plain(self, code, text):
        body = text.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _authorized(self):
        if not API_KEY:
            return False  # ключ не налаштовано — усе забороняємо
        key = self.headers.get("X-API-Key", "")
        if not key:
            # дозволяємо також ?api_key= для простоти інтеграції
            from urllib.parse import urlparse, parse_qs
            q = parse_qs(urlparse(self.path).query)
            key = (q.get("api_key") or [""])[0]
        return key == API_KEY

    def _path_allowed(self, path):
        if not ALLOW_PREFIXES:
            return True
        return any(path.startswith(p) for p in ALLOW_PREFIXES)

    # --- proxy core ------------------------------------------------------- #
    def _proxy(self):
        # healthcheck без авторизації
        if self.path in ("/healthz", "/health"):
            jar = load_cookies()
            self._send_plain(200, f"ok cookies={len(jar)}")
            return

        if self.command not in ALLOW_METHODS:
            self._send_plain(405, f"method {self.command} not allowed")
            return

        if not self._authorized():
            self._send_plain(401, "unauthorized: bad or missing X-API-Key")
            return

        # шлях + query клієнта -> upstream
        client_path = self.path.lstrip("/")
        if not self._path_allowed("/" + client_path):
            self._send_plain(403, "path not allowed")
            return

        url = urljoin(TARGET_BASE, client_path)

        jar = load_cookies()
        if not jar:
            self._send_plain(
                503,
                "no session cookies yet — set them via the Telegram bot "
                "(/setcookies)",
            )
            return

        headers = {
            "User-Agent": USER_AGENT,
            "Accept": self.headers.get(
                "Accept",
                "text/html,application/json,application/xhtml+xml,"
                "application/xml;q=0.9,*/*;q=0.8",
            ),
            "Accept-Language": "uk-UA,uk;q=0.9,en;q=0.8",
        }

        body = None
        if self.command in ("POST", "PUT", "PATCH"):
            length = int(self.headers.get("Content-Length", 0) or 0)
            if length:
                body = self.rfile.read(length)
            ct = self.headers.get("Content-Type")
            if ct:
                headers["Content-Type"] = ct

        try:
            resp = requests.request(
                self.command,
                url,
                headers=headers,
                cookies=jar,
                data=body,
                timeout=HTTP_TIMEOUT,
                allow_redirects=False,
                stream=True,
            )
        except requests.RequestException as exc:
            self._send_plain(502, f"upstream error: {exc}")
            return

        # Якщо upstream редіректить на логін — сесія, найпевніше, впала.
        if resp.status_code in (301, 302, 303, 307, 308):
            loc = resp.headers.get("Location", "")
            if any(w in loc.lower()
                   for w in ("login", "signin", "auth", "id.gov.ua")):
                resp.close()
                self._send_plain(
                    502,
                    f"session appears down (redirect to {loc}); "
                    "refresh cookies via the Telegram bot",
                )
                return

        content = resp.content  # читаємо (stream=True + .content дочитує тіло)
        self.send_response(resp.status_code)
        for k, v in resp.headers.items():
            if k.lower() in HOP_BY_HOP:
                continue
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(content)
        resp.close()

    # --- method entrypoints ---------------------------------------------- #
    def do_GET(self):
        self._proxy()

    def do_HEAD(self):
        self._proxy()

    def do_POST(self):
        self._proxy()

    def do_PUT(self):
        self._proxy()

    def do_PATCH(self):
        self._proxy()


def main():
    if not API_KEY:
        sys.exit("GATEWAY_API_KEY не задано — відмовляюсь стартувати без ключа.")
    log(f"EDESSB gateway on http://{BIND}:{PORT} -> {TARGET_BASE}")
    log(f"methods={sorted(ALLOW_METHODS)} "
        f"allow_prefixes={ALLOW_PREFIXES or 'ALL'} cookies_file={COOKIE_FILE}")
    httpd = ThreadingHTTPServer((BIND, PORT), Handler)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
