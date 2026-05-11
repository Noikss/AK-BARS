#!/usr/bin/env python3
"""
Telegram-бот: мониторинг билетов ХК Ак Барс
API: api.ak-bars.ru/portal/
Авторизация: cookie-сессия после POST /portal/auth/login
"""

import asyncio
import logging
import os
import sqlite3
from pathlib import Path
from datetime import datetime

import httpx
import httpx_socks  # pip install httpx-socks
from telegram import Update, ReplyKeyboardMarkup, ReplyKeyboardRemove
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    ConversationHandler, ContextTypes, filters,
)

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "8593827143:AAFgSm-Y5cKU1LYbQv6Bc9WeA2EauVbPsZM")
CHECK_INTERVAL     = int(os.getenv("CHECK_INTERVAL", "120"))
TICKETS_URL        = "https://www.ak-bars.ru/tickets"

API_BASE    = "https://api.ak-bars.ru/portal"
API_LOGIN   = f"{API_BASE}/auth/login"
API_USER    = f"{API_BASE}/auth/user"
API_TICKETS = f"{API_BASE}/tickets"
API_MATCHES = f"{API_BASE}/matches"
API_EVENTS  = f"{API_BASE}/events"
API_SCHEDULE= f"{API_BASE}/schedule"

DATA_DIR = Path("/app/data")
DATA_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH  = DATA_DIR / "bot.db"

def db_init():
    with sqlite3.connect(DB_PATH) as c:
        c.execute("""CREATE TABLE IF NOT EXISTS users (
            chat_id INTEGER PRIMARY KEY, phone TEXT, password TEXT,
            active INTEGER DEFAULT 1)""")
        c.execute("""CREATE TABLE IF NOT EXISTS seen_tickets (
            chat_id INTEGER, ticket_id TEXT,
            PRIMARY KEY (chat_id, ticket_id))""")
        c.commit()

def db_save(chat_id, phone, pwd):
    with sqlite3.connect(DB_PATH) as c:
        c.execute("""INSERT INTO users (chat_id,phone,password,active) VALUES(?,?,?,1)
            ON CONFLICT(chat_id) DO UPDATE SET phone=excluded.phone,
            password=excluded.password, active=1""", (chat_id, phone, pwd))
        c.commit()

def db_set_active(chat_id, val):
    with sqlite3.connect(DB_PATH) as c:
        c.execute("UPDATE users SET active=? WHERE chat_id=?", (val, chat_id))
        c.commit()

def db_active_users():
    with sqlite3.connect(DB_PATH) as c:
        return c.execute("SELECT chat_id,phone,password FROM users WHERE active=1").fetchall()

def db_seen(chat_id, tid):
    with sqlite3.connect(DB_PATH) as c:
        return c.execute("SELECT 1 FROM seen_tickets WHERE chat_id=? AND ticket_id=?",
                         (chat_id, tid)).fetchone() is not None

def db_mark(chat_id, tid):
    with sqlite3.connect(DB_PATH) as c:
        c.execute("INSERT OR IGNORE INTO seen_tickets VALUES(?,?)", (chat_id, tid))
        c.commit()

logging.basicConfig(format="%(asctime)s | %(levelname)s | %(message)s", level=logging.INFO)
log = logging.getLogger(__name__)

PHONE, PASSWORD = range(2)
tasks: dict[int, asyncio.Task] = {}

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Content-Type": "application/json",
    "Origin": "https://www.ak-bars.ru",
    "Referer": "https://www.ak-bars.ru/",
}


async def do_login(client: httpx.AsyncClient, phone: str, password: str) -> bool:
    """
    Авторизация. Сервер возвращает {dId, firstName, lastName} — токен в cookie.
    После логина делаем GET /auth/user чтобы получить Bearer токен.
    """
    try:
        r = await client.post(API_LOGIN, json={"login": phone, "password": password}, timeout=15)
        log.info(f"Login → {r.status_code} | body: {r.text[:200]}")
        log.info(f"Login cookies: {dict(client.cookies)}")
        log.info(f"Login response headers: {dict(r.headers)}")

        if r.status_code != 200:
            return False

        # Токен приходит в заголовке ak-token
        ak_token = r.headers.get("ak-token") or r.headers.get("AK-Token")
        if ak_token:
            client.headers.update({"Authorization": f"Bearer {ak_token}"})
            log.info(f"Токен получен из заголовка ak-token!")
            return True

        return True

    except Exception as e:
        log.error(f"Login exception: {type(e).__name__}: {e}")
        return False


