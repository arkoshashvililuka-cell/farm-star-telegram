import os
import time
import requests

TOKEN = os.environ["BOT_TOKEN"]
API = f"https://api.telegram.org/bot{TOKEN}"

WEB_APP_URL = "https://jolly-wave-93e2.vpr4t6mfc6.workers.dev/"


def api(method, data=None):
    r = requests.post(
        f"{API}/{method}",
        json=data or {},
        timeout=40
    )
    r.raise_for_status()
    return r.json()["result"]


def send(chat_id, text, keyboard=None):
    data = {
        "chat_id": chat_id,
        "text": text
    }

    if keyboard:
        data["reply_markup"] = keyboard

    api("sendMessage", data)


def app_button():
    return {
        "inline_keyboard": [[
            {
                "text": "🚀 Открыть Farm Stars",
                "web_app": {
                    "url": WEB_APP_URL
                }
            }
        ]]
    }


def handle(update):
    message = update.get("message")

    if not message:
        return

    chat_id = message["chat"]["id"]
    text = message.get("text", "")

    if text.startswith("/start"):
        send(
            chat_id,
            "⭐ Добро пожаловать в Farm Stars!\n\n"
            "Нажми кнопку ниже, чтобы открыть приложение.",
            app_button()
        )

    elif text.startswith("/app"):
        send(
            chat_id,
            "🚀 Открывай Farm Stars:",
            app_button()
        )

    elif text.startswith("/balance"):
        send(
            chat_id,
            "⭐ Твой баланс Farm Stars: 0"
        )

    elif text.startswith("/profile"):
        user = message.get("from", {})
        name = user.get("first_name", "Farmer")

        send(
            chat_id,
            f"👤 Профиль\n\n"
            f"Имя: {name}\n"
            f"Telegram ID: {user.get('id')}\n\n"
            "⭐ Farm Stars: 0"
        )

    elif text.startswith("/help"):
        send(
            chat_id,
            "📚 Команды:\n\n"
            "/start — открыть Farm Stars\n"
            "/app — открыть приложение\n"
            "/balance — баланс\n"
            "/profile — профиль\n"
            "/help — помощь"
        )


def main():
    api("deleteWebhook")

    offset = 0

    print("Farm Stars bot started")

    while True:
        try:
            updates = api(
                "getUpdates",
                {
                    "offset": offset,
                    "timeout": 30
                }
            )

            for update in updates:
                offset = update["update_id"] + 1
                handle(update)

        except Exception as e:
            print("Error:", e)
            time.sleep(3)


if __name__ == "__main__":
    main()
