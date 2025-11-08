from __future__ import annotations

from telegram import InlineKeyboardMarkup, InlineKeyboardButton, Update
from telegram.ext import ContextTypes
from ..config import DEFAULT_BASE
from ..ui import main_menu_markup, cancel_markup, base_menu_markup, format_amount
from ..storage import db, get_user, set_user
from ..logic import normalize_ticker, add_tickers, remove_tickers, fetch_prices_sync

async def on_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    data = q.data or ""
    uid = update.effective_user.id
    user = get_user(db, uid)

    if data == "ACT:BACK":
        await q.edit_message_reply_markup(reply_markup=main_menu_markup(user))
        return
    if data == "ACT:LIST":
        tickers = user.get("tickers", [])
        text = "Тикеры: " + (", ".join(tickers) if tickers else "пусто")
        await q.message.reply_text(text, reply_markup=main_menu_markup(user))
        return
    if data == "ACT:PRICE":
        base = (user.get("base") or DEFAULT_BASE).lower()
        tickers = user.get("tickers", [])
        if not tickers:
            await q.message.reply_text("Список пуст. Нажми «➕ Добавить».", reply_markup=main_menu_markup(user))
            return
        from asyncio import to_thread
        prices = await to_thread(fetch_prices_sync, tickers, base)
        if not prices:
            await q.message.reply_text("Не удалось получить цены. Попробуй позже.", reply_markup=main_menu_markup(user))
            return
        lines = []
        for k in tickers:
            p = prices.get(k)
            if not p:
                continue
            chg = p.get("chg")
            chg_txt = f" ({chg:+.2%})" if isinstance(chg, (int, float)) else ""
            price_txt = format_amount(p["price"], 2)
            lines.append(f"{k}: {price_txt} {(base or 'usd').upper()}{chg_txt}")
        await q.message.reply_text("📊 Котировки:\n" + ("\n".join(lines) if lines else "Нет данных."), reply_markup=main_menu_markup(user))
        return
    if data == "ACT:ADD":
        context.user_data["mode"] = "add"
        await q.message.reply_text("Введи тикеры через пробел (пример: `AAPL NVDA BTC`).", parse_mode="Markdown", reply_markup=cancel_markup())
        return
    if data == "ACT:REMOVE":
        context.user_data["mode"] = "remove"
        await q.message.reply_text("Введи тикеры для удаления (пример: `AAPL NVDA`).", parse_mode="Markdown", reply_markup=cancel_markup())
        return
    if data == "ACT:CLEAR":
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ Подтвердить очистку", callback_data="CONFIRM:CLEAR")],
            [InlineKeyboardButton("◀️ Отмена", callback_data="ACT:BACK")],
        ])
        await q.message.reply_text("Очистить список тикеров?", reply_markup=kb)
        return
    if data == "CONFIRM:CLEAR":
        user["tickers"] = []
        set_user(db, uid, user)
        await q.message.reply_text("Список очищен.", reply_markup=main_menu_markup(user))
        return
    if data == "ACT:BASE":
        await q.message.reply_text("Выбери базовую валюту:", reply_markup=base_menu_markup(user))
        return
    if data.startswith("BASE:"):
        base = data.split(":",1)[1].lower()
        if base in {"usd","eur","rub"}:
            user["base"] = base
            set_user(db, uid, user)
            await q.message.reply_text(f"Базовая валюта: {base.upper()}", reply_markup=main_menu_markup(user))
        else:
            await q.message.reply_text("Неподдерживаемая валюта.", reply_markup=main_menu_markup(user))
        return
    if data == "ACT:CHART":
        context.user_data["mode"] = "chart"
        await q.message.reply_text("Введи тикер и период (пример: `BTC 30d`). По умолчанию — 7d.", parse_mode="Markdown", reply_markup=cancel_markup())
        return