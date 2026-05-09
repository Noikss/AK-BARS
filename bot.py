#!/usr/bin/env python3
"""
Telegram-бот: мониторинг билетов ХК Ак Барс
Хостинг: bothost.ru
БД: SQLite в /app/data/bot.db
"""

import asyncio
import logging
import os
import sqlite3
from pathlib import Path
from datetime import datetime

import httpx
from telegram import Update, ReplyKeyboardMarkup, ReplyKeyboardRemove
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ConversationHandler,
    ContextTypes,
    filters,
)

# ── Настройки ──────────────────────────────────
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
CHECK_INTERVAL     = int(os.getenv("CHECK_INTERVAL", "120"))   # секунды
TICKETS_URL        = "https://www.ak-bars.ru/tickets"

# API-эндпоинты сайта (React SPA использует их под капотом)
API_BASE    = "https://api.ak-bars.ru"          # основной вариант
API_LOGIN   = f"{API_BASE}/auth/login"
API_TICKETS = f"{API_BASE}/tickets"

# Запасные эндпоинты на случай другой структуры
ALT_API_BASE    = "https://irbis.ak-bars.ru/api"
ALT_API_LOGIN   = f"{ALT_API_BASE}/auth/login"
ALT_API_TICKETS = f"{ALT_API_BASE}/tickets"
# ───────────────────────────────────────────────

# ── База данных ─────────────────────────────────
DATA_DIR = Path("/app/data")
DATA_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH  = DATA_DIR / "bot.db"

