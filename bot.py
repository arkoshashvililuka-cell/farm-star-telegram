import os
import json
import time
import uuid
import hmac
import hashlib
import sqlite3
import secrets
import threading
from pathlib import Path
from urllib.parse import urlparse, parse_qs

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError


# ============================================================
# CONFIG
# ============================================================

TOKEN = os.environ["BOT_TOKEN"]

# Твой Render URL
WEB_APP_URL = os.environ.get(
    "WEB_APP_URL",
    "https://farm-star-telegram.onrender.com/"
).rstrip("/")

PORT = int(os.environ.get("PORT", "10000"))

API = f"https://api.telegram.org/bot{TOKEN}"

BASE_DIR = Path(__file__).resolve().parent
INDEX_FILE = BASE_DIR / "index.html"

DB_FILE = Path(
    os.environ.get(
        "DB_FILE",
        str(BASE_DIR / "farm_star.db")
    )
)

# 1 Telegram Star = 1 Farm Star
FARM_STAR_RATE = 1

# Реферальный бонус
REFERRAL_BONUS = 10
REFERRAL_MAX = 5

# Минимальное / максимальное пополнение
MIN_TOPUP = 1
MAX_TOPUP = 100000


# ============================================================
# DATABASE
# ============================================================

db_lock = threading.Lock()


def db():
    conn = sqlite3.connect(
        str(DB_FILE),
        timeout=30,
        check_same_thread=False
    )

    conn.row_factory = sqlite3.Row

    return conn


