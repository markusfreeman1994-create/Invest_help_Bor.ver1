from __future__ import annotations

import os
from pathlib import Path
from typing import List, Dict
import re
import asyncio
import logging
import tempfile
import time
import io
from datetime import datetime
import requests
import ujson as json
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.dates as mdates

COINGECKO_BASE = os.getenv("COINGECKO_BASE", "https://api.coingecko.com/api/v3")
COINGECKO_API_KEY = os.getenv("COINGECKO_API_KEY") or os.getenv("CG_API_KEY")
TONAPI_BASE = os.getenv("TONAPI_BASE", "https://tonapi.io")
DEFAULT_BASE = os.getenv("DEFAULT_BASE", "usd").lower()

# Minimal static symbol->id map for CoinGecko (we'll extend dynamically on demand)
CG_ID_MAP_STATIC = {
    "BTC": "bitcoin",
    "ETH": "ethereum",
    "TON": "toncoin",
    "USDT": "tether",
    "USDC": "usd-coin",
    "BNB": "binancecoin",
    "SOL": "solana",
    "TRX": "tron",
    "DOGE": "dogecoin",
  }
_cg_symbol_to_id_cache: Dict[str, str] = {}
_fx_cache: Dict[str, float] = {}

DATA_PATH = Path("data.json")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger("invest_bot")
# keep noise low for HTTP libs
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("urllib3").setLevel(logging.WARNING)

def load_db() -> Dict[str, Dict]:
    if not DATA_PATH.exists():
        return {}
    try:
        return json.loads(DATA_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}

def save_db(db: Dict[str, Dict]):
    tmp_fd, tmp_path = tempfile.mkstemp(prefix="data.", suffix=".json")
    with os.fdopen(tmp_fd, "w", encoding="utf-8") as f:
        f.write(json.dumps(db, ensure_ascii=False, indent=2))
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp_path, DATA_PATH)

def get_user(db: Dict[str, Dict], uid: int) -> Dict:
    return db.get(str(uid), {"tickers": [], "base": DEFAULT_BASE})

def set_user(db: Dict[str, Dict], uid: int, user: Dict):
    db[str(uid)] = user
    save_db(db)

def _ya_session() -> requests.Session:
    s = requests.Session()
    s.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    })
    return s

def _http_get_json(url: str, params: Dict = None, headers: Dict = None, timeout: int = 10):
    try:
        r = requests.get(url, params=params, headers=headers, timeout=timeout)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        logger.warning("GET %s failed: %s", url, e)
        return None

def _cg_headers() -> Dict[str, str]:
    h = {"Accept": "application/json"}
    if COINGECKO_API_KEY:
        # CoinGecko now requires an API key (free or pro)
        h["x-cg-pro-api-key"] = COINGECKO_API_KEY
    return h

# --- FX rates and Yahoo Stocks provider ---
def _fx_rate(frm: str, to: str) -> float | None:
    frm = (frm or "").upper()
    to = (to or "").upper()
    if not frm or not to:
        return None
    if frm == to:
        return 1.0
    key = f"{frm}->{to}"
    if key in _fx_cache:
        return _fx_cache[key]
    data = _http_get_json(
        "https://api.exchangerate.host/convert",
        params={"from": frm, "to": to},
        timeout=10,
    )
    rate = None
    if isinstance(data, dict):
        rate = data.get("result") or (data.get("info", {}) or {}).get("rate")
    if isinstance(rate, (int, float)) and rate > 0:
        _fx_cache[key] = float(rate)
        return _fx_cache[key]
    logger.warning("FX rate fetch failed %s -> %s", frm, to)
    # Fallback provider
    data2 = _http_get_json(
        f"https://open.er-api.com/v6/latest/{frm}",
        timeout=10,
    )
    try:
        rate2 = (data2 or {}).get("rates", {}).get(to)
        if isinstance(rate2, (int, float)) and rate2 > 0:
            _fx_cache[key] = float(rate2)
            return _fx_cache[key]
    except Exception:
        pass
    return None