async def get_tickets(client: httpx.AsyncClient) -> list[dict]:
    endpoints = [
        API_TICKETS, API_MATCHES, API_EVENTS, API_SCHEDULE,
        "https://api.ak-bars.ru/portal/games",
        "https://api.ak-bars.ru/portal/game",
        "https://api.ak-bars.ru/portal/home-games",
        "https://api.ak-bars.ru/portal/homeGames",
        "https://api.ak-bars.ru/portal/sale",
        "https://api.ak-bars.ru/portal/orders",
        "https://irbis.ak-bars.ru/api/matches",
    ]
    for url in endpoints:
        try:
            r = await client.get(url, timeout=15)
            log.info(f"Fetch {url} → {r.status_code}")
            if r.status_code != 200:
                continue
            data = r.json()
            log.info(f"Data from {url}: {str(data)[:400]}")
            if isinstance(data, list) and data:
                return data
            if isinstance(data, dict):
                for key in ("data","items","results","matches","tickets","events","schedule"):
                    val = data.get(key)
                    if isinstance(val, list) and val:
                        return val
        except Exception as e:
            log.error(f"Fetch error {url}: {type(e).__name__}: {e}")
    return []


def match_id(m):
    return str(m.get("booking_id") or m.get("id") or m.get("uuid") or str(m)[:80])

def match_label(m):
    # tna-tickets.ru формат
    home  = m.get("home_team") or m.get("home") or "Ак Барс"
    away  = m.get("away_team") or m.get("away") or m.get("guest") or m.get("opponent") or m.get("title") or m.get("name") or "—"
    date  = m.get("date") or m.get("match_date") or m.get("start_at") or m.get("startAt") or m.get("game_date") or ""
    price = m.get("price") or m.get("min_price") or m.get("minPrice") or m.get("min_cost") or ""
    booking_id = m.get("booking_id") or m.get("id") or ""

    sectors = m.get("_sectors", [])
    free_sectors = [s for s in sectors if (s.get("available", 0) or s.get("free", 0) or s.get("count", 0)) > 0]

    parts = [f"🏒 {home} — {away}"]
    if date:  parts.append(f"📅 {date}")
    if price: parts.append(f"💰 от {price} ₽")
    if free_sectors:
        parts.append(f"✅ Свободных секторов: {len(free_sectors)}")
    if booking_id:
        parts.append(f"🔗 ak-bars.ru/tickets → матч {booking_id}")
    return "\n".join(parts)

def is_available(m):
    # Матч считается доступным если есть хоть один свободный сектор
    return m.get("_has_tickets", False) or bool(m.get("_sectors"))


async def monitor(chat_id: int, phone: str, password: str, app: Application):
    log.info(f"[{chat_id}] Мониторинг запущен")

    proxy = "socks5://okurali02g:ZCUqsM7kgx@45.153.163.149:50101"
    transport = httpx_socks.AsyncProxyTransport.from_url(proxy)
    async with httpx.AsyncClient(headers=HEADERS, follow_redirects=True, transport=transport) as client:
        await app.bot.send_message(chat_id, "🔐 Авторизуюсь на сайте...")
        ok = await do_login(client, phone, password)

        if ok:
            await app.bot.send_message(chat_id, "✅ Авторизация прошла! Мониторинг запущен — проверяю каждые 2 минуты.")
        else:
            await app.bot.send_message(chat_id, "⚠️ Ошибка авторизации. Проверь логин/пароль и нажми /start.")
            return

        check_count = 0
        while True:
            try:
                matches = await get_tickets(client)
                check_count += 1
                now = datetime.now().strftime("%H:%M")

                available = [m for m in matches if is_available(m)]
                new_ones  = [m for m in available if not db_seen(chat_id, match_id(m))]

                if new_ones:
                    for m in new_ones:
                        db_mark(chat_id, match_id(m))
                    lines = "\n".join(match_label(m) for m in new_ones[:10])
                    await app.bot.send_message(
                        chat_id,
                        f"🚨 *БИЛЕТЫ ПОЯВИЛИСЬ!*\n\n{lines}\n\n"
                        f"👉 [Купить]({TICKETS_URL})\n🕐 {now}",
                        parse_mode="Markdown", disable_web_page_preview=True)
                else:
                    log.info(f"[{chat_id}] #{check_count} {now} матчей={len(matches)} доступных={len(available)} новых=0")

                if check_count % 60 == 0:
                    await app.bot.send_message(chat_id,
                        f"🔄 Бот работает. Проверок: {check_count}\n"
                        f"Матчей: {len(matches)}, с билетами: {len(available)}")

            except asyncio.CancelledError:
                return
            except Exception as e:
                log.error(f"[{chat_id}] Ошибка: {type(e).__name__}: {e}")

            await asyncio.sleep(CHECK_INTERVAL)