def init_db():

    with db_lock:

        conn = db()

        conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                telegram_id INTEGER PRIMARY KEY,
                username TEXT DEFAULT '',
                first_name TEXT DEFAULT '',
                last_name TEXT DEFAULT '',
                stars INTEGER NOT NULL DEFAULT 50,
                referrals INTEGER NOT NULL DEFAULT 0,
                referred_by INTEGER,
                created_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL
            )
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS payments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                telegram_id INTEGER NOT NULL,
                amount INTEGER NOT NULL,
                payload TEXT NOT NULL UNIQUE,
                charge_id TEXT UNIQUE,
                status TEXT NOT NULL,
                created_at INTEGER NOT NULL
            )
        """)

        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_payments_charge
            ON payments(charge_id)
        """)

        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_users_referrer
            ON users(referred_by)
        """)

        conn.commit()
        conn.close()


# ============================================================
# TELEGRAM API
# ============================================================

def tg(method, data=None):

    if data is None:
        data = {}

    body = json.dumps(
        data,
        ensure_ascii=False
    ).encode("utf-8")

    req = Request(
        f"{API}/{method}",
        data=body,
        headers={
            "Content-Type": "application/json"
        },
        method="POST"
    )

    try:

        with urlopen(req, timeout=25) as response:

            raw = response.read().decode(
                "utf-8",
                errors="replace"
            )

            result = json.loads(raw)

    except HTTPError as e:

        raw = e.read().decode(
            "utf-8",
            errors="replace"
        )

        raise RuntimeError(
            f"Telegram HTTP {e.code}: {raw}"
        )

    except URLError as e:

        raise RuntimeError(
            f"Telegram connection error: {e}"
        )

    if not result.get("ok"):

        raise RuntimeError(
            f"Telegram API error: "
            f"{result.get('description', 'unknown error')}"
        )

    return result.get("result")


def send_message(
    chat_id,
    text,
    reply_markup=None
):

    data = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML"
    }

    if reply_markup is not None:
        data["reply_markup"] = reply_markup

    return tg(
        "sendMessage",
        data
    )


# ============================================================
# KEYBOARDS
# ============================================================

def games_keyboard():

    return {
        "inline_keyboard": [

            [
                {
                    "text": "🎮 Открыть игры",
                    "web_app": {
                        "url": WEB_APP_URL
                    }
                }
            ],

            [
                {
                    "text": "⭐ Пополнить Farm Stars",
                    "callback_data": "topup_menu"
                }
            ],

            [
                {
                    "text": "👥 Пригласить друзей",
                    "callback_data": "referral"
                }
            ]

        ]
    }


def topup_keyboard():

    return {
        "inline_keyboard": [

            [
                {
                    "text": "⭐ +10",
                    "callback_data": "topup_10"
                },
                {
                    "text": "⭐ +50",
                    "callback_data": "topup_50"
                }
            ],

            [
                {
                    "text": "⭐ +100",
                    "callback_data": "topup_100"
                },
                {
                    "text": "⭐ +500",
                    "callback_data": "topup_500"
                }
            ],

            [
                {
                    "text": "⭐ +1000",
                    "callback_data": "topup_1000"
                }
            ],

            [
                {
                    "text": "🎮 Назад к играм",
                    "web_app": {
                        "url": WEB_APP_URL
                    }
                }
            ]

        ]
    }


# ============================================================
# USERS
# ============================================================

def get_user(user_id):

    with db_lock:

        conn = db()

        row = conn.execute(
            """
            SELECT *
            FROM users
            WHERE telegram_id = ?
            """,
            (int(user_id),)
        ).fetchone()

        conn.close()

    return row


def create_user_if_needed(
    user,
    referral_id=None
):

    user_id = int(user["id"])

    now = int(time.time())

    username = user.get(
        "username",
        ""
    ) or ""

    first_name = user.get(
        "first_name",
        ""
    ) or ""

    last_name = user.get(
        "last_name",
        ""
    ) or ""

    with db_lock:

        conn = db()

        existing = conn.execute(
            """
            SELECT *
            FROM users
            WHERE telegram_id = ?
            """,
            (user_id,)
        ).fetchone()

        if existing:

            conn.execute(
                """
                UPDATE users
                SET username = ?,
                    first_name = ?,
                    last_name = ?,
                    updated_at = ?
                WHERE telegram_id = ?
                """,
                (
                    username,
                    first_name,
                    last_name,
                    now,
                    user_id
                )
            )

            conn.commit()
            conn.close()

            return existing

        referred_by = None

        if referral_id:

            try:

                referral_id = int(
                    referral_id
                )

            except Exception:

                referral_id = None

            if (
                referral_id and
                referral_id != user_id
            ):

                referrer = conn.execute(
                    """
                    SELECT *
                    FROM users
                    WHERE telegram_id = ?
                    """,
                    (referral_id,)
                ).fetchone()

                if referrer:

                    current_count = int(
                        referrer["referrals"]
                    )

                    if current_count < REFERRAL_MAX:

                        referred_by = referral_id

                        # +10 ⭐ пригласившему
                        conn.execute(
                            """
                            UPDATE users
                            SET stars = stars + ?,
                                referrals = referrals + 1,
                                updated_at = ?
                            WHERE telegram_id = ?
                            """,
                            (
                                REFERRAL_BONUS,
                                now,
                                referral_id
                            )
                        )

        conn.execute(
            """
            INSERT INTO users (
                telegram_id,
                username,
                first_name,
                last_name,
                stars,
                referrals,
                referred_by,
                created_at,
                updated_at
            )
            VALUES (?, ?, ?, ?, ?, 0, ?, ?, ?)
            """,
            (
                user_id,
                username,
                first_name,
                last_name,
                50,
                referred_by,
                now,
                now
            )
        )

        conn.commit()

        row = conn.execute(
            """
            SELECT *
            FROM users
            WHERE telegram_id = ?
            """,
            (user_id,)
        ).fetchone()

        conn.close()

    return row


def get_balance(user_id):

    row = get_user(user_id)

    if not row:
        return 0

    return int(row["stars"])


# ============================================================
# REFERRALS
# ============================================================

def referral_link(user_id):

    # start-параметр Telegram
    return (
        f"https://t.me/"
        f"Farme_star_bot"
        f"?start=ref_{user_id}"
    )


def referral_text(user_id):

    row = get_user(user_id)

    count = 0

    if row:
        count = int(
            row["referrals"]
        )

    left = max(
        0,
        REFERRAL_MAX - count
    )

    return (
        "👥 <b>Реферальная программа</b>\n\n"
        f"За каждого нового друга: "
        f"<b>+{REFERRAL_BONUS} ⭐</b>\n\n"
        f"Приглашено: <b>{count}/{REFERRAL_MAX}</b>\n"
        f"Осталось мест: <b>{left}</b>\n\n"
        "Отправь другу эту ссылку:\n"
        f"<code>{referral_link(user_id)}</code>"
    )


# ============================================================
# PAYMENTS
# ============================================================

def create_invoice_for_user(
    user_id,
    amount
):

    amount = int(amount)

    if amount < MIN_TOPUP:
        raise ValueError(
            f"Минимум {MIN_TOPUP} ⭐"
        )

    if amount > MAX_TOPUP:
        raise ValueError(
            f"Максимум {MAX_TOPUP} ⭐"
        )

    # Уникальный payload.
    payload = (
        f"topup:"
        f"{int(user_id)}:"
        f"{amount}:"
        f"{uuid.uuid4().hex}"
    )

    result = tg(
        "createInvoiceLink",
        {
            "title": "Farm Stars",

            "description":
                f"Пополнение баланса "
                f"на {amount} Farm Stars",

            "payload": payload,

            # Для Telegram Stars.
            "provider_token": "",

            "currency": "XTR",

            "prices": [
                {
                    "label": "Farm Stars",
                    "amount": amount
                }
            ]
        }
    )

    with db_lock:

        conn = db()

        conn.execute(
            """
            INSERT INTO payments (
                telegram_id,
                amount,
                payload,
                status,
                created_at
            )
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                int(user_id),
                amount,
                payload,
                "created",
                int(time.time())
            )
        )

        conn.commit()
        conn.close()

    return result