def _fetch_history_stooq(symbol: str, days: int) -> List[tuple[datetime, float]]:
    """
    Daily close history for US stock from Stooq (no key).
    Returns list of (datetime, close_usd). Cuts to last `days`.
    """
    sym = symbol.upper()
    try:
        stooq_sym = f"{sym.replace('-', '.').lower()}.us"
        url = "https://stooq.com/q/d/l/"
        params = {"s": stooq_sym, "i": "d"}
        r = requests.get(url, params=params, timeout=10)
        r.raise_for_status()
        lines = (r.text or "").strip().splitlines()
        if len(lines) < 3:
            return []
        out = []
        for row in lines[1:]:
            parts = row.split(",")
            if len(parts) >= 5 and parts[4] not in ("", "null", "None"):
                try:
                    dt = datetime.strptime(parts[0], "%Y-%m-%d")
                    close = float(parts[4])
                    out.append((dt, close))
                except Exception:
                    continue
        out = [x for x in out if isinstance(x[1], (int, float))]
        out.sort(key=lambda t: t[0])
        if days and len(out) > days:
            out = out[-days:]
        return out
    except Exception as e:
        logger.warning("Stooq history failed for %s: %s", sym, e)
        return []

def _fetch_history_coingecko(symbol: str, vs: str, days: int) -> List[tuple[datetime, float]]:
    """
    Uses /coins/{id}/market_chart to get historical prices. Returns list of (datetime, price_vs).
    """
    sym = symbol.upper()
    ids_map = _map_symbols_to_cg_ids([sym])
    cid = ids_map.get(sym)
    if not cid:
        return []
    params = {"vs_currency": vs, "days": str(days), "interval": "daily" if days > 1 else "hourly"}
    data = _http_get_json(f"{COINGECKO_BASE}/coins/{cid}/market_chart", params=params, headers=_cg_headers(), timeout=20)
    try:
        prices = (data or {}).get("prices") or []
        out = []
        for ts, price in prices:
            try:
                dt = datetime.utcfromtimestamp(ts / 1000.0)
                out.append((dt, float(price)))
            except Exception:
                continue
        return out
    except Exception as e:
        logger.warning("CG history failed for %s: %s", sym, e)
        return []

def _make_chart(series: List[tuple[datetime, float]], ticker: str, base: str) -> bytes:
    """
    Renders a simple line chart into PNG bytes.
    """
    if not series:
        return b""
    dates = [d for d, _ in series]
    values = [v for _, v in series]
    fig, ax = plt.subplots(figsize=(6, 3))
    ax.plot(dates, values, linewidth=2)
    ax.set_title(f"{ticker} in {base.upper()}")
    ax.set_xlabel("Date")
    ax.set_ylabel(base.upper())
    ax.grid(True, linestyle="--", linewidth=0.5, alpha=0.5)
    ax.xaxis.set_major_locator(mdates.AutoDateLocator())
    ax.xaxis.set_major_formatter(mdates.ConciseDateFormatter(ax.xaxis.get_major_locator()))
    fig.tight_layout()
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=160)
    plt.close(fig)
    buf.seek(0)
    return buf.read()

