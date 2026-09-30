import os
import time
import sqlite3
import threading
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import requests


# =========================================================
# НАСТРОЙКИ
# =========================================================

TOKEN = os.environ.get("BOT_TOKEN")

if not TOKEN:
    raise RuntimeError("BOT_TOKEN не найден в Environment Variables")

API = f"https://api.telegram.org/bot{TOKEN}"

PORT = int(os.environ.get("PORT", "10000"))

WEB_APP_URL = os.environ.get(
    "WEB_APP_URL",
    "https://farm-star-telegram.onrender.com/"
).rstrip("/") + "/"

BASE_DIR = Path(__file__).resolve().parent

# SQLite база
DB_FILE = BASE_DIR / "farm_stars.db"


# =========================================================
# ПОИСК index.html
# =========================================================

SEARCH_DIRS = [
    BASE_DIR,
    Path.cwd(),
    Path("/app"),
    Path("/opt/render/project/src"),
    Path("/opt/render/project/src/public"),
    Path("/opt/render/project/src/static"),
]

INDEX_FILE = None


def find_index_file():
    """Ищет index.html в проекте."""

    # Сначала проверяем основные места
    for directory in SEARCH_DIRS:

        try:
            file = directory / "index.html"

            if file.is_file():
                return file.resolve()

        except Exception:
            pass

    # Если не нашли — ищем глубже
    for directory in [
        BASE_DIR,
        Path.cwd(),
        Path("/app"),
        Path("/opt/render/project/src"),
    ]:

        try:

            if not directory.exists():
                continue

            for file in directory.rglob("index.html"):

                if file.is_file():
                    return file.resolve()

        except Exception as e:

            print(
                f"[WEB] Ошибка поиска: {e}",
                flush=True
            )

    return None


def refresh_index():

    global INDEX_FILE

    INDEX_FILE = find_index_file()

    if INDEX_FILE:

        print(
            f"[WEB] index.html найден: {INDEX_FILE}",
            flush=True
        )

    else:

        print(
            "[WEB] index.html НЕ НАЙДЕН",
            flush=True
        )


# =========================================================
# DATABASE
# =========================================================

db_lock = threading.Lock()


def db_connect():

    connection = sqlite3.connect(
        DB_FILE,
        check_same_thread=False
    )

    connection.row_factory = sqlite3.Row

    return connection


def init_database():

    with db_lock:

        db = db_connect()

        db.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                telegram_id INTEGER PRIMARY KEY,
                username TEXT,
                first_name TEXT,
                stars INTEGER DEFAULT 50,
                coins INTEGER DEFAULT 100,
                created_at INTEGER
            )
            """
        )

        db.commit()
        db.close()

    print(
        f"[DB] Database: {DB_FILE}",
        flush=True
    )


def register_user(user):

    telegram_id = user.get("id")

    if not telegram_id:
        return False

    username = user.get(
        "username",
        ""
    )

    first_name = user.get(
        "first_name",
        "Farmer"
    )

    with db_lock:

        db = db_connect()

        existing = db.execute(
            """
            SELECT telegram_id
            FROM users
            WHERE telegram_id = ?
            """,
            (telegram_id,)
        ).fetchone()

        if existing:

            db.execute(
                """
                UPDATE users
                SET username = ?,
                    first_name = ?
                WHERE telegram_id = ?
                """,
                (
                    username,
                    first_name,
                    telegram_id
                )
            )

            db.commit()
            db.close()

            return False

        db.execute(
            """
            INSERT INTO users
            (
                telegram_id,
                username,
                first_name,
                stars,
                coins,
                created_at
            )
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                telegram_id,
                username,
                first_name,
                50,
                100,
                int(time.time())
            )
        )

        db.commit()
        db.close()

    return True


def get_user(telegram_id):

    with db_lock:

        db = db_connect()

        user = db.execute(
            """
            SELECT *
            FROM users
            WHERE telegram_id = ?
            """,
            (telegram_id,)
        ).fetchone()

        db.close()

    return user


