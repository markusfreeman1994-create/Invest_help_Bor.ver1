from __future__ import annotations

from telegram import Update
from telegram.ext import ContextTypes
from ..config import DEFAULT_BASE
from ..ui import main_menu_markup, format_amount
from ..storage import db, get_user, set_user
from ..logic import normalize_ticker, add_tickers, remove_tickers, clear_tickers, fetch_prices_sync
from ..charts import parse_period, history_for_chart, make_chart

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = get_user(db, update.effective_user.id)
    text = (
        "Привет! Я крипто/рынок-бот: акции через Stooq, TON через TonAPI, остальное через CoinGecko.\n"
        "Выбирай действие кнопками ниже. Где нужен ввод — я попрошу написать в чат.\n\n"
        "Поддержка: /chart и команды тоже работают, но кнопки удобнее 😊"
    )
    await update.message.reply_text(text, reply_markup=main_menu_markup(user))

async def add_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if not args:
        await update.message.reply_text("Использование: /add AAPL NVDA TSLA")
        return
    new_list = add_tickers(db, update.effective_user.id, args)
    await update.message.reply_text("Ок. Текущий список: " + (", ".join(new_list) if new_list else "пусто"))

async def list_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = get_user(db, update.effective_user.id)
    tickers = user.get("tickers", [])
    await update.message.reply_text("Тикеры: " + (", ".join(tickers) if tickers else "пусто"))

async def remove_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if not args:
        await update.message.reply_text("Использование: /remove AAPL NVDA")
        return
    removed = remove_tickers(db, update.effective_user.id, args)
    if removed:
        await update.message.reply_text("Удалил: " + ", ".join(removed))
    else:
        await update.message.reply_text("Ничего не удалил — не нашёл таких тикеров в списке.")

async def clear_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    clear_tickers(db, update.effective_user.id)
    await update.message.reply_text("Список очищен.")

async def price_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    user = get_user(db, update.effective_user.id)
    base = (user.get("base") or DEFAULT_BASE).lower()
    tickers = [normalize_ticker(t) for t in (args or user.get("tickers", []))]
    tickers = [t for t in tickers if t]
    if not tickers:
        await update.message.reply_text("Список пуст. Добавь тикеры: /add BTC ETH TON")
        return
    from asyncio import to_thread
    prices = await to_thread(fetch_prices_sync, tickers, base)
    if not prices:
        await update.message.reply_text("Не удалось получить цены. Попробуй позже.")
        return
    lines = []
    for k in tickers:
        p = prices.get(k)
        if not p:
            continue
        chg = p.get("chg")
        chg_txt = f" ({chg:+.2%})" if isinstance(chg, (int, float)) else ""
        price_txt = format_amount(p["price"], 2)
        lines.append(f"{k}: {price_txt} {base.upper()}{chg_txt}")
    await update.message.reply_text("📊 Котировки:\n" + ("\n".join(lines) if lines else "Нет данных."))

async def setbase_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Использование: /setbase USD | EUR | RUB")
        return
    base = context.args[0].lower()
    if base not in {"usd","eur","rub"}:
        await update.message.reply_text("Поддерживаемые валюты: USD, EUR, RUB")
        return
    user = get_user(db, update.effective_user.id)
    user["base"] = base
    set_user(db, update.effective_user.id, user)
    await update.message.reply_text(f"Базовая валюта установлена: {base.upper()}")

async def chart_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if not args:
        await update.message.reply_text("Использование: /chart TICKER [7d|30d|90d|1y]")
        return
    ticker = normalize_ticker(args[0])
    period_days = parse_period(args[1]) if len(args) > 1 else 7
    user = get_user(db, update.effective_user.id)
    base = (user.get("base") or DEFAULT_BASE).lower()
    series = history_for_chart(ticker, base, period_days)
    if not series:
        await update.message.reply_text("Не удалось получить историю для графика.")
        return
    png = make_chart(series, ticker, base)
    if not png:
        await update.message.reply_text("Не удалось построить график.")
        return
    await update.message.reply_photo(photo=png, caption=f"{ticker} · {period_days}d")