def validate_payment_payload(
    payload,
    user_id
):

    parts = payload.split(":")

    if len(parts) != 4:
        return None

    if parts[0] != "topup":
        return None

    try:

        payload_user_id = int(
            parts[1]
        )

        amount = int(
            parts[2]
        )

    except Exception:

        return None

    if payload_user_id != int(user_id):
        return None

    if amount < MIN_TOPUP:
        return None

    if amount > MAX_TOPUP:
        return None

    return amount


def process_successful_payment(
    message
):

    payment = message.get(
        "successful_payment"
    )

    if not payment:
        return False

    user = message.get(
        "from"
    )

    if not user:
        return False

    user_id = int(
        user["id"]
    )

    currency = payment.get(
        "currency"
    )

    amount = int(
        payment.get(
            "total_amount",
            0
        )
    )

    payload = payment.get(
        "invoice_payload",
        ""
    )

    charge_id = payment.get(
        "telegram_payment_charge_id",
        ""
    )

    # Telegram Stars должны быть XTR.
    if currency != "XTR":
        return False

    # Проверяем payload.
    payload_amount = validate_payment_payload(
        payload,
        user_id
    )

    if payload_amount is None:
        return False

    # Цена в payload должна совпасть
    # с реально оплаченной суммой.
    if amount != payload_amount:
        return False

    if not charge_id:
        return False

    with db_lock:

        conn = db()

        # Защита от двойного начисления.
        already = conn.execute(
            """
            SELECT id
            FROM payments
            WHERE charge_id = ?
            """,
            (charge_id,)
        ).fetchone()

        if already:

            conn.close()

            return True

        payment_row = conn.execute(
            """
            SELECT *
            FROM payments
            WHERE payload = ?
            """,
            (payload,)
        ).fetchone()

        if not payment_row:

            conn.close()

            return False

        # 1 XTR = 1 Farm Star.
        farm_stars = (
            amount *
            FARM_STAR_RATE
        )

        now = int(time.time())

        conn.execute(
            """
            UPDATE users
            SET stars = stars + ?,
                updated_at = ?
            WHERE telegram_id = ?
            """,
            (
                farm_stars,
                now,
                user_id
            )
        )

        conn.execute(
            """
            UPDATE payments
            SET charge_id = ?,
                status = ?,
                created_at = ?
            WHERE payload = ?
            """,
            (
                charge_id,
                "paid",
                now,
                payload
            )
        )

        conn.commit()
        conn.close()

    # Сообщение после успешной оплаты.
    send_message(
        user_id,
        (
            "✅ <b>Оплата получена!</b>\n\n"
            f"Зачислено: "
            f"<b>+{farm_stars} ⭐ Farm Stars</b>\n\n"
            "Курс: <b>1 Telegram Star = "
            "1 Farm Star</b>\n\n"
            "🎮 Можно продолжать игру."
        ),
        games_keyboard()
    )

    return True