def _fetch_from_stooq_stocks(symbols: List[str], vs: str) -> Dict[str, Dict[str, float]]:
    """
    Fetch US stock daily close and % change from Stooq (no API key).
    Converts prices to `vs` (USD/EUR/RUB) via exchangerate.host.
    """
    out: Dict[str, Dict[str, float]] = {}
    if not symbols:
        return out
    rate = _fx_rate("USD", (vs or "USD").upper())
    if not isinstance(rate, (int, float)) or rate <= 0:
        logger.warning("FX rate unavailable for USD -> %s", vs)
        return out
    for sym in symbols:
        try:
            # Stooq uses suffix .us for US listings; symbols are lowercase and dots for class (e.g., brk.b)
            stooq_sym = f"{sym.replace('-', '.').lower()}.us"
            url = "https://stooq.com/q/d/l/"
            params = {"s": stooq_sym, "i": "d"}
            r = requests.get(url, params=params, timeout=10)
            r.raise_for_status()
            txt = (r.text or "").strip()
            lines = txt.splitlines()
            # Need at least header + 2 rows to compute change
            if len(lines) < 3:
                continue
            # CSV header: Date,Open,High,Low,Close,Volume
            def _close_of(row: str):
                parts = row.split(",")
                if len(parts) >= 5 and parts[4] not in ("", "null", "None"):
                    try:
                        return float(parts[4])
                    except Exception:
                        return None
                return None
            last_close = _close_of(lines[-1])
            prev_close = _close_of(lines[-2])
            if not isinstance(last_close, (int, float)) or not isinstance(prev_close, (int, float)) or prev_close == 0:
                continue
            price_vs = float(last_close) * float(rate)
            chg = (last_close - prev_close) / prev_close
            out[sym.upper()] = {"price": price_vs, "chg": chg, "src": "stooq"}
        except Exception as e:
            logger.warning("Stooq fetch failed for %s: %s", sym, e)
            continue
    return out

def normalize_ticker(s: str) -> str:
    s = s.strip().upper()
    s = re.sub(r'^\$+', '', s)              # remove leading $
    s = re.sub(r'[^A-Z0-9\.\-]', '', s)     # keep only valid chars
    s = s.replace('.', '-')                 # Yahoo format: BRK.B -> BRK-B
    return s

def remove_tickers(db: Dict[str, Dict], uid: int, symbols: List[str]) -> List[str]:
    user = get_user(db, uid)
    current = set(user.get("tickers", []))
    to_remove = {normalize_ticker(s) for s in symbols if normalize_ticker(s)}
    removed = sorted(list(current & to_remove))
    current -= to_remove
    user["tickers"] = sorted(current)
    set_user(db, uid, user)
    return removed

def clear_tickers(db: Dict[str, Dict], uid: int) -> None:
    user = get_user(db, uid)
    user["tickers"] = []
    set_user(db, uid, user)

def add_tickers(db: Dict[str, Dict], uid: int, symbols: List[str]) -> List[str]:
    user = get_user(db, uid)
    exist = set(normalize_ticker(t) for t in user.get("tickers", []))
    for s in symbols:
        s = normalize_ticker(s)
        if s:
            exist.add(s)
    user["tickers"] = sorted(exist)
    set_user(db, uid, user)
    return user["tickers"]

def _map_symbols_to_cg_ids(symbols: List[str]) -> Dict[str, str]:
    """Map common tickers like BTC -> bitcoin, TON -> toncoin using static map and one-time /coins/list fetch."""
    out = {}
    unknown = []
    for s in symbols:
        key = s.upper()
        if key in CG_ID_MAP_STATIC:
            out[key] = CG_ID_MAP_STATIC[key]
        elif key in _cg_symbol_to_id_cache:
            out[key] = _cg_symbol_to_id_cache[key]
        else:
            unknown.append(key)
    if unknown:
        # Lazy-load list and map by symbol (first match)
        data = _http_get_json(f"{COINGECKO_BASE}/coins/list", headers=_cg_headers(), timeout=20)
        if isinstance(data, list):
            # Build a symbol->id map (symbols are lowercase on CoinGecko)
            tmp = {}
            for item in data:
                sym = (item.get("symbol") or "").upper()
                cid = item.get("id")
                if sym and cid and sym not in tmp:
                    tmp[sym] = cid
            for sym in unknown:
                cid = tmp.get(sym)
                if cid:
                    _cg_symbol_to_id_cache[sym] = cid
                    out[sym] = cid
    return out