def db_init():
    """Создаём таблицы при первом запуске."""
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                chat_id     INTEGER PRIMARY KEY,
                phone       TEXT NOT NULL,
                password    TEXT NOT NULL,
                active      INTEGER DEFAULT 1,
                created_at  TEXT DEFAULT (datetime('now'))
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS seen_tickets (
                chat_id   INTEGER,
                ticket_id TEXT,
                seen_at   TEXT DEFAULT (datetime('now')),
                PRIMARY KEY (chat_id, ticket_id)
            )
        """)
        conn.commit()

def db_save_user(chat_id: int, phone: str, password: str):
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("""
            INSERT INTO users (chat_id, phone, password, active)
            VALUES (?, ?, ?, 1)
            ON CONFLICT(chat_id) DO UPDATE SET
                phone=excluded.phone,
                password=excluded.password,
                active=1
        """, (chat_id, phone, password))
        conn.commit()

def db_set_active(chat_id: int, active: bool):
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("UPDATE users SET active=? WHERE chat_id=?",
                     (1 if active else 0, chat_id))
        conn.commit()

def db_get_all_active():
    with sqlite3.connect(DB_PATH) as conn:
        rows = conn.execute(
            "SELECT chat_id, phone, password FROM users WHERE active=1"
        ).fetchall()
    return rows

def db_seen_ticket(chat_id: int, ticket_id: str) -> bool:
    with sqlite3.connect(DB_PATH) as conn:
        row = conn.execute(
            "SELECT 1 FROM seen_tickets WHERE chat_id=? AND ticket_id=?",
            (chat_id, ticket_id)
        ).fetchone()
    return row is not None

def db_mark_seen(chat_id: int, ticket_id: str):
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            "INSERT OR IGNORE INTO seen_tickets (chat_id, ticket_id) VALUES (?,?)",
            (chat_id, ticket_id)
        )
        conn.commit()
# ───────────────────────────────────────────────

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(message)s",
    level=logging.INFO,
)
log = logging.getLogger(__name__)

PHONE, PASSWORD = range(2)

# Активные задачи мониторинга { chat_id: asyncio.Task }
tasks: dict[int, asyncio.Task] = {}


# ── HTTP: авторизация и проверка билетов ────────

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
    "Content-Type": "application/json",
    "Referer": "https://www.ak-bars.ru/",
    "Origin":  "https://www.ak-bars.ru",
}


async def try_login(client: httpx.AsyncClient, phone: str, password: str) -> str | None:
    """
    Пробуем несколько вариантов API авторизации.
    Возвращает токен (Bearer) или None если не получилось.
    """
    payloads = [
        {"phone": phone, "password": password},
        {"login": phone, "password": password},
        {"username": phone, "password": password},
        {"email": phone, "password": password},
    ]
    endpoints = [API_LOGIN, ALT_API_LOGIN,
                 "https://www.ak-bars.ru/api/auth/login",
                 "https://www.ak-bars.ru/api/v1/auth/login"]

    for url in endpoints:
        for payload in payloads:
            try:
                r = await client.post(url, json=payload, timeout=15)
                log.info(f"Login attempt {url} → {r.status_code}")
                if r.status_code == 200:
                    data = r.json()
                    # ищем токен в разных полях
                    token = (
                        data.get("token") or
                        data.get("access_token") or
                        data.get("accessToken") or
                        (data.get("data") or {}).get("token") or
                        (data.get("data") or {}).get("access_token")
                    )
                    if token:
                        log.info(f"Login OK via {url}")
                        return str(token)
            except Exception as e:
                log.debug(f"Login error {url}: {e}")

    return None


async def fetch_tickets(client: httpx.AsyncClient) -> list[dict]:
    """
    Запрашиваем список билетов/матчей.
    Возвращает список событий (словарей).
    """
    endpoints = [
        API_TICKETS,
        f"{API_BASE}/matches",
        f"{API_BASE}/events",
        f"{API_BASE}/schedule",
        f"{ALT_API_BASE}/tickets",
        f"{ALT_API_BASE}/matches",
        "https://www.ak-bars.ru/api/tickets",
        "https://www.ak-bars.ru/api/v1/matches",
        "https://www.ak-bars.ru/api/v1/tickets",
    ]

    for url in endpoints:
        try:
            r = await client.get(url, timeout=15)
            if r.status_code == 200:
                data = r.json()
                # данные могут быть массивом или объектом с items/data/results
                if isinstance(data, list):
                    return data
                for key in ("data", "items", "results", "tickets", "matches", "events"):
                    if isinstance(data.get(key), list):
                        return data[key]
        except Exception as e:
            log.debug(f"Tickets error {url}: {e}")

    return []


def ticket_id(ticket: dict) -> str:
    """Уникальный идентификатор билета/матча."""
    return str(
        ticket.get("id") or
        ticket.get("match_id") or
        ticket.get("event_id") or
        ticket.get("uuid") or
        str(ticket)[:100]
    )

def ticket_label(ticket: dict) -> str:
    """Человекочитаемое описание матча."""
    opponent = (
        ticket.get("opponent") or
        ticket.get("away_team") or
        ticket.get("title") or
        ticket.get("name") or
        "—"
    )
    date = (
        ticket.get("date") or
        ticket.get("match_date") or
        ticket.get("event_date") or
        ticket.get("start_at") or
        ""
    )
    price = ticket.get("price") or ticket.get("min_price") or ""
    status = ticket.get("status") or ticket.get("ticket_status") or ""
    parts = [f"🏒 {opponent}"]
    if date:
        parts.append(f"📅 {date}")
    if price:
        parts.append(f"💰 от {price} ₽")
    if status:
        parts.append(f"Статус: {status}")
    return " | ".join(parts)

def ticket_available(ticket: dict) -> bool:
    """Проверяем, доступен ли билет для покупки."""
    status = str(ticket.get("status") or ticket.get("ticket_status") or "").lower()
    available = ticket.get("available") or ticket.get("tickets_available")
    count = ticket.get("tickets_count") or ticket.get("available_count") or 0

    # Если явно недоступно
    bad = {"sold_out", "unavailable", "closed", "soldout", "распродано"}
    if status in bad:
        return False
    if available is False:
        return False
    if count == 0 and count is not None and isinstance(count, int):
        return False

    # Если явно доступно
    good = {"available", "open", "on_sale", "sale", "доступно"}
    if status in good:
        return True
    if available is True:
        return True
    if isinstance(count, int) and count > 0:
        return True

    # Если нет явного признака — считаем что билет есть (вернём его)
    return True


# ── Мониторинг ──────────────────────────────────

async def monitor(chat_id: int, phone: str, password: str, app: Application):
    """Основной цикл мониторинга для одного пользователя."""
    log.info(f"[{chat_id}] Мониторинг запущен для {phone}")

    async with httpx.AsyncClient(headers=HEADERS, follow_redirects=True) as client:
        # Авторизация
        await app.bot.send_message(chat_id, "🔐 Авторизуюсь на сайте...")
        token = await try_login(client, phone, password)

        if not token:
            # Пробуем авторизацию без явного токена — возможно через cookie-сессию
            log.warning(f"[{chat_id}] Токен не получен, пробую сессионный вход")
            # Ряд сайтов устанавливают сессионную cookie при POST-запросе
            # Продолжаем с теми cookies что есть в client

        if token:
            client.headers.update({"Authorization": f"Bearer {token}"})
            await app.bot.send_message(chat_id, "✅ Авторизация успешна! Начинаю мониторинг каждые 2 минуты...")
        else:
            await app.bot.send_message(
                chat_id,
                "⚠️ Не удалось авторизоваться через API.\n"
                "Продолжаю мониторинг как гость — часть матчей может быть видна.\n"
                "Если нужна полная авторизация, напиши /stop и /start снова, "
                "проверь правильность номера и пароля."
            )

        check_count = 0

        while True:
            try:
                tickets = await fetch_tickets(client)
                check_count += 1
                now = datetime.now().strftime("%H:%M")

                available = [t for t in tickets if ticket_available(t)]
                new_ones  = [t for t in available if not db_seen_ticket(chat_id, ticket_id(t))]

                if new_ones:
                    for t in new_ones:
                        db_mark_seen(chat_id, ticket_id(t))

                    lines = "\n".join(ticket_label(t) for t in new_ones[:10])
                    msg = (
                        f"🚨 *БИЛЕТЫ ПОЯВИЛИСЬ!*\n\n"
                        f"{lines}\n\n"
                        f"👉 [Купить на сайте]({TICKETS_URL})\n"
                        f"🕐 {now}"
                    )
                    await app.bot.send_message(chat_id, msg, parse_mode="Markdown",
                                               disable_web_page_preview=True)
                    log.info(f"[{chat_id}] Отправлено уведомление: {len(new_ones)} матчей")
                else:
                    log.info(f"[{chat_id}] Проверка #{check_count} ({now}) — новых билетов нет"
                             f" (всего на сайте: {len(tickets)}, доступных: {len(available)})")

                # Каждые 40 мин (20 проверок) — отчёт что бот жив
                if check_count % 20 == 0:
                    await app.bot.send_message(
                        chat_id,
                        f"🔄 Бот работает. Проверок: {check_count}\n"
                        f"На сайте матчей: {len(tickets)}, с билетами: {len(available)}\n"
                        f"🕐 {now}"
                    )

            except asyncio.CancelledError:
                log.info(f"[{chat_id}] Мониторинг остановлен")
                return
            except Exception as e:
                log.error(f"[{chat_id}] Ошибка при проверке: {e}")

            await asyncio.sleep(CHECK_INTERVAL)


# ── Telegram handlers ───────────────────────────

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    chat_id = update.effective_chat.id

    # Останавливаем старый мониторинг
    if chat_id in tasks and not tasks[chat_id].done():
        tasks[chat_id].cancel()
        db_set_active(chat_id, False)

    await update.message.reply_text(
        "🏒 *Мониторинг билетов ХК Ак Барс*\n\n"
        "Введи номер телефона, привязанный к аккаунту на ak-bars.ru\n"
        "Например: `+79161234567`",
        parse_mode="Markdown",
        reply_markup=ReplyKeyboardRemove(),
    )
    return PHONE


async def got_phone(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    ctx.user_data["phone"] = update.message.text.strip()
    await update.message.reply_text(
        "🔑 Теперь введи пароль от сайта ak-bars.ru\n"
        "_(сообщение сразу удалится из чата)_",
        parse_mode="Markdown",
    )
    return PASSWORD


async def got_password(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    chat_id = update.effective_chat.id
    phone   = ctx.user_data["phone"]
    password = update.message.text.strip()

    # Удаляем пароль из чата
    try:
        await update.message.delete()
    except Exception:
        pass

    # Сохраняем в БД
    db_save_user(chat_id, phone, password)

    await update.message.reply_text(
        "⏳ Запускаю мониторинг...",
        reply_markup=ReplyKeyboardMarkup([["/stop", "/status"]], resize_keyboard=True),
    )

    # Запускаем мониторинг в фоне
    task = asyncio.create_task(monitor(chat_id, phone, password, ctx.application))
    tasks[chat_id] = task

    return ConversationHandler.END


async def cmd_stop(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if chat_id in tasks and not tasks[chat_id].done():
        tasks[chat_id].cancel()
        db_set_active(chat_id, False)
        await update.message.reply_text("🛑 Мониторинг остановлен.", reply_markup=ReplyKeyboardRemove())
    else:
        await update.message.reply_text("Мониторинг не запущен. Нажми /start")


async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if chat_id in tasks and not tasks[chat_id].done():
        await update.message.reply_text("✅ Мониторинг активен. Проверяю каждые 2 минуты.")
    else:
        await update.message.reply_text("❌ Мониторинг не запущен. Нажми /start")


async def cmd_cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text("Отменено. Нажми /start чтобы начать заново.")
    return ConversationHandler.END


async def on_startup(app: Application):
    """При старте бота восстанавливаем мониторинг для всех активных пользователей из БД."""
    rows = db_get_all_active()
    log.info(f"Восстанавливаю мониторинг для {len(rows)} пользователей из БД")
    for chat_id, phone, password in rows:
        task = asyncio.create_task(monitor(chat_id, phone, password, app))
        tasks[chat_id] = task
        try:
            await app.bot.send_message(
                chat_id,
                "🔄 Бот перезапустился и возобновил мониторинг автоматически."
            )
        except Exception:
            pass


# ── Main ────────────────────────────────────────

def main():
    db_init()

    token = TELEGRAM_BOT_TOKEN
    if not token:
        print("❌ Задай переменную окружения TELEGRAM_BOT_TOKEN!")
        return

    app = Application.builder().token(token).post_init(on_startup).build()

    conv = ConversationHandler(
        entry_points=[CommandHandler("start", cmd_start)],
        states={
            PHONE:    [MessageHandler(filters.TEXT & ~filters.COMMAND, got_phone)],
            PASSWORD: [MessageHandler(filters.TEXT & ~filters.COMMAND, got_password)],
        },
        fallbacks=[CommandHandler("cancel", cmd_cancel)],
    )

    app.add_handler(conv)
    app.add_handler(CommandHandler("stop", cmd_stop))
    app.add_handler(CommandHandler("status", cmd_status))

    log.info("🤖 Бот запущен")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
