import os
import time
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import requests


# =========================
# SETTINGS
# =========================

TOKEN = os.environ["BOT_TOKEN"]
API = f"https://api.telegram.org/bot{TOKEN}"

PORT = int(os.environ.get("PORT", 10000))

BASE_DIR = Path(__file__).resolve().parent
INDEX_FILE = BASE_DIR / "index.html"

WEB_APP_URL = "https://farm-star-telegram.onrender.com/"


# =========================
# WEB SERVER
# =========================

class WebHandler(BaseHTTPRequestHandler):

    def do_GET(self):
        path = self.path.split("?")[0]

        # Главная страница Mini App
        if path == "/" or path == "/index.html":
            if not INDEX_FILE.exists():
                self.send_response(500)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.end_headers()
                self.wfile.write(
                    b"ERROR: index.html not found"
                )
                return

            try:
                content = INDEX_FILE.read_bytes()

                self.send_response(200)
                self.send_header(
                    "Content-Type",
                    "text/html; charset=utf-8"
                )
                self.send_header(
                    "Content-Length",
                    str(len(content))
                )
                self.end_headers()

                self.wfile.write(content)

            except Exception as e:
                self.send_response(500)
                self.send_header(
                    "Content-Type",
                    "text/plain; charset=utf-8"
                )
                self.end_headers()

                self.wfile.write(
                    f"ERROR: {e}".encode("utf-8")
                )

            return

        # Health check Render
        if path == "/health":
            response = b'{"status":"ok","app":"Farm Stars"}'

            self.send_response(200)
            self.send_header(
                "Content-Type",
                "application/json; charset=utf-8"
            )
            self.send_header(
                "Content-Length",
                str(len(response))
            )
            self.end_headers()

            self.wfile.write(response)
            return

        # Все неизвестные страницы
        self.send_response(404)
        self.send_header(
            "Content-Type",
            "text/plain; charset=utf-8"
        )
        self.end_headers()

        self.wfile.write(b"404 Not Found")

    def log_message(self, format, *args):
        pass


def start_server():
    server = HTTPServer(
        ("0.0.0.0", PORT),
        WebHandler
    )

    print(f"Web server started on port {PORT}")
    print(f"Index file: {INDEX_FILE}")

    server.serve_forever()


# =========================
# TELEGRAM API
# =========================

def api(method, data=None):
    response = requests.post(
        f"{API}/{method}",
        json=data or {},
        timeout=40
    )

    response.raise_for_status()

    result = response.json()

    if not result.get("ok"):
        raise Exception(result)

    return result["result"]


# =========================
# SEND MESSAGE
# =========================

def send(chat_id, text, keyboard=None):
    data = {
        "chat_id": chat_id,
        "text": text
    }

    if keyboard:
        data["reply_markup"] = keyboard

    api("sendMessage", data)


# =========================
# MINI APP BUTTON
# =========================

def app_button():
    return {
        "inline_keyboard": [
            [
                {
                    "text": "🚀 Открыть Farm Stars",
                    "web_app": {
                        "url": WEB_APP_URL
                    }
                }
            ]
        ]
    }


# =========================
# UPDATE HANDLER
# =========================

def handle(update):

    message = update.get("message")

    if not message:
        return

    chat = message.get("chat", {})
    user = message.get("from", {})

    chat_id = chat.get("id")

    if not chat_id:
        return

    text = message.get("text", "").strip()

    # /start
    if text.startswith("/start"):

        name = user.get("first_name", "Farmer")

        send(
            chat_id,
            f"⭐ Добро пожаловать в Farm Stars, {name}!\n\n"
            "Твоя ферма, мини-игры и Farm Stars ждут тебя.\n\n"
            "Нажми кнопку ниже:",
            app_button()
        )

    # /app
    elif text.startswith("/app"):

        send(
            chat_id,
            "🚀 Открывай Farm Stars:",
            app_button()
        )

    # /balance
    elif text.startswith("/balance"):

        send(
            chat_id,
            "⭐ Твой баланс Farm Stars: 0"
        )

    # /profile
    elif text.startswith("/profile"):

        name = user.get("first_name", "Farmer")
        username = user.get("username", "")

        username_text = (
            f"@{username}"
            if username
            else "не указан"
        )

        send(
            chat_id,
            "👤 Профиль\n\n"
            f"Имя: {name}\n"
            f"Username: {username_text}\n"
            f"Telegram ID: {user.get('id')}\n\n"
            "⭐ Farm Stars: 0"
        )

    # /help
    elif text.startswith("/help"):

        send(
            chat_id,
            "📚 Команды Farm Stars:\n\n"
            "/start — открыть Farm Stars\n"
            "/app — открыть приложение\n"
            "/balance — баланс\n"
            "/profile — профиль\n"
            "/help — помощь"
        )


# =========================
# TELEGRAM BOT
# =========================

def main():

    print("Farm Stars bot starting...")

    try:
        api("deleteWebhook")
        print("Webhook deleted")
    except Exception as e:
        print("Webhook error:", e)

    offset = 0

    while True:

        try:

            updates = api(
                "getUpdates",
                {
                    "offset": offset,
                    "timeout": 30,
                    "allowed_updates": [
                        "message"
                    ]
                }
            )

            for update in updates:

                offset = update["update_id"] + 1

                try:
                    handle(update)

                except Exception as e:
                    print(
                        "Update error:",
                        e
                    )

        except Exception as e:

            print(
                "Telegram error:",
                e
            )

            time.sleep(3)


# =========================
# START
# =========================

if __name__ == "__main__":

    web_thread = threading.Thread(
        target=start_server,
        daemon=True
    )

    web_thread.start()

    main()