def answer_pre_checkout(
    query
):

    query_id = query["id"]

    user = query.get(
        "from",
        {}
    )

    user_id = int(
        user.get("id", 0)
    )

    currency = query.get(
        "currency"
    )

    amount = int(
        query.get(
            "total_amount",
            0
        )
    )

    payload = query.get(
        "invoice_payload",
        ""
    )

    # Проверяем валюту.
    if currency != "XTR":

        tg(
            "answerPreCheckoutQuery",
            {
                "pre_checkout_query_id":
                    query_id,

                "ok": False,

                "error_message":
                    "Оплата должна быть "
                    "в Telegram Stars."
            }
        )

        return

    # Проверяем payload.
    expected_amount = (
        validate_payment_payload(
            payload,
            user_id
        )
    )

    if (
        expected_amount is None or
        expected_amount != amount
    ):

        tg(
            "answerPreCheckoutQuery",
            {
                "pre_checkout_query_id":
                    query_id,

                "ok": False,

                "error_message":
                    "Счёт недействителен. "
                    "Попробуйте создать новый."
            }
        )

        return

    # Всё нормально.
    tg(
        "answerPreCheckoutQuery",
        {
            "pre_checkout_query_id":
                query_id,

            "ok": True
        }
    )


# ============================================================
# TELEGRAM WEB APP INIT DATA
# ============================================================

def validate_init_data(
    init_data,
    max_age=86400
):

    if not init_data:
        return None

    try:

        parsed = parse_qs(
            init_data,
            keep_blank_values=True
        )

        received_hash = (
            parsed.get(
                "hash",
                [""]
            )[0]
        )

        auth_date = int(
            parsed.get(
                "auth_date",
                ["0"]
            )[0]
        )

        if not received_hash:
            return None

        # Проверяем срок.
        if (
            int(time.time()) -
            auth_date >
            max_age
        ):
            return None

        data_pairs = []

        for key in sorted(parsed.keys()):

            if key == "hash":
                continue

            value = parsed[key][0]

            data_pairs.append(
                f"{key}={value}"
            )

        data_check_string = "\n".join(
            data_pairs
        )

        secret_key = hmac.new(
            b"WebAppData",
            TOKEN.encode("utf-8"),
            hashlib.sha256
        ).digest()

        calculated_hash = hmac.new(
            secret_key,
            data_check_string.encode(
                "utf-8"
            ),
            hashlib.sha256
        ).hexdigest()

        if not hmac.compare_digest(
            calculated_hash,
            received_hash
        ):
            return None

        user_json = parsed.get(
            "user",
            [None]
        )[0]

        if not user_json:
            return None

        user = json.loads(
            user_json
        )

        return user

    except Exception as e:

        print(
            "initData validation error:",
            e
        )

        return None


# ============================================================
# HTTP SERVER
# ============================================================