def _fetch_from_coingecko(symbols: List[str], vs: str) -> Dict[str, Dict[str, float]]:
    ids_map = _map_symbols_to_cg_ids(symbols)
    if not ids_map:
        return {}
    ids_csv = ",".join(sorted(set(ids_map.values())))
    params = {
        "ids": ids_csv,
        "vs_currencies": vs,
        "include_24hr_change": "true",
    }
    data = _http_get_json(f"{COINGECKO_BASE}/simple/price", params=params, headers=_cg_headers(), timeout=15)
    out: Dict[str, Dict[str, float]] = {}
    if isinstance(data, dict):
        for sym, cid in ids_map.items():
            row = data.get(cid)
            if isinstance(row, dict) and vs in row:
                price = row.get(vs)
                chg = row.get(f"{vs}_24h_change")
                if isinstance(price, (int, float)):
                    out[sym] = {"price": float(price), "chg": float(chg) / 100.0 if isinstance(chg, (int, float)) else None, "src": "coingecko"}
    return out

def _fetch_from_tonapi(symbols: List[str], vs: str) -> Dict[str, Dict[str, float]]:
    # TonAPI covers TON and TON jettons. For now we support TON via 'tokens=ton'.
    out: Dict[str, Dict[str, float]] = {}
    need_ton = any(s.upper() == "TON" for s in symbols)
    if not need_ton:
        return out
    params = {"tokens": "ton", "currencies": vs}
    data = _http_get_json(f"{TONAPI_BASE}/v2/rates", params=params, timeout=10)
    # Expected format (per OpenAPI): {"rates": {"ton": { ... currencies/prices ... } } }
    try:
        if isinstance(data, dict) and "rates" in data:
            ton_obj = data["rates"].get("ton") or data["rates"].get("TON")
            if isinstance(ton_obj, dict):
                # Try common shapes
                # 1) {"prices":{"usd":2.3}}  2) {"usd":2.3}  3) {"values":{"usd":2.3}}
                candidates = []
                if "prices" in ton_obj and isinstance(ton_obj["prices"], dict):
                    candidates.append(ton_obj["prices"])
                if "values" in ton_obj and isinstance(ton_obj["values"], dict):
                    candidates.append(ton_obj["values"])
                if isinstance(ton_obj.get(vs), (int, float)):
                    candidates.append({vs: ton_obj.get(vs)})
                price = None
                for d in candidates:
                    if isinstance(d.get(vs), (int, float)):
                        price = float(d[vs]); break
                if price is not None:
                    out["TON"] = {"price": price, "chg": None, "src": "tonapi"}
    except Exception as e:
        logger.warning("TonAPI parse error: %s", e)
    return out

def fetch_prices_sync(symbols: List[str], vs: str) -> Dict[str, Dict[str, float]]:
    """
    Aggregate provider that returns prices in `vs` (usd/eur/rub) with daily change if available.
    Order: Yahoo (stocks) -> TonAPI (TON) -> CoinGecko (crypto & fallback).
    """
    symbols_norm = [normalize_ticker(s) for s in symbols if normalize_ticker(s)]
    if not symbols_norm:
        return {}
    vs_l = (vs or "usd").lower()
    vs_u = vs_l.upper()

    result: Dict[str, Dict[str, float]] = {}

    # 1) Stocks via Stooq (adds daily % change)
    try:
        stocks = _fetch_from_stooq_stocks(symbols_norm, vs_u)
        if stocks:
            result.update(stocks)
    except Exception as e:
        logger.warning("Stocks provider failed: %s", e)

    # 2) TON via TonAPI
    remaining = [s for s in symbols_norm if s not in result]
    if remaining:
        try:
            tonp = _fetch_from_tonapi(remaining, vs_l)
            if tonp:
                result.update(tonp)
        except Exception as e:
            logger.warning("TonAPI provider failed: %s", e)

    # 3) Everything else via CoinGecko (includes 24h change)
    remaining = [s for s in symbols_norm if s not in result]
    if remaining:
        try:
            cg = _fetch_from_coingecko(remaining, vs_l)
            if cg:
                result.update(cg)
        except Exception as e:
            logger.warning("CoinGecko provider failed: %s", e)

    return result

