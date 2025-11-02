import os
from pathlib import Path
from typing import List, Dict
import ujson as json
import yfinance as yf
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

DATA_PATH = Path("data.json")

def load_db() -> Dict[str, Dict]:
    if not DATA_PATH.exists():
        return {}
    try:
        return json.loads(DATA_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}

def save_db(db: Dict[str, Dict]):
    DATA_PATH.write_text(json.dumps(db, ensure_ascii=False, indent=2), encoding="utf-8")

def get_user(db: Dict[str, Dict], uid: int) -> Dict:
    return db.get(str(uid), {"tickers": []})

def set_user(db: Dict[str, Dict], uid: int, user: Dict):
    db[str(uid)] = user
    save_db(db)

def add_tickers(db: Dict[str, Dict], uid: int, symbols: List[str]) -> List[str]:
    user = get_user(db, uid)
    exist = set(t.upper() for t in user.get("tickers", []))
    for s in symbols:
        s = s.strip().upper()
        if s:
            exist.add(s)
    user["tickers"] = sorted(exist)
    set_user(db, uid, user)
    return user["tickers"]

def fetch_prices(symbols: List[str]) -> Dict[str, Dict[str, float]]:
    out = {}
    for sym in symbols:
        sym = sym.strip().upper()
        if not sym:
            continue

        last_price = None
        prev_close = None

        try:
            ticker = yf.Ticker(sym)
        except Exception:
            continue

        try:
            daily = ticker.history(period="10d", interval="1d")
        except Exception:
            daily = None

        if daily is not None and not daily.empty:
            closes = daily.get("Close")
            if closes is not None:
                closes = closes.dropna()
                if not closes.empty:
                    last_price = float(closes.iloc[-1])
                    if len(closes) >= 2:
                        prev_close = float(closes.iloc[-2])

        try:
            intraday = ticker.history(period="1d", interval="1m")
        except Exception:
            intraday = None

        if intraday is not None and not intraday.empty:
            intraday_closes = intraday.get("Close")
            if intraday_closes is not None:
                intraday_closes = intraday_closes.dropna()
                if not intraday_closes.empty:
                    last_price = float(intraday_closes.iloc[-1])

        if last_price is None:
            continue

        change = None
        change_pct = None
        if prev_close is not None and prev_close != 0:
            change = last_price - prev_close
            change_pct = (change / prev_close) * 100

        out[sym] = {
            "price": last_price,
            "change": change,
            "change_pct": change_pct,
        }

    return out

def parse_args(text: str) -> List[str]:
    parts = text.strip().split()
    return parts[1:] if len(parts) > 1 else []

load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN", "")

db = load_db()

async def start(update: Update, _: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Привет! Я простой бот для котировок.\n"
        "Команды:\n"
        "/add TICKER1 TICKER2 — добавить\n"
        "/list — список тикеров\n"
        "/price [тикеры] — показать цены"
    )

async def add_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = parse_args(update.message.text)
    if not args:
        await update.message.reply_text("Использование: /add AAPL NVDA TSLA")
        return
    new_list = add_tickers(db, update.effective_user.id, args)
    await update.message.reply_text("Ок. Текущий список: " + (", ".join(new_list) if new_list else "пусто"))

async def list_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = get_user(db, update.effective_user.id)
    tickers = user.get("tickers", [])
    await update.message.reply_text("Тикеры: " + (", ".join(tickers) if tickers else "пусто"))

async def price_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = parse_args(update.message.text)
    user = get_user(db, update.effective_user.id)
    tickers = args or user.get("tickers", [])
    if not tickers:
        await update.message.reply_text("Список пуст. Добавь тикеры: /add AAPL NVDA")
        return
    prices = fetch_prices(tickers)
    if not prices:
        await update.message.reply_text("Не удалось получить цены. Попробуй позже.")
        return
    lines = []
    for k in sorted(prices):
        data = prices[k]
        price = data.get("price")
        change = data.get("change")
        change_pct = data.get("change_pct")
        parts = [f"{k}: {price:.2f}"] if price is not None else [f"{k}: —"]
        if change is not None and change_pct is not None:
            parts.append(f"({change:+.2f}, {change_pct:+.2f}% за день)")
        elif change is not None:
            parts.append(f"({change:+.2f} за день)")
        lines.append(" ".join(parts))
    await update.message.reply_text("📊 Цены:\n" + "\n".join(lines))

def main():
    if not BOT_TOKEN:
        raise SystemExit("Нет BOT_TOKEN в .env")
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("add", add_cmd))
    app.add_handler(CommandHandler("list", list_cmd))
    app.add_handler(CommandHandler("price", price_cmd))
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()