def get_users_count():

    with db_lock:

        db = db_connect()

        result = db.execute(
            """
            SELECT COUNT(*) AS count
            FROM users
            """
        ).fetchone()

        db.close()

    return int(result["count"])


# =========================================================
# WEB SERVER
# =========================================================

class WebHandler(BaseHTTPRequestHandler):

    def send_text(
        self,
        status,
        text,
        content_type="text/plain; charset=utf-8"
    ):

        data = text.encode("utf-8")

        self.send_response(status)

        self.send_header(
            "Content-Type",
            content_type
        )

        self.send_header(
            "Content-Length",
            str(len(data))
        )

        self.send_header(
            "Cache-Control",
            "no-cache"
        )

        self.end_headers()

        self.wfile.write(data)

    def do_GET(self):

        path = self.path.split("?")[0]

        print(
            f"[WEB] GET {path}",
            flush=True
        )

        # -------------------------------------------------
        # HEALTH
        # -------------------------------------------------

        if path == "/health":

            response = (
                '{"status":"ok",'
                '"app":"Farm Stars",'
                '"index_found":'
                + (
                    "true"
                    if INDEX_FILE
                    else "false"
                )
                + "}"
            )

            self.send_text(
                200,
                response,
                "application/json; charset=utf-8"
            )

            return

        # -------------------------------------------------
        # ГЛАВНАЯ СТРАНИЦА
        # -------------------------------------------------

        if path in ("/", "/index.html"):

            if not INDEX_FILE or not INDEX_FILE.is_file():

                refresh_index()

            if not INDEX_FILE:

                self.send_text(
                    500,
                    "ERROR: index.html not found\n\n"
                    f"BASE_DIR: {BASE_DIR}\n"
                    f"CURRENT_DIR: {Path.cwd()}"
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

                self.send_header(
                    "Cache-Control",
                    "no-cache"
                )

                self.end_headers()

                self.wfile.write(content)

            except Exception as e:

                self.send_text(
                    500,
                    f"ERROR reading index.html: {e}"
                )

            return

        # -------------------------------------------------
        # FAVICON
        # -------------------------------------------------

        if path == "/favicon.ico":

            self.send_response(204)
            self.end_headers()

            return

        # -------------------------------------------------
        # 404
        # -------------------------------------------------

        self.send_text(
            404,
            "404 Not Found"
        )

    def log_message(self, format, *args):

        print(
            "[HTTP]",
            format % args,
            flush=True
        )


def start_web_server():

    refresh_index()

    server = ThreadingHTTPServer(
        ("0.0.0.0", PORT),
        WebHandler
    )

    print(
        f"[WEB] Server запущен на порту {PORT}",
        flush=True
    )

    print(
        f"[WEB] Mini App: {WEB_APP_URL}",
        flush=True
    )

    server.serve_forever()


# =========================================================
# TELEGRAM API
# =========================================================

def api(method, data=None):

    response = requests.post(
        f"{API}/{method}",
        json=data or {},
        timeout=40
    )

    response.raise_for_status()

    result = response.json()

    if not result.get("ok"):

        raise RuntimeError(
            str(result)
        )

    return result["result"]


# =========================================================
# КНОПКИ
# =========================================================

def main_keyboard():

    return {
        "keyboard": [
            [
                {
                    "text": "💰 Баланс"
                },
                {
                    "text": "🎮 Играть"
                }
            ],
            [
                {
                    "text": "☰ Меню"
                }
            ]
        ],
        "resize_keyboard": True
    }


def games_keyboard():

    return {
        "inline_keyboard": [

            [
                {
                    "text": "🚀 Rocket",
                    "web_app": {
                        "url": WEB_APP_URL
                    }
                }
            ],

            [
                {
                    "text": "💣 Mines",
                    "web_app": {
                        "url": WEB_APP_URL
                    }
                }
            ],

            [
                {
                    "text": "📈 Higher / Lower",
                    "web_app": {
                        "url": WEB_APP_URL
                    }
                }
            ],

            [
                {
                    "text": "🎡 Lucky Spin",
                    "web_app": {
                        "url": WEB_APP_URL
                    }
                }
            ],

            [
                {
                    "text": "🌾 Открыть Farm Stars",
                    "web_app": {
                        "url": WEB_APP_URL
                    }
                }
            ]

        ]
    }


def menu_keyboard():

    return {
        "inline_keyboard": [

            [
                {
                    "text": "🌾 Ферма",
                    "web_app": {
                        "url": WEB_APP_URL
                    }
                },
                {
                    "text": "🎁 Daily",
                    "web_app": {
                        "url": WEB_APP_URL
                    }
                }
            ],

            [
                {
                    "text": "🎡 Lucky Spin",
                    "web_app": {
                        "url": WEB_APP_URL
                    }
                },
                {
                    "text": "💣 Mines",
                    "web_app": {
                        "url": WEB_APP_URL
                    }
                }
            ],

            [
                {
                    "text": "📈 Higher / Lower",
                    "web_app": {
                        "url": WEB_APP_URL
                    }
                },
                {
                    "text": "🚀 Rocket",
                    "web_app": {
                        "url": WEB_APP_URL
                    }
                }
            ],

            [
                {
                    "text": "👤 Профиль",
                    "web_app": {
                        "url": WEB_APP_URL
                    }
                },
                {
                    "text": "👥 Рефералы",
                    "web_app": {
                        "url": WEB_APP_URL
                    }
                }
            ],

            [
                {
                    "text": "🏆 Рейтинг",
                    "web_app": {
                        "url": WEB_APP_URL
                    }
                },
                {
                    "text": "💱 Обмен",
                    "web_app": {
                        "url": WEB_APP_URL
                    }
                }
            ]

        ]
    }


# =========================================================
# ОТПРАВКА
# =========================================================

def send(
    chat_id,
    text,
    reply_markup=None
):

    data = {
        "chat_id": chat_id,
        "text": text
    }

    if reply_markup is not None:

        data["reply_markup"] = reply_markup

    api(
        "sendMessage",
        data
    )


# =========================================================
# BALANCE
# =========================================================

def balance_text(telegram_id):

    user = get_user(
        telegram_id
    )

    if not user:

        return (
            "💰 Баланс\n\n"
            "⭐ Farm Stars: 50\n"
            "🪙 Coins: 100"
        )

    return (
        "💰 Твой баланс\n\n"
        f"⭐ Farm Stars: {user['stars']}\n"
        f"🪙 Coins: {user['coins']}"
    )


# =========================================================
# HANDLE UPDATE
# =========================================================

def handle(update):

    message = update.get("message")

    if not message:
        return

    chat = message.get(
        "chat",
        {}
    )

    user = message.get(
        "from",
        {}
    )

    chat_id = chat.get(
        "id"
    )

    if not chat_id:
        return

    # Регистрируем пользователя
    is_new = register_user(
        user
    )

    text = message.get(
        "text",
        ""
    ).strip()

    # =====================================================
    # /start
    # =====================================================

    if text.startswith("/start"):

        name = user.get(
            "first_name",
            "Farmer"
        )

        users_count = get_users_count()

        if is_new:

            extra = (
                "\n\n🎉 Ты новый игрок!"
                "\n⭐ Стартовый бонус: 50 Stars"
                "\n🪙 Coins: 100"
            )

        else:

            extra = ""

        send(
            chat_id,

            f"⭐ Добро пожаловать в Farm Stars, {name}!\n\n"
            "🌾 Ферма, мини-игры и награды ждут тебя.\n\n"
            f"👥 Игроков: {users_count}"
            f"{extra}\n\n"
            "Выбери действие ниже 👇",

            main_keyboard()
        )

        return

    # =====================================================
    # БАЛАНС
    # =====================================================

    if (
        text == "💰 Баланс"
        or text.startswith("/balance")
    ):

        send(
            chat_id,

            balance_text(
                user.get("id")
            ),

            main_keyboard()
        )

        return

    # =====================================================
    # ИГРАТЬ
    # =====================================================

    if (
        text == "🎮 Играть"
        or text.startswith("/app")
    ):

        send(
            chat_id,

            "🎮 Мини-игры Farm Stars\n\n"
            "Выбери игру:",

            games_keyboard()
        )

        return

    # =====================================================
    # МЕНЮ
    # =====================================================

    if (
        text == "☰ Меню"
    ):

        users_count = get_users_count()

        send(
            chat_id,

            "☰ Меню Farm Stars\n\n"
            f"👥 Всего игроков: {users_count}\n\n"
            "Выбери раздел:",

            menu_keyboard()
        )

        return

    # =====================================================
    # PROFILE
    # =====================================================

    if text.startswith("/profile"):

        name = user.get(
            "first_name",
            "Farmer"
        )

        username = user.get(
            "username",
            ""
        )

        username_text = (
            f"@{username}"
            if username
            else "не указан"
        )

        db_user = get_user(
            user.get("id")
        )

        stars = (
            db_user["stars"]
            if db_user
            else 50
        )

        coins = (
            db_user["coins"]
            if db_user
            else 100
        )

        send(
            chat_id,

            "👤 Профиль\n\n"
            f"Имя: {name}\n"
            f"Username: {username_text}\n"
            f"Telegram ID: {user.get('id')}\n\n"
            f"⭐ Farm Stars: {stars}\n"
            f"🪙 Coins: {coins}\n\n"
            f"👥 Игроков в Farm Stars: "
            f"{get_users_count()}",

            main_keyboard()
        )

        return

    # =====================================================
    # HELP
    # =====================================================

    if text.startswith("/help"):

        send(
            chat_id,

            "📚 Farm Stars\n\n"
            "/start — главное меню\n"
            "/app — мини-игры\n"
            "/balance — баланс\n"
            "/profile — профиль\n"
            "/help — помощь",

            main_keyboard()
        )

        return

    # =====================================================
    # НЕИЗВЕСТНАЯ КОМАНДА
    # =====================================================

    if text.startswith("/"):

        send(
            chat_id,

            "❓ Неизвестная команда.\n\n"
            "Используй /help.",

            main_keyboard()
        )

        return


# =========================================================
# BOT
# =========================================================

def main():

    print(
        "======================================",
        flush=True
    )

    print(
        "⭐ FARM STARS BOT",
        flush=True
    )

    print(
        "======================================",
        flush=True
    )

    print(
        f"[BOT] Mini App: {WEB_APP_URL}",
        flush=True
    )

    # База
    init_database()

    # Удаляем webhook
    try:

        api(
            "deleteWebhook",
            {
                "drop_pending_updates": False
            }
        )

        print(
            "[BOT] Webhook удалён",
            flush=True
        )

    except Exception as e:

        print(
            f"[BOT] Webhook error: {e}",
            flush=True
        )

    # Проверяем Telegram
    try:

        bot = api(
            "getMe"
        )

        print(
            f"[BOT] Подключён: "
            f"@{bot.get('username')}",
            flush=True
        )

    except Exception as e:

        print(
            f"[BOT] Telegram connection error: {e}",
            flush=True
        )

        raise

    print(
        f"[BOT] Пользователей в базе: "
        f"{get_users_count()}",
        flush=True
    )

    offset = 0

    # =====================================================
    # LOOP
    # =====================================================

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

                offset = (
                    update["update_id"] + 1
                )

                try:

                    handle(
                        update
                    )

                except Exception as e:

                    print(
                        f"[BOT] Update error: {e}",
                        flush=True
                    )

        except Exception as e:

            print(
                f"[BOT] Telegram error: {e}",
                flush=True
            )

            time.sleep(3)


# =========================================================
# START
# =========================================================

if __name__ == "__main__":

    web_thread = threading.Thread(
        target=start_web_server,
        daemon=True
    )

    web_thread.start()

    time.sleep(1)

    main()