load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN", "")

db = load_db()

async def start(update: Update, _: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Привет! Я крипто/рынок-бот: акции через Stooq, TON через TonAPI, остальное через CoinGecko.\n"
        "Команды:\n"
        "/add BTC ETH TON AAPL NVDA — добавить тикеры\n"
        "/remove T1 T2 — удалить тикеры\n"
        "/clear — очистить список\n"
        "/list — показать список\n"
        "/price [тикеры] — цены и изменение за 24ч\n"
        "/chart TICKER [период] — график (пример: /chart BTC 30d)\n"
        "/setbase USD|EUR|RUB — базовая валюта\n"
        "\n"
        "⚙️ .env: BOT_TOKEN=<токен>, опц. COINGECKO_API_KEY=<key>"
    )
def _parse_period(arg: str) -> int:
    """
    Returns number of days for period strings like '1d','7d','30d','90d','1y'.
    Defaults to 7 days if invalid.
    """
    s = (arg or "").lower().strip()
    if s.endswith("d") and s[:-1].isdigit():
        return max(1, int(s[:-1]))
    if s in {"1y","12m"}:
        return 365
    if s in {"3m"}:
        return 90
    if s in {"6m"}:
        return 180
    return 7

async def chart_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Usage: /chart TICKER [period]
    args = context.args
    if not args:
        await update.message.reply_text("Использование: /chart TICKER [7d|30d|90d|1y]")
        return
    ticker = normalize_ticker(args[0])
    period_days = _parse_period(args[1]) if len(args) > 1 else 7
    user = get_user(db, update.effective_user.id)
    base = (user.get("base") or DEFAULT_BASE).lower()

    series: List[tuple[datetime, float]] = []
    # Try stocks via Stooq (USD), then convert to base
    stq = _fetch_history_stooq(ticker, period_days)
    if stq:
        rate = _fx_rate("USD", base.upper())
        if isinstance(rate, (int, float)) and rate > 0:
            series = [(d, v * rate) for (d, v) in stq]
    # Else try CoinGecko (crypto or fallback)
    if not series:
        series = _fetch_history_coingecko(ticker, base, period_days)

    if not series:
        await update.message.reply_text("Не удалось получить историю для графика.")
        return
    png = _make_chart(series, ticker, base)
    if not png:
        await update.message.reply_text("Не удалось построить график.")
        return
    await update.message.reply_photo(photo=png, caption=f"{ticker} · {period_days}d")

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
    prices = await asyncio.to_thread(fetch_prices_sync, tickers, base)
    if not prices:
        await update.message.reply_text("Не удалось получить цены. Попробуй позже.")
        return
    lines = []
    for k in tickers:
        p = prices.get(k)
        if not p:
            continue
        chg_txt = f" ({p['chg']:+.2%})" if isinstance(p.get("chg"), float) else ""
        src = p.get("src", "")
        src_txt = f" · {src}" if src else ""
        lines.append(f"{k}: {p['price']:.6g} {base.upper()}{chg_txt}{src_txt}")
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

def main():
    if not BOT_TOKEN:
        raise SystemExit("Нет BOT_TOKEN в .env")
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("add", add_cmd))
    app.add_handler(CommandHandler("remove", remove_cmd))
    app.add_handler(CommandHandler("clear", clear_cmd))
    app.add_handler(CommandHandler("list", list_cmd))
    app.add_handler(CommandHandler("price", price_cmd))
    app.add_handler(CommandHandler("chart", chart_cmd))
    app.add_handler(CommandHandler("setbase", setbase_cmd))
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()