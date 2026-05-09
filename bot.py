#!/usr/bin/env python3
"""
Telegram-бот: мониторинг билетов ХК Ак Барс
Хостинг: bothost.ru  |  БД: /app/data/bot.db
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
    Application, CommandHandler, MessageHandler,
    ConversationHandler, ContextTypes, filters,
)

# ── Настройки ─────────────────────────────────
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "8593827143:AAFgSm-Y5cKU1LYbQv6Bc9WeA2EauVbPsZM")
CHECK_INTERVAL     = int(os.getenv("CHECK_INTERVAL", "120"))
TICKETS_URL        = "https://www.ak-bars.ru/tickets"
IRBIS_API          = "https://irbis.ak-bars.ru/api"   # рабочий по логам!
# ──────────────────────────────────────────────

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
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "application/json",
    "Content-Type": "application/json",
    "Referer": "https://www.ak-bars.ru/",
    "Origin": "https://www.ak-bars.ru",
}


def find_token(obj, depth=0):
    """Рекурсивно ищет токен в JSON любой вложенности."""
    if depth > 5:
        return None
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k.lower() in {"token","access_token","accesstoken","jwt","bearer","auth_token"} \
               and isinstance(v, str) and len(v) > 15:
                return v
            r = find_token(v, depth+1)
            if r: return r
    elif isinstance(obj, list):
        for item in obj:
            r = find_token(item, depth+1)
            if r: return r
    return None


async def do_login(client: httpx.AsyncClient, phone: str, password: str) -> str | None:
    endpoints = [
        f"{IRBIS_API}/auth/login",          # этот давал 200 по логам
        "https://www.ak-bars.ru/api/auth/login",
        "https://www.ak-bars.ru/api/v1/auth/login",
    ]
    payloads = [
        {"phone": phone, "password": password},
        {"login": phone, "password": password},
        {"username": phone, "password": password},
    ]
    for url in endpoints:
        for payload in payloads:
            try:
                r = await client.post(url, json=payload, timeout=15)
                log.info(f"Login {url} payload_key={list(payload.keys())[0]} → {r.status_code}")
                if r.status_code == 200:
                    try:
                        data = r.json()
                    except Exception:
                        log.warning(f"Не JSON: {r.text[:200]}")
                        continue
                    # Показываем весь ответ — так поймём структуру
                    log.info(f"LOGIN RESPONSE: {str(data)[:800]}")
                    token = find_token(data)
                    if token:
                        log.info(f"Токен найден: {token[:20]}...")
                        return token
                    # Если 200 но нет токена — сессия через cookie
                    if client.cookies:
                        log.info(f"Cookie сессия: {dict(client.cookies)}")
                        return "cookie"
                    log.warning(f"200 OK но токен не найден, ключи: {list(data.keys()) if isinstance(data, dict) else type(data)}")
            except Exception as e:
                log.debug(f"Login error {url}: {e}")
    return None


async def get_matches(client: httpx.AsyncClient) -> list[dict]:
    endpoints = [
        f"{IRBIS_API}/matches",             # давал 200 по логам!
        f"{IRBIS_API}/tickets",
        f"{IRBIS_API}/schedule",
        "https://www.ak-bars.ru/api/matches",
        "https://www.ak-bars.ru/api/tickets",
    ]
    for url in endpoints:
        try:
            r = await client.get(url, timeout=15)
            log.info(f"Fetch {url} → {r.status_code}")
            if r.status_code != 200:
                continue
            try:
                data = r.json()
            except Exception:
                continue
            # Первый успешный ответ — логируем полностью для отладки
            log.info(f"MATCHES RESPONSE: {str(data)[:1000]}")
            if isinstance(data, list) and data:
                return data
            if isinstance(data, dict):
                for key in ("data","items","results","matches","tickets","events","schedule"):
                    val = data.get(key)
                    if isinstance(val, list) and val:
                        return val
        except Exception as e:
            log.debug(f"Fetch error {url}: {e}")
    return []


def match_id(m):
    return str(m.get("id") or m.get("match_id") or m.get("uuid") or str(m)[:80])

def match_label(m):
    opp = m.get("opponent") or m.get("away_team") or m.get("title") or m.get("name") or "—"
    date = m.get("date") or m.get("match_date") or m.get("start_at") or ""
    price = m.get("price") or m.get("min_price") or m.get("minPrice") or ""
    status = m.get("status") or m.get("ticketStatus") or ""
    parts = [f"🏒 {opp}"]
    if date: parts.append(f"📅 {date}")
    if price: parts.append(f"💰 от {price} ₽")
    if status: parts.append(f"[{status}]")
    return " | ".join(parts)

def match_available(m):
    status = str(m.get("status") or m.get("ticketStatus") or "").lower()
    avail  = m.get("available") or m.get("ticketsAvailable")
    count  = m.get("tickets_count") or m.get("availableCount") or 0
    if status in {"sold_out","unavailable","closed","cancelled","распродано"}: return False
    if avail is False: return False
    if isinstance(count, int) and count == 0: return False
    if status in {"available","open","on_sale","sale","active","доступно"}: return True
    if avail is True: return True
    if isinstance(count, int) and count > 0: return True
    return True  # нет явного признака — считаем что есть


# ── Мониторинг ─────────────────────────────────

async def monitor(chat_id: int, phone: str, password: str, app: Application):
    log.info(f"[{chat_id}] Мониторинг запущен")
    async with httpx.AsyncClient(headers=HEADERS, follow_redirects=True) as client:

        await app.bot.send_message(chat_id, "🔐 Авторизуюсь на сайте...")
        token = await do_login(client, phone, password)

        if token and token != "cookie":
            client.headers.update({"Authorization": f"Bearer {token}"})
            await app.bot.send_message(chat_id, "✅ Авторизация прошла! Мониторинг запущен — проверяю каждые 2 минуты.")
        elif token == "cookie":
            await app.bot.send_message(chat_id, "✅ Вошёл через сессию! Мониторинг запущен.")
        else:
            await app.bot.send_message(chat_id,
                "⚠️ Не смог авторизоваться — смотрю матчи в открытом доступе.\n"
                "Мониторинг продолжается. Посмотри логи на Bothost — там виден ответ API.")

        check_count = 0
        while True:
            try:
                matches = await get_matches(client)
                check_count += 1
                now = datetime.now().strftime("%H:%M")

                available = [m for m in matches if match_available(m)]
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

                if check_count % 20 == 0:
                    await app.bot.send_message(chat_id,
                        f"🔄 Бот работает. Проверок: {check_count}\n"
                        f"Матчей: {len(matches)}, с билетами: {len(available)}\n🕐 {now}")

            except asyncio.CancelledError:
                return
            except Exception as e:
                log.error(f"[{chat_id}] Ошибка: {e}")

            await asyncio.sleep(CHECK_INTERVAL)


# ── Handlers ────────────────────────────────────

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
    await update.message.reply_text("🔑 Теперь введи пароль _(удалится сразу)_", parse_mode="Markdown")
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
    app.add_handler(CommandHandler("stop", cmd_stop))
    app.add_handler(CommandHandler("status", cmd_status))
    log.info("🤖 Бот запущен")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