async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    chat_id = update.effective_chat.id
    if chat_id in tasks and not tasks[chat_id].done():
        tasks[chat_id].cancel()
        db_set_active(chat_id, 0)
    await update.message.reply_text(
        "🏒 *Мониторинг билетов ХК Ак Барс*\n\n"
        "Введи номер телефона от аккаунта на ak-bars.ru\n"
        "Пример: `79161234567`",
        parse_mode="Markdown", reply_markup=ReplyKeyboardRemove())
    return PHONE

async def got_phone(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    ctx.user_data["phone"] = update.message.text.strip()
    await update.message.reply_text("🔑 Введи пароль _(удалится сразу)_", parse_mode="Markdown")
    return PASSWORD

async def got_password(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    chat_id  = update.effective_chat.id
    phone    = ctx.user_data["phone"]
    password = update.message.text.strip()
    try: await update.message.delete()
    except Exception: pass
    db_save(chat_id, phone, password)
    await update.message.reply_text("⏳ Запускаю...",
        reply_markup=ReplyKeyboardMarkup([["/stop", "/status"]], resize_keyboard=True))
    tasks[chat_id] = asyncio.create_task(monitor(chat_id, phone, password, ctx.application))
    return ConversationHandler.END

async def cmd_test(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Показывает как будет выглядеть уведомление о билетах."""
    chat_id = update.effective_chat.id
    fake = [
        {"id": 1, "name": "Ак Барс — Металлург", "date": "2026-09-10 19:00", "min_price": 500, "available": True},
        {"id": 2, "name": "Ак Барс — ЦСКА", "date": "2026-09-15 19:00", "min_price": 800, "available": True},
    ]
    lines = "\n".join(match_label(m) for m in fake)
    await update.message.reply_text(
        f"🚨 *БИЛЕТЫ ПОЯВИЛИСЬ!*\n\n{lines}\n\n"
        f"👉 [Купить](https://www.ak-bars.ru/tickets)\n\n"
        f"_(это тестовое сообщение)_",
        parse_mode="Markdown", disable_web_page_preview=True)

async def cmd_stop(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if chat_id in tasks and not tasks[chat_id].done():
        tasks[chat_id].cancel()
        db_set_active(chat_id, 0)
        await update.message.reply_text("🛑 Остановлен.", reply_markup=ReplyKeyboardRemove())
    else:
        await update.message.reply_text("Не запущен. /start")

async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if chat_id in tasks and not tasks[chat_id].done():
        await update.message.reply_text("✅ Мониторинг активен.")
    else:
        await update.message.reply_text("❌ Не запущен. /start")

async def cmd_cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text("Отменено.")
    return ConversationHandler.END

async def on_startup(app: Application):
    for chat_id, phone, password in db_active_users():
        tasks[chat_id] = asyncio.create_task(monitor(chat_id, phone, password, app))
        try: await app.bot.send_message(chat_id, "🔄 Бот перезапустился, мониторинг возобновлён.")
        except Exception: pass


def main():
    db_init()
    if not TELEGRAM_BOT_TOKEN:
        print("❌ Задай TELEGRAM_BOT_TOKEN!")
        return
    app = Application.builder().token(TELEGRAM_BOT_TOKEN).post_init(on_startup).build()
    conv = ConversationHandler(
        entry_points=[CommandHandler("start", cmd_start)],
        states={
            PHONE:    [MessageHandler(filters.TEXT & ~filters.COMMAND, got_phone)],
            PASSWORD: [MessageHandler(filters.TEXT & ~filters.COMMAND, got_password)],
        },
        fallbacks=[CommandHandler("cancel", cmd_cancel)],
    )
    app.add_handler(conv)
    app.add_handler(CommandHandler("test", cmd_test))
    app.add_handler(CommandHandler("stop", cmd_stop))
    app.add_handler(CommandHandler("status", cmd_status))
    log.info("🤖 Бот запущен")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