class WebHandler(
    BaseHTTPRequestHandler
):

    def log_message(
        self,
        format,
        *args
    ):

        print(
            f"[HTTP] {self.address_string()} "
            f"- {format % args}"
        )

    def send_json(
        self,
        data,
        status=200
    ):

        body = json.dumps(
            data,
            ensure_ascii=False
        ).encode("utf-8")

        self.send_response(status)

        self.send_header(
            "Content-Type",
            "application/json; charset=utf-8"
        )

        self.send_header(
            "Cache-Control",
            "no-store"
        )

        self.send_header(
            "Access-Control-Allow-Origin",
            "*"
        )

        self.send_header(
            "Access-Control-Allow-Headers",
            "Content-Type, X-Telegram-Init-Data"
        )

        self.send_header(
            "Content-Length",
            str(len(body))
        )

        self.end_headers()

        self.wfile.write(body)

    def send_html(
        self,
        content,
        status=200
    ):

        body = content.encode(
            "utf-8"
        )

        self.send_response(status)

        self.send_header(
            "Content-Type",
            "text/html; charset=utf-8"
        )

        self.send_header(
            "Content-Length",
            str(len(body))
        )

        self.end_headers()

        self.wfile.write(body)

    def do_OPTIONS(self):

        self.send_response(204)

        self.send_header(
            "Access-Control-Allow-Origin",
            "*"
        )

        self.send_header(
            "Access-Control-Allow-Headers",
            "Content-Type, X-Telegram-Init-Data"
        )

        self.send_header(
            "Access-Control-Allow-Methods",
            "GET, POST, OPTIONS"
        )

        self.end_headers()

    def get_init_user(self):

        init_data = self.headers.get(
            "X-Telegram-Init-Data",
            ""
        )

        return validate_init_data(
            init_data
        )

    def do_GET(self):

        path = (
            urlparse(
                self.path
            ).path
        )

        query = parse_qs(
            urlparse(
                self.path
            ).query
        )

        # ----------------------------------------------------
        # HEALTH
        # ----------------------------------------------------

        if path == "/health":

            self.send_json(
                {
                    "status": "ok",
                    "app": "Farm Stars",
                    "index_found":
                        INDEX_FILE.exists(),
                    "db_found":
                        DB_FILE.exists()
                }
            )

            return

        # ----------------------------------------------------
        # BALANCE
        # ----------------------------------------------------

        if path == "/api/me/balance":

            user = self.get_init_user()

            if not user:

                self.send_json(
                    {
                        "ok": False,
                        "error":
                            "Telegram authorization required"
                    },
                    401
                )

                return

            create_user_if_needed(
                user
            )

            balance = get_balance(
                user["id"]
            )

            self.send_json(
                {
                    "ok": True,
                    "stars": balance
                }
            )

            return

        # ----------------------------------------------------
        # CREATE INVOICE
        # ----------------------------------------------------

        if path == "/api/payments/create-invoice":

            user = self.get_init_user()

            if not user:

                self.send_json(
                    {
                        "ok": False,
                        "error":
                            "Telegram authorization required"
                    },
                    401
                )

                return

            try:

                amount = int(
                    query.get(
                        "amount",
                        ["0"]
                    )[0]
                )

            except Exception:

                amount = 0

            if (
                amount <
                MIN_TOPUP or
                amount >
                MAX_TOPUP
            ):

                self.send_json(
                    {
                        "ok": False,
                        "error":
                            f"Сумма должна быть "
                            f"от {MIN_TOPUP} "
                            f"до {MAX_TOPUP} ⭐"
                    },
                    400
                )

                return

            create_user_if_needed(
                user
            )

            try:

                invoice_url = (
                    create_invoice_for_user(
                        user["id"],
                        amount
                    )
                )

                self.send_json(
                    {
                        "ok": True,
                        "url":
                            invoice_url,
                        "amount":
                            amount,
                        "farm_stars":
                            amount
                    }
                )

            except Exception as e:

                print(
                    "Invoice error:",
                    e
                )

                self.send_json(
                    {
                        "ok": False,
                        "error":
                            "Не удалось создать оплату"
                    },
                    500
                )

            return

        # ----------------------------------------------------
        # INDEX
        # ----------------------------------------------------

        if path in (
            "/",
            "/index.html"
        ):

            if not INDEX_FILE.exists():

                self.send_html(
                    """
                    <!doctype html>
                    <html>
                    <body style="
                        background:#111;
                        color:white;
                        font-family:Arial;
                        padding:30px
                    ">
                    <h2>ERROR: index.html not found</h2>
                    <p>BASE_DIR: %s</p>
                    <p>INDEX_FILE: %s</p>
                    </body>
                    </html>
                    """
                    % (
                        BASE_DIR,
                        INDEX_FILE
                    ),
                    500
                )

                return

            try:

                html = INDEX_FILE.read_text(
                    encoding="utf-8"
                )

                self.send_html(
                    html
                )

            except Exception as e:

                print(
                    "Index read error:",
                    e
                )

                self.send_html(
                    "ERROR: cannot read index.html",
                    500
                )

            return

        # ----------------------------------------------------
        # 404
        # ----------------------------------------------------

        self.send_json(
            {
                "ok": False,
                "error": "Not found"
            },
            404
        )


# ============================================================
# BOT COMMANDS
# ============================================================

def welcome_text():

    return (
        "🌟 <b>Farm Stars</b>\n\n"
        "Добро пожаловать!\n\n"
        "🎮 Здесь можно открыть игры "
        "прямо внутри Telegram.\n\n"
        "⭐ Farm Stars — игровая валюта.\n"
        "Telegram Stars пополняют её "
        "по курсу <b>1:1</b>.\n\n"
        "Выбери действие ниже:"
    )


def handle_start(
    message,
    start_param=""
):

    user = message.get(
        "from"
    )

    if not user:
        return

    referral_id = None

    if start_param.startswith(
        "ref_"
    ):

        referral_id = (
            start_param[4:]
        )

    create_user_if_needed(
        user,
        referral_id
    )

    send_message(
        message["chat"]["id"],
        welcome_text(),
        games_keyboard()
    )


