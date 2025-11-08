from __future__ import annotations

from telegram import Update
from telegram.ext import ContextTypes
from ..config import DEFAULT_BASE
from ..ui import cancel_markup, main_menu_markup
from ..storage import db, get_user
from ..logic import normalize_ticker, add_tickers, remove_tickers
from ..charts import parse_period, history_for_chart, make_chart

async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    user = get_user(db, uid)
    mode = context.user_data.get("mode")
    text = (update.message.text or "").strip()

    if mode == "add":
        parts = [normalize_ticker(x) for x in text.split() if normalize_ticker(x)]
        if not parts:
            await update.message.reply_text("Не распознал тикеры. Пример: `AAPL NVDA BTC`", parse_mode="Markdown", reply_markup=cancel_markup())
            return
        new_list = add_tickers(db, uid, parts)
        context.user_data.pop("mode", None)
        await update.message.reply_text("Ок. Текущий список: " + (", ".join(new_list) if new_list else "пусто"), reply_markup=main_menu_markup(get_user(db, uid)))
        return

    if mode == "remove":
        parts = [normalize_ticker(x) for x in text.split() if normalize_ticker(x)]
        if not parts:
            await update.message.reply_text("Не распознал тикеры. Пример: `AAPL NVDA`", parse_mode="Markdown", reply_markup=cancel_markup())
            return
        removed = remove_tickers(db, uid, parts)
        context.user_data.pop("mode", None)
        await update.message.reply_text(("Удалил: " + ", ".join(removed)) if removed else "Ничего не удалил.", reply_markup=main_menu_markup(get_user(db, uid)))
        return

    if mode == "chart":
        args = text.split()
        ticker = normalize_ticker(args[0]) if args else ""
        period_days = parse_period(args[1]) if len(args) > 1 else 7
        base = (user.get("base") or DEFAULT_BASE).lower()
        if not ticker:
            await update.message.reply_text("Пример: `BTC 30d`", parse_mode="Markdown", reply_markup=cancel_markup())
            return
        series = history_for_chart(ticker, base, period_days)
        if not series:
            await update.message.reply_text("Не удалось получить историю для графика.", reply_markup=main_menu_markup(user))
            return
        png = make_chart(series, ticker, base)
        context.user_data.pop("mode", None)
        if not png:
            await update.message.reply_text("Не удалось построить график.", reply_markup=main_menu_markup(user))
            return
        await update.message.reply_photo(photo=png, caption=f"{ticker} · {period_days}d", reply_markup=main_menu_markup(user))
        return

    await update.message.reply_text("Используй кнопки ниже ⤵️", reply_markup=main_menu_markup(user))