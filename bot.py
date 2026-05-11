#!/usr/bin/env python3
"""
Telegram-бот: мониторинг билетов ХК Ак Барс
API билетов: api.tna-tickets.ru
Авторизация: api.ak-bars.ru/portal/auth/login
"""

import asyncio
import logging
import os
import sqlite3
from pathlib import Path
from datetime import datetime

import httpx
import httpx_socks
from telegram import Update, ReplyKeyboardMarkup, ReplyKeyboardRemove
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    ConversationHandler, ContextTypes, filters,
)

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "8593827143:AAFgSm-Y5cKU1LYbQv6Bc9WeA2EauVbPsZM")
CHECK_INTERVAL     = int(os.getenv("CHECK_INTERVAL", "120"))
TICKETS_URL        = "https://www.ak-bars.ru/tickets"

# Авторизация
AK_LOGIN_URL = "https://api.ak-bars.ru/portal/auth/login"

# TNA Tickets API — реальный источник билетов
TNA_TOKEN    = "5f4dbf2e5629d8cc19e7d51874266678"
TNA_GAMES    = f"https://api.tna-tickets.ru/api/v1/game?access-token={TNA_TOKEN}&sport=1"
TNA_SECTORS  = "https://api.tna-tickets.ru/api/v1/booking/{id}/sectors?access-token=" + TNA_TOKEN

# Прокси
PROXY = "socks5://okurali02g:ZCUqsM7kgx@45.153.163.149:50101"

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
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/148.0.0.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Content-Type": "application/json",
    "Origin": "https://www.ak-bars.ru",
    "Referer": "https://www.ak-bars.ru/",
}


async def do_login(client: httpx.AsyncClient, phone: str, password: str) -> bool:
    try:
        r = await client.post(AK_LOGIN_URL, json={"login": phone, "password": password}, timeout=15)
        log.info(f"Login → {r.status_code}")
        if r.status_code != 200:
            log.error(f"Login failed: {r.text[:200]}")
            return False
        ak_token = r.headers.get("ak-token") or r.headers.get("AK-Token")
        if ak_token:
            client.headers.update({"Authorization": f"Bearer {ak_token}"})
            log.info("Токен получен!")
            return True
        log.warning("200 OK но токен не найден")
        return True
    except Exception as e:
        log.error(f"Login error: {e}")
        return False


async def get_tickets(client: httpx.AsyncClient) -> list[dict]:
    """Получаем матчи и проверяем секторы через api.tna-tickets.ru"""
    result = []
    try:
        # Шаг 1: список матчей
        r = await client.get(TNA_GAMES, timeout=15)
        log.info(f"TNA Games → {r.status_code}")
        if r.status_code != 200:
            log.error(f"TNA Games error: {r.text[:200]}")
            return []

        data = r.json()
        log.info(f"TNA Games response: {str(data)[:600]}")

        # Извлекаем список игр
        games = []
        if isinstance(data, list):
            games = data
        elif isinstance(data, dict):
            for key in ("result", "data", "items", "results", "games", "matches", "list"):
                if isinstance(data.get(key), list):
                    games = data[key]
                    break

        log.info(f"Матчей найдено: {len(games)}")

        # Шаг 2: для каждого матча проверяем секторы
        for game in games:
            gid = (game.get("id") or game.get("booking_id") or
                   game.get("game_id") or game.get("uuid"))
            if not gid:
                log.warning(f"Нет ID у матча: {game}")
                continue

            sectors_url = TNA_SECTORS.format(id=gid)
            try:
                rs = await client.get(sectors_url, timeout=15)
                log.info(f"Sectors [{gid}] → {rs.status_code}")
                if rs.status_code != 200:
                    continue

                sd = rs.json()
                log.info(f"Sectors [{gid}]: {str(sd)[:400]}")

                sectors = []
                if isinstance(sd, list):
                    sectors = sd
                elif isinstance(sd, dict):
                    for key in ("data", "items", "sectors", "results"):
                        if isinstance(sd.get(key), list):
                            sectors = sd[key]
                            break

                # Считаем свободные места
                free = []
                for s in sectors:
                    cnt = (s.get("available") or s.get("free") or
                           s.get("count") or s.get("seats_available") or
                           s.get("free_seats") or 0)
                    if cnt and int(cnt) > 0:
                        free.append(s)

                log.info(f"Матч [{gid}]: секторов={len(sectors)}, свободных={len(free)}")

                game["_id"] = str(gid)
                game["_sectors_total"] = len(sectors)
                game["_sectors_free"] = len(free)
                game["_has_tickets"] = len(free) > 0
                result.append(game)

            except Exception as e:
                log.error(f"Sectors [{gid}] error: {e}")

    except Exception as e:
        log.error(f"TNA Games exception: {e}")

    return result


def game_id(m):
    return str(m.get("_id") or m.get("id") or m.get("booking_id") or str(m)[:50])

def game_label(m):
    home  = m.get("home_team") or m.get("home") or "Ак Барс"
    away  = (m.get("away_team") or m.get("away") or m.get("guest") or
             m.get("opponent") or m.get("title") or m.get("name") or "—")
    date  = (m.get("date") or m.get("game_date") or m.get("match_date") or
             m.get("start_at") or m.get("startAt") or "")
    price = (m.get("price") or m.get("min_price") or m.get("minPrice") or
             m.get("min_cost") or "")
    free  = m.get("_sectors_free", 0)
    total = m.get("_sectors_total", 0)

    parts = [f"🏒 {home} — {away}"]
    if date:  parts.append(f"📅 {date}")
    if price: parts.append(f"💰 от {price} ₽")
    parts.append(f"✅ Свободных секторов: {free} из {total}")
    return "\n".join(parts)

def has_tickets(m):
    return m.get("_has_tickets", False)


async def monitor(chat_id: int, phone: str, password: str, app: Application):
    log.info(f"[{chat_id}] Мониторинг запущен")

    transport = httpx_socks.AsyncProxyTransport.from_url(PROXY)
    async with httpx.AsyncClient(headers=HEADERS, follow_redirects=True, transport=transport) as client:

        await app.bot.send_message(chat_id, "🔐 Авторизуюсь на сайте...")
        ok = await do_login(client, phone, password)

        if ok:
            await app.bot.send_message(chat_id, "✅ Авторизация прошла! Мониторинг запущен — проверяю каждые 2 минуты.")
        else:
            await app.bot.send_message(chat_id, "⚠️ Ошибка авторизации. Нажми /start и попробуй снова.")
            return

        check_count = 0
        while True:
            try:
                matches = await get_tickets(client)
                check_count += 1
                now = datetime.now().strftime("%H:%M")

                # Матчи с билетами
                with_tickets = [m for m in matches if has_tickets(m)]
                # Новые (ещё не уведомляли)
                new_ones = [m for m in with_tickets if not db_seen(chat_id, game_id(m))]

                if new_ones:
                    for m in new_ones:
                        db_mark(chat_id, game_id(m))
                    lines = "\n\n".join(game_label(m) for m in new_ones[:5])
                    await app.bot.send_message(
                        chat_id,
                        f"🚨 *БИЛЕТЫ ПОЯВИЛИСЬ!*\n\n{lines}\n\n"
                        f"👉 [Купить на сайте]({TICKETS_URL})\n🕐 {now}",
                        parse_mode="Markdown", disable_web_page_preview=True)
                else:
                    log.info(f"[{chat_id}] #{check_count} {now} матчей={len(matches)} с_билетами={len(with_tickets)} новых=0")

                if check_count % 60 == 0:
                    await app.bot.send_message(chat_id,
                        f"🔄 Бот работает. Проверок: {check_count}\n"
                        f"Матчей: {len(matches)}, с билетами: {len(with_tickets)}")

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
        reply_markup=ReplyKeyboardMarkup([["/stop", "/check"]], resize_keyboard=True))
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

async def cmd_test(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🚨 *БИЛЕТЫ ПОЯВИЛИСЬ!*\n\n"
        "🏒 Ак Барс — Локомотив\n"
        "📅 15 мая 2026, 19:00\n"
        "💰 от 3400 ₽\n"
        "✅ Свободных секторов: 5 из 18\n\n"
        "👉 [Купить на сайте](https://www.ak-bars.ru/tickets)\n\n"
        "_(это тестовое сообщение)_",
        parse_mode="Markdown", disable_web_page_preview=True)

async def cmd_check(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Мгновенная проверка билетов по запросу."""
    chat_id = update.effective_chat.id
    if chat_id not in tasks or tasks[chat_id].done():
        await update.message.reply_text("❌ Мониторинг не запущен. Нажми /start")
        return

    await update.message.reply_text("🔍 Проверяю прямо сейчас...")

    # Берём данные пользователя из БД
    with sqlite3.connect(DB_PATH) as c:
        row = c.execute("SELECT phone, password FROM users WHERE chat_id=?", (chat_id,)).fetchone()
    if not row:
        await update.message.reply_text("❌ Не найден аккаунт. Нажми /start")
        return

    phone, password = row
    transport = httpx_socks.AsyncProxyTransport.from_url(PROXY)
    async with httpx.AsyncClient(headers=HEADERS, follow_redirects=True, transport=transport) as client:
        await do_login(client, phone, password)
        matches = await get_tickets(client)

    with_tickets = [m for m in matches if has_tickets(m)]
    now = datetime.now().strftime("%H:%M")

    if with_tickets:
        lines = "\n\n".join(game_label(m) for m in with_tickets[:5])
        await update.message.reply_text(
            f"🚨 *БИЛЕТЫ ЕСТЬ!*\n\n{lines}\n\n"
            f"👉 [Купить на сайте]({TICKETS_URL})\n🕐 {now}",
            parse_mode="Markdown", disable_web_page_preview=True)
    else:
        await update.message.reply_text(
            f"❌ Билетов пока нет\n"
            f"Матчей на сайте: {len(matches)}\n"
            f"🕐 Проверено в {now}")

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
    app.add_handler(CommandHandler("test", cmd_test))
    app.add_handler(CommandHandler("check", cmd_check))
    log.info("🤖 Бот запущен")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