def handle_games(
    message
):

    send_message(
        message["chat"]["id"],
        (
            "🎮 <b>Игры Farm Stars</b>\n\n"
            "Открой Mini App прямо "
            "в Telegram:"
        ),
        games_keyboard()
    )


def handle_topup(
    message,
    amount
):

    user = message.get(
        "from"
    )

    if not user:
        return

    create_user_if_needed(
        user
    )

    try:

        invoice_url = (
            create_invoice_for_user(
                user["id"],
                amount
            )
        )

        send_message(
            message["chat"]["id"],
            (
                "⭐ <b>Пополнение Farm Stars</b>\n\n"
                f"Сумма: <b>{amount} ⭐</b>\n"
                f"Получишь: <b>{amount} Farm Stars</b>\n\n"
                "Курс: <b>1:1</b>\n\n"
                "Нажми кнопку оплаты:"
            ),
            {
                "inline_keyboard": [
                    [
                        {
                            "text":
                                f"⭐ Оплатить {amount}",
                            "url":
                                invoice_url
                        }
                    ],
                    [
                        {
                            "text":
                                "🎮 Открыть игры",
                            "web_app": {
                                "url":
                                    WEB_APP_URL
                            }
                        }
                    ]
                ]
            }
        )

    except Exception as e:

        print(
            "Topup error:",
            e
        )

        send_message(
            message["chat"]["id"],
            "❌ Не удалось создать счёт."
        )


# ============================================================
# CALLBACKS
# ============================================================

def answer_callback(
    callback_id,
    text=None,
    show_alert=False
):

    data = {
        "callback_query_id":
            callback_id
    }

    if text:
        data["text"] = text

    if show_alert:
        data["show_alert"] = True

    try:

        tg(
            "answerCallbackQuery",
            data
        )

    except Exception as e:

        print(
            "Callback answer error:",
            e
        )


def handle_callback(
    callback
):

    callback_id = callback["id"]

    user = callback.get(
        "from"
    )

    data = callback.get(
        "data",
        ""
    )

    message = callback.get(
        "message"
    )

    if not user or not message:
        return

    chat_id = message["chat"]["id"]

    create_user_if_needed(
        user
    )

    if data == "topup_menu":

        answer_callback(
            callback_id
        )

        send_message(
            chat_id,
            (
                "⭐ <b>Пополнение Farm Stars</b>\n\n"
                "Выбери сумму.\n"
                "Курс: <b>1:1</b>"
            ),
            topup_keyboard()
        )

        return

    if data == "referral":

        answer_callback(
            callback_id
        )

        send_message(
            chat_id,
            referral_text(
                user["id"]
            )
        )

        return

    if data.startswith(
        "topup_"
    ):

        try:

            amount = int(
                data.split("_")[1]
            )

        except Exception:

            answer_callback(
                callback_id,
                "Ошибка суммы",
                True
            )

            return

        answer_callback(
            callback_id
        )

        handle_topup(
            {
                "chat": {
                    "id": chat_id
                },
                "from": user
            },
            amount
        )

        return


# ============================================================
# BOT UPDATE LOOP
# ============================================================

def handle_update(
    update
):

    try:

        # ----------------------------------------------------
        # PRE-CHECKOUT
        # ----------------------------------------------------

        if "pre_checkout_query" in update:

            answer_pre_checkout(
                update[
                    "pre_checkout_query"
                ]
            )

            return

        # ----------------------------------------------------
        # MESSAGE
        # ----------------------------------------------------

        message = update.get(
            "message"
        )

        if message:

            # Успешная оплата.
            if message.get(
                "successful_payment"
            ):

                process_successful_payment(
                    message
                )

                return

            text = (
                message.get(
                    "text",
                    ""
                )
                or ""
            ).strip()

            if text.startswith(
                "/start"
            ):

                parts = text.split(
                    maxsplit=1
                )

                start_param = ""

                if len(parts) == 2:

                    start_param = (
                        parts[1].strip()
                    )

                handle_start(
                    message,
                    start_param
                )

                return

            if text in (
                "/games",
                "/play",
                "игры"
            ):

                handle_games(
                    message
                )

                return

            if text in (
                "/topup",
                "/stars",
                "пополнить"
            ):

                send_message(
                    message["chat"]["id"],
                    (
                        "⭐ <b>Пополнение</b>\n\n"
                        "Выбери сумму:"
                    ),
                    topup_keyboard()
                )

                return

            if text in (
                "/ref",
                "/referral",
                "реферал"
            ):

                user = message.get(
                    "from"
                )

                if user:

                    create_user_if_needed(
                        user
                    )

                    send_message(
                        message["chat"]["id"],
                        referral_text(
                            user["id"]
                        )
                    )

                return

            # Если пользователь написал что-то обычное.
            if message.get(
                "chat",
                {}
            ).get(
                "type"
            ) == "private":

                send_message(
                    message["chat"]["id"],
                    (
                        "🎮 Нажми «Игры», "
                        "чтобы открыть Farm Stars "
                        "прямо в Telegram."
                    ),
                    games_keyboard()
                )

                return

        # ----------------------------------------------------
        # CALLBACK QUERY
        # ----------------------------------------------------

        if "callback_query" in update:

            handle_callback(
                update[
                    "callback_query"
                ]
            )

            return

    except Exception as e:

        print(
            "UPDATE ERROR:",
            repr(e)
        )


def bot_loop():

    print(
        "Bot polling started"
    )

    print(
        "WEB_APP_URL:",
        WEB_APP_URL
    )

    offset = 0

    while True:

        try:

            updates = tg(
                "getUpdates",
                {
                    "offset": offset,
                    "timeout": 25,

                    "allowed_updates": [
                        "message",
                        "callback_query",
                        "pre_checkout_query"
                    ]
                }
            )

            for update in updates:

                offset = (
                    update["update_id"] + 1
                )

                handle_update(
                    update
                )

        except Exception as e:

            print(
                "Polling error:",
                repr(e)
            )

            time.sleep(3)


# ============================================================
# BOT SETUP
# ============================================================

def setup_bot():

    print(
        "Setting Telegram bot menu..."
    )

    # Кнопка меню рядом с полем ввода.
    tg(
        "setChatMenuButton",
        {
            "menu_button": {
                "type": "web_app",
                "text": "🎮 Игры",
                "web_app": {
                    "url": WEB_APP_URL
                }
            }
        }
    )

    # Команды бота.
    commands = [

        {
            "command": "start",
            "description":
                "🌟 Запустить Farm Stars"
        },

        {
            "command": "games",
            "description":
                "🎮 Открыть игры"
        },

        {
            "command": "topup",
            "description":
                "⭐ Пополнить Farm Stars"
        },

        {
            "command": "ref",
            "description":
                "👥 Пригласить друзей"
        }

    ]

    tg(
        "setMyCommands",
        {
            "commands": commands
        }
    )

    print(
        "Telegram bot configured"
    )


# ============================================================
# START HTTP
# ============================================================

def start_http():

    server = ThreadingHTTPServer(
        ("0.0.0.0", PORT),
        WebHandler
    )

    print(
        f"HTTP server listening on 0.0.0.0:{PORT}"
    )

    print(
        "BASE_DIR:",
        BASE_DIR
    )

    print(
        "INDEX_FILE:",
        INDEX_FILE
    )

    print(
        "INDEX EXISTS:",
        INDEX_FILE.exists()
    )

    print(
        "DB_FILE:",
        DB_FILE
    )

    server.serve_forever()


# ============================================================
# MAIN
# ============================================================

def main():

    print(
        "===================================="
    )

    print(
        "       FARM STARS BOT"
    )

    print(
        "===================================="
    )

    print(
        "BASE_DIR:",
        BASE_DIR
    )

    print(
        "WEB_APP_URL:",
        WEB_APP_URL
    )

    print(
        "INDEX_FILE:",
        INDEX_FILE
    )

    print(
        "INDEX EXISTS:",
        INDEX_FILE.exists()
    )

    print(
        "DB_FILE:",
        DB_FILE
    )

    init_db()

    # Проверяем Telegram token.
    me = tg(
        "getMe"
    )

    print(
        "BOT:",
        me.get("username"),
        me.get("id")
    )

    setup_bot()

    http_thread = threading.Thread(
        target=start_http,
        daemon=True
    )

    http_thread.start()

    bot_loop()


if __name__ == "__main__":
    main()
