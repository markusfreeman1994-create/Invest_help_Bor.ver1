from __future__ import annotations

# ========== STD/3rd-party imports ==========
import os
import re
import io
import time
import json
import asyncio
import logging
import tempfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from requests.adapters import HTTPAdapter
from pathlib import Path
from datetime import datetime, timedelta
from typing import Dict, List, Tuple, Optional
from contextlib import asynccontextmanager
from telegram.constants import ChatAction
from urllib.parse import urlparse

# --- circuit breakers (unix timestamps) ---
_AI_DOWN_UNTIL = globals().get('_AI_DOWN_UNTIL', 0.0)      # OpenAI временно «выключен»
_CG_DOWN_UNTIL = globals().get('_CG_DOWN_UNTIL', 0.0)      # CoinGecko временно «выключен»
_STOOQ_DOWN_UNTIL = globals().get('_STOOQ_DOWN_UNTIL', 0.0)  # Stooq временно «выключен»

import requests
try:
    import ujson as ujson_mod
except Exception:
    ujson_mod = None
try:
    from dotenv import load_dotenv
except Exception:
    load_dotenv = lambda: None

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.dates as mdates

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

# ========== Config ==========
load_dotenv()

COINGECKO_BASE = os.getenv("COINGECKO_BASE", "https://api.coingecko.com/api/v3")
COINGECKO_API_KEY = os.getenv("COINGECKO_API_KEY") or os.getenv("CG_API_KEY")
DEFAULT_BASE = (os.getenv("DEFAULT_BASE", "usd") or "usd").lower()
DATA_PATH = Path("data.json")

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY") or os.getenv("OPENAI_KEY")
OPENAI_BASE = os.getenv("OPENAI_BASE", "https://api.openai.com/v1")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o")
OPENAI_ORG = os.getenv("OPENAI_ORG")
OPENAI_PROJECT = os.getenv("OPENAI_PROJECT")
OPENAI_ALLOW_FALLBACK = os.getenv("OPENAI_ALLOW_FALLBACK", "0") in ("1", "true", "yes", "on")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger("invest_bot")
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("urllib3").setLevel(logging.WARNING)

# Reusable HTTP session with connection pooling
_SESSION = requests.Session()
_SESSION.mount("http://", HTTPAdapter(pool_connections=20, pool_maxsize=50, max_retries=0))
_SESSION.mount("https://", HTTPAdapter(pool_connections=20, pool_maxsize=50, max_retries=0))

# Thread pool for parallel I/O
_IO_POOL = ThreadPoolExecutor(max_workers=int(os.getenv("IO_POOL", "12")))

# ========== HTTP utils ==========
_DEFAULT_HEADERS = {
    "Accept": "application/json",
    "User-Agent": "InvestHelpBot/1.0 (+telegram-bot)",
}

def http_get_json(
        url: str,
        params: Dict = None,
        headers: Dict = None,
        timeout: int = 10,
        retries: int = 2,
        backoff: float = 0.6,
):
    host = ""
    try:
        host = urlparse(url).netloc
    except Exception:
        pass

        # Быстрый выход, если CoinGecko недавно «падал»
    if host.endswith("api.coingecko.com") and time.time() < _CG_DOWN_UNTIL:
        raise requests.exceptions.RequestException("CoinGecko temporarily disabled by breaker")

    tries = max(1, int(os.getenv("HTTP_JSON_RETRIES", "2")))
    backoff = float(os.getenv("HTTP_JSON_BACKOFF", "0.25"))
    for i in range(tries):
        try:
            r = _SESSION.get(url, params=params, headers=headers, timeout=timeout)
            r.raise_for_status()
            if ujson_mod:
                return ujson_mod.loads(r.text)
            return r.json()
        except requests.exceptions.ReadTimeout:
            # Армируем breaker для CG на последней попытке
            if host.endswith("api.coingecko.com") and (i + 1) == tries:
                globals()["_CG_DOWN_UNTIL"] = time.time() + float(os.getenv("CG_BREAKER_SEC", "45"))
            if (i + 1) >= tries:
                raise
        except requests.exceptions.RequestException:
            if (i + 1) >= tries:
                raise
        time.sleep(backoff * (i + 1))
    return {}

# Helper: HTTP POST JSON
def http_post_json(
        url: str,
        json_body: Dict,
        headers: Dict = None,
        timeout: int = 20,
):
    hdr = dict(_DEFAULT_HEADERS)
    if headers:
        hdr.update(headers)
    try:
        r = _SESSION.post(url, json=json_body, headers=hdr, timeout=timeout)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        logger.warning("POST %s failed: %s", url, e)
        return None

# ========== FX provider (with cache & fallbacks) ==========
_FX_CACHE: Dict[str, Tuple[float, float]] = {}  # key -> (rate, ts)
_FX_TTL = 3600.0
# Short-lived cache for aggregated prices per (tuple(sorted(symbols)), base)
_PRICE_CACHE: Dict[Tuple[Tuple[str, ...], str], Tuple[float, Dict[str, Dict[str, float]]]] = {}
_PRICE_TTL = float(os.getenv("PRICE_TTL", "5.0"))  # seconds

def _fx_cache_get(frm: str, to: str) -> Optional[float]:
    row = _FX_CACHE.get(f"{frm}->{to}")
    if not row:
        return None
    rate, ts = row
    if time.time() - ts < _FX_TTL:
        return rate
    return None

def _fx_cache_set(frm: str, to: str, rate: float):
    _FX_CACHE[f"{frm}->{to}"] = (float(rate), time.time())

def _try_exchangerate_host(frm: str, to: str) -> Optional[float]:
    data = http_get_json("https://api.exchangerate.host/convert", params={"from": frm, "to": to}, timeout=10)
    if isinstance(data, dict):
        rate = data.get("result") or (data.get("info", {}) or {}).get("rate")
        if isinstance(rate, (int, float)) and rate > 0:
            return float(rate)
    return None

def _try_open_er_api(frm: str, to: str) -> Optional[float]:
    data = http_get_json(f"https://open.er-api.com/v6/latest/{frm}", timeout=10)
    try:
        rate = (data or {}).get("rates", {}).get(to)
        if isinstance(rate, (int, float)) and rate > 0:
            return float(rate)
    except Exception:
        pass
    return None

def _try_frankfurter(frm: str, to: str) -> Optional[float]:
    data = http_get_json("https://api.frankfurter.app/latest", params={"from": frm, "to": to}, timeout=10)
    try:
        rate = (data or {}).get("rates", {}).get(to)
        if isinstance(rate, (int, float)) and rate > 0:
            return float(rate)
    except Exception:
        pass
    return None

def _try_cbr(frm: str, to: str) -> Optional[float]:
    if not {frm, to} <= {"USD", "RUB"}:
        return None
    data = http_get_json("https://www.cbr-xml-daily.ru/daily_json.js", timeout=10)
    try:
        usd = (data or {}).get("Valute", {}).get("USD", {})
        val = usd.get("Value") or usd.get("Previous")
        if isinstance(val, (int, float)) and val > 0:
            if frm == "USD" and to == "RUB":
                return float(val)
            if frm == "RUB" and to == "USD":
                return 1.0 / float(val)
    except Exception:
        pass
    return None

def fx_rate(frm: str, to: str) -> Optional[float]:
    frm = (frm or "").upper()
    to = (to or "").upper()
    if not frm or not to:
        return None
    if frm == to:
        return 1.0
    cached = _fx_cache_get(frm, to)
    if isinstance(cached, (int, float)):
        return cached
    for fn in (_try_exchangerate_host, _try_open_er_api, _try_frankfurter, _try_cbr):
        rate = fn(frm, to)
        if isinstance(rate, (int, float)) and rate > 0:
            _fx_cache_set(frm, to, rate)
            return rate
    logger.warning("FX rate fetch failed %s -> %s", frm, to)
    return None

# ====== Fast TTL caches (performance) ======
_HIST_CACHE: Dict[Tuple[str, str, int], Tuple[float, List[Tuple[datetime, float]]]] = {}
_HIST_TTL = float(os.getenv("HIST_TTL", "60"))  # seconds

_CG_CHG_CACHE: Dict[Tuple[str, str], Tuple[float, float]] = {}
_CG_CHG_TTL = float(os.getenv("CG_CHG_TTL", "60"))  # seconds

def _hist_cache_get(key: Tuple[str, str, int]) -> Optional[List[Tuple[datetime, float]]]:
    item = _HIST_CACHE.get(key)
    if item:
        ts, series = item
        if (time.time() - ts) < _HIST_TTL:
            return series
    return None

def _hist_cache_set(key: Tuple[str, str, int], series: List[Tuple[datetime, float]]):
    _HIST_CACHE[key] = (time.time(), series)

def _cg_chg_cache_get(key: Tuple[str, str]) -> Optional[float]:
    item = _CG_CHG_CACHE.get(key)
    if item:
        ts, val = item
        if (time.time() - ts) < _CG_CHG_TTL:
            return val
    return None

def _cg_chg_cache_set(key: Tuple[str, str], val: float):
    _CG_CHG_CACHE[key] = (time.time(), float(val))

# ========== Stooq provider (stocks) ==========
_STOOQ_DOWN_UNTIL = 0.0  # epoch seconds; when > now, skip Stooq calls
def _csv_close(row: str):
    parts = row.split(",")
    if len(parts) >= 5 and parts[4] not in ("", "null", "None"):
        try:
            return float(parts[4])
        except Exception:
            return None
    return None

def _csv_open(row: str):
    parts = row.split(",")
    if len(parts) >= 2 and parts[1] not in ("", "null", "None"):
        try:
            return float(parts[1])
        except Exception:
            return None
    return None

# Parallelized Stooq fetches
def _fetch_stooq_one(sym: str, usd_to_vs: float) -> Optional[Tuple[str, Dict[str, float]]]:
    try:
        if time.time() < _STOOQ_DOWN_UNTIL:
            return None
        stooq_sym = f"{sym.replace('-', '.').lower()}.us"
        url = "https://stooq.com/q/d/l/"
        params = {"s": stooq_sym, "i": "d"}
        r = _SESSION.get(url, params=params, timeout=10)
        r.raise_for_status()
        lines = (r.text or "").strip().splitlines()
        if len(lines) < 2:
            return None
        last_close = _csv_close(lines[-1])
        prev_close = _csv_close(lines[-2]) if len(lines) >= 3 else _csv_open(lines[-1])
        if not isinstance(last_close, (int, float)) or not isinstance(prev_close, (int, float)) or prev_close == 0:
            return None
        price_vs = float(last_close) * float(usd_to_vs)
        chg = (last_close - prev_close) / prev_close
        return sym.upper(), {"price": price_vs, "chg": chg}
    except Exception:
        globals()["_STOOQ_DOWN_UNTIL"] = time.time() + 300.0  # 5 минут пауза
        return None

def fetch_stock_prices(symbols: List[str], vs: str) -> Dict[str, Dict[str, float]]:
    out: Dict[str, Dict[str, float]] = {}
    if time.time() < _STOOQ_DOWN_UNTIL:
        return out  #
    if not symbols:
        return out
    rate = fx_rate("USD", (vs or "USD").upper())
    if not isinstance(rate, (int, float)) or rate <= 0:
        logger.warning("FX rate unavailable for USD -> %s", vs)
        return out
    futures = []
    # Limit per-batch workers to avoid hammering the source
    max_workers = min(len(symbols), 8)
    for sym in symbols:
        futures.append(_IO_POOL.submit(_fetch_stooq_one, sym, rate))
    for fut in as_completed(futures):
        res = fut.result()
        if res:
            k, v = res
            out[k] = v
        # Если Stooq что-то не вернул — попробуем добрать из Yahoo
    missing = [s for s in symbols if s.upper() not in out]
    if missing:
        y = _yahoo_fetch_prices(missing, vs)
        out.update(y)
    return out

def stooq_history(symbol: str, days: int) -> List[Tuple[datetime, float]]:
    global _STOOQ_DOWN_UNTIL
    # If Stooq recently failed, skip for a short time to avoid blocking
    if time.time() < _STOOQ_DOWN_UNTIL:
        return []
    sym = symbol.upper()
    try:
        stooq_sym = f"{sym.replace('-', '.').lower()}.us"
        url = "https://stooq.com/q/d/l/"
        params = {"s": stooq_sym, "i": "d"}
        r = requests.get(url, params=params, timeout=10)
        r.raise_for_status()
        lines = (r.text or "").strip().splitlines()
        if len(lines) < 2:
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
        # Back off from Stooq for 5 minutes to avoid repeated connection attempts
        _STOOQ_DOWN_UNTIL = time.time() + 300.0  # 5 минут паузы
        return []

# ========== MOEX provider (Russian stocks) ==========
# Heuristics for RU tickers (can be extended)
_RU_TICKERS = {
    "SBER", "GAZP", "LKOH", "GMKN", "ROSN", "NVTK", "TATN", "TATNP", "MTSS",
    "AFLT", "MAGN", "ALRS", "PLZL", "CHMF", "SNGS", "SNGSP", "VTBR", "POLY",
    "YDEX", "YNDX", "PHOR", "PIKK", "FIVE", "MOEX", "IRAO", "HYDR", "BANEP",
    "ENPG", "LSRG", "TRNFP", "MVID", "FIXP", "OZON", "QIWI", "RUAL", "RSTI",
    "MSNG", "RTKM", "RTKMP", "TGKA", "TGKB", "TGKD", "TGKN", "KMAZ",
}

def _is_ru_ticker(sym: str) -> bool:
    s = (sym or "").upper()
    return s.endswith("-ME") or s in _RU_TICKERS

def _moex_secid(sym: str) -> str:
    """Convert normalized ticker to MOEX SECID (strip '-ME' suffix if present)."""
    s = (sym or "").upper()
    return s[:-3] if s.endswith("-ME") else s

def _moex_candles(secid: str, days: int) -> List[Tuple[datetime, float]]:
    """
    Fetch daily candles for SECID from MOEX ISS API.
    Returns list of (UTC datetime, close RUB), ascending by time.
    """
    try:
        till = datetime.utcnow().date()
        frm = (till - timedelta(days=max(1, int(days)+3))).isoformat()
        url = f"https://iss.moex.com/iss/engines/stock/markets/shares/boards/TQBR/securities/{secid}/candles.json"
        params = {"from": frm, "till": till.isoformat(), "interval": 24}
        data = http_get_json(url, params=params, timeout=12)
        tab = (data or {}).get("candles")

        # MOEX ISS usually returns {"candles": {"columns":[...], "data":[...]}}
        if isinstance(tab, dict):
            cols = [str(c).lower() for c in (tab.get("columns") or [])]
            rows = tab.get("data") or []
        # Some gateways may wrap differently, try list[0]
        elif isinstance(tab, list) and tab and isinstance(tab[0], dict):
            cols = [str(c).lower() for c in (tab[0].get("columns") or [])]
            rows = tab[0].get("data") or []
        else:
            return []

        try:
            i_close = cols.index("close")
        except ValueError:
            return []
        i_time = None
        for cand in ("end", "begin", "datetime", "time", "date"):
            if cand in cols:
                i_time = cols.index(cand)
                break
        if i_time is None:
            return []

        out: List[Tuple[datetime, float]] = []
        for row in rows:
            try:
                ts = row[i_time]
                close = row[i_close]
                if close is None:
                    continue
                # Parse ISO timestamp
                if isinstance(ts, str):
                    # Handle 'YYYY-MM-DDTHH:MM:SS' or with 'Z'
                    ts_s = ts.replace("Z", "+00:00")
                    try:
                        dt = datetime.fromisoformat(ts_s)
                    except Exception:
                        dt = datetime.strptime(ts.split("T")[0], "%Y-%m-%d")
                else:
                    # Fallback: treat as epoch seconds
                    dt = datetime.utcfromtimestamp(float(ts))
                out.append((dt, float(close)))
            except Exception:
                continue

        out.sort(key=lambda t: t[0])
        # Keep only the requested window
        if days and len(out) > days:
            out = out[-days:]
        return out
    except Exception as e:
        logger.warning("MOEX candles failed for %s: %s", secid, e)
        return []

# Parallelized MOEX per-ticker computation
def _moex_last_two(secid: str) -> Optional[Tuple[float, float]]:
    series = _moex_candles(secid, 10)
    if len(series) < 2:
        return None
    prev_close = series[-2][1]
    last_close = series[-1][1]
    if not isinstance(prev_close, (int, float)) or not isinstance(last_close, (int, float)) or prev_close == 0:
        return None
    return float(prev_close), float(last_close)

def fetch_moex_prices(symbols: List[str], vs: str) -> Dict[str, Dict[str, float]]:
    """
    Return last close and daily % change computed from candles for RU tickers.
    Price is converted from RUB to 'vs' (USD/EUR/RUB).
    """
    vs_u = (vs or "RUB").upper()
    rub_rate = 1.0 if vs_u == "RUB" else (fx_rate("RUB", vs_u) or 0.0)
    out: Dict[str, Dict[str, float]] = {}
    if not symbols:
        return out
    futures = []
    for sym in symbols:
        secid = _moex_secid(sym)
        futures.append(_IO_POOL.submit(_moex_last_two, secid))
    for sym, fut in zip(symbols, futures):
        res = fut.result()
        if not res:
            continue
        prev_close, last_close = res
        chg = (last_close - prev_close) / prev_close
        if rub_rate == 0 and vs_u != "RUB":
            rate_try = fx_rate("RUB", vs_u)
            if not isinstance(rate_try, (int, float)) or rate_try <= 0:
                logger.warning("MOEX: FX RUB->%s unavailable for %s", vs_u, sym)
                continue
            rub_rate = rate_try
        price_vs = last_close if vs_u == "RUB" else last_close * rub_rate
        out[sym.upper()] = {"price": float(price_vs), "chg": float(chg)}
    return out

def moex_history(symbol: str, base: str, days: int) -> List[Tuple[datetime, float]]:
    """
    History for RU ticker, converted to 'base' currency.
    """
    secid = _moex_secid(symbol)
    series = _moex_candles(secid, days)
    if not series:
        return []
    if (base or "").lower() == "rub":
        return series
    rate = fx_rate("RUB", (base or "RUB").upper())
    if not isinstance(rate, (int, float)) or rate <= 0:
        return []
    return [(dt, v * float(rate)) for dt, v in series]

# ========== CoinGecko provider (crypto) ==========
CG_ID_MAP_STATIC: Dict[str, str] = {
    "BTC": "bitcoin",
    "ETH": "ethereum",
    "TON": "toncoin",  # important: ensure Toncoin, not Tokamak Network
    "USDT": "tether",
    "USDC": "usd-coin",
    "BNB": "binancecoin",
    "SOL": "solana",
    "TRX": "tron",
    "DOGE": "dogecoin",
    "XRP": "ripple",
    "ADA": "cardano",
    "DOT": "polkadot",
    "AVAX": "avalanche-2",
    "MATIC": "polygon-pos",
    "LINK": "chainlink",
    "XLM": "stellar",
    "ATOM": "cosmos",
    "NEAR": "near",
    "APT": "aptos",
    "ARB": "arbitrum",
    "OP": "optimism",
    "TIA": "celestia",
    "SUI": "sui",
    "ICP": "internet-computer",
    "ETC": "ethereum-classic",
    "BCH": "bitcoin-cash",
    "LTC": "litecoin",
    "PEPE": "pepe",
    "SHIB": "shiba-inu",
    "WBTC": "wrapped-bitcoin",
    "WETH": "weth",
}
_symbol_to_id_cache: Dict[str, str] = {}

def cg_headers() -> Dict[str, str]:
    h = {"Accept": "application/json"}
    if COINGECKO_API_KEY:
        h["x-cg-pro-api-key"] = COINGECKO_API_KEY
    return h

def is_crypto_symbol(sym: str) -> bool:
    s = (sym or "").upper()
    return s in CG_ID_MAP_STATIC or s in _symbol_to_id_cache or s in {"TON", "BTC", "ETH"}

def map_symbols_to_ids(symbols: List[str]) -> Dict[str, str]:
    out, unknown = {}, []
    for s in symbols:
        k = (s or "").upper()
        if not k:
            continue
        if k in CG_ID_MAP_STATIC:
            out[k] = CG_ID_MAP_STATIC[k]
        elif k in _symbol_to_id_cache:
            out[k] = _symbol_to_id_cache[k]
        else:
            unknown.append(k)
    if not unknown:
        return out
    data = http_get_json(f"{COINGECKO_BASE}/coins/list", headers=cg_headers(), timeout=20)
    sym_to_ids: Dict[str, List[str]] = {}
    if isinstance(data, list):
        for it in data:
            sym = (it.get("symbol") or "").upper()
            cid = it.get("id")
            if sym and cid:
                sym_to_ids.setdefault(sym, []).append(cid)
    for sym in unknown:
        cids = sym_to_ids.get(sym, []) or []
        preferred = []
        if sym == "TON":
            preferred = ["toncoin", "the-open-network"]
        chosen = None
        for pref in preferred:
            if pref in cids:
                chosen = pref
                break
        if chosen is None and cids:
            chosen = cids[0]
        if chosen:
            _symbol_to_id_cache[sym] = chosen
            out[sym] = chosen
    return out

def cg_fetch_prices(symbols: List[str], vs: str) -> Dict[str, Dict[str, float]]:
    vs = (vs or "usd").lower()
    ids_map = map_symbols_to_ids([s.upper() for s in symbols])
    if not ids_map:
        return {}
    ids_csv = ",".join(sorted(set(ids_map.values())))
    params = {"ids": ids_csv, "vs_currencies": vs, "include_24hr_change": "true"}
    data = http_get_json(f"{COINGECKO_BASE}/simple/price", params=params, headers=cg_headers(), timeout=15)
    out: Dict[str, Dict[str, float]] = {}
    if isinstance(data, dict):
        for sym, cid in ids_map.items():
            row = data.get(cid)
            if isinstance(row, dict) and vs in row:
                price = row.get(vs)
                chg = row.get(f"{vs}_24h_change")
                if isinstance(price, (int, float)):
                    out[sym] = {"price": float(price), "chg": float(chg) / 100.0 if isinstance(chg, (int, float)) else None}
    return out

# --- Helper: get 24h change via /coins/markets endpoint ---
def cg_change_24h(symbol: str, vs: str) -> Optional[float]:
    """
    Возвращает относительное изменение за 24ч (доля), кэшируется на _CG_CHG_TTL сек.
    """
    key = (symbol.upper(), (vs or "usd").lower())
    cached = _cg_chg_cache_get(key)
    if isinstance(cached, (int, float)):
        return float(cached)

    ids_map = map_symbols_to_ids([symbol.upper()])
    cid = ids_map.get(symbol.upper())
    if not cid:
        return None
    params = {"vs_currency": key[1], "ids": cid, "price_change_percentage": "24h"}
    data = http_get_json(
        f"{COINGECKO_BASE}/coins/markets",
        params=params,
        headers=cg_headers(),
        timeout=15,
    )
    try:
        if isinstance(data, list) and data:
            val = data[0].get("price_change_percentage_24h_in_currency")
            if isinstance(val, (int, float)):
                out = float(val) / 100.0
                _cg_chg_cache_set(key, out)
                return out
    except Exception:
        pass
    return None
def cg_history(symbol: str, vs: str, days: int) -> List[Tuple[datetime, float]]:
    vs = (vs or "usd").lower()
    ids_map = map_symbols_to_ids([symbol.upper()])
    cid = ids_map.get(symbol.upper())
    if not cid:
        return []
    params = {"vs_currency": vs, "days": str(days), "interval": "daily" if days > 1 else "hourly"}
    data = http_get_json(f"{COINGECKO_BASE}/coins/{cid}/market_chart", params=params, headers=cg_headers(), timeout=20)
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
        logger.warning("CG history failed for %s: %s", symbol, e)
        return []

# ========== TON direct provider (exchanges) ==========
def _ton_from_binance() -> Optional[Tuple[float, Optional[float]]]:
    data = http_get_json("https://api.binance.com/api/v3/ticker/24hr", params={"symbol": "TONUSDT"}, timeout=10)
    if isinstance(data, dict) and "lastPrice" in data:
        try:
            last = float(data["lastPrice"])
            chg = data.get("priceChangePercent")
            chg = float(chg) / 100.0 if chg is not None else None
            return last, chg
        except Exception:
            return None
    return None

def _ton_from_okx() -> Optional[Tuple[float, Optional[float]]]:
    data = http_get_json("https://www.okx.com/api/v5/market/ticker", params={"instId": "TON-USDT"}, timeout=10)
    try:
        arr = (data or {}).get("data") or []
        if arr:
            d = arr[0]
            last = float(d.get("last"))
            open24h = d.get("open24h")
            chg = None
            if open24h is not None:
                open24h = float(open24h)
                if open24h > 0:
                    chg = (last - open24h) / open24h
            return last, chg
    except Exception:
        return None
    return None

def _ton_from_kucoin() -> Optional[Tuple[float, Optional[float]]]:
    data = http_get_json("https://api.kucoin.com/api/v1/market/stats", params={"symbol": "TON-USDT"}, timeout=10)
    try:
        d = (data or {}).get("data") or {}
        last = float(d.get("last"))
        chg = d.get("changeRate")
        chg = float(chg) if chg is not None else None
        return last, chg
    except Exception:
        return None

def ton_price_direct(vs_l: str) -> Optional[Dict[str, float]]:
    """
    Прямая цена TON с бирж (USDT) с 24h %, без CoinGecko.
    Возвращает словарь формата {"price": <в базовой валюте>, "chg": <доля>} или None.
    """
    def _wrap(fn):
        try:
            return fn()
        except Exception:
            return None
    futures = [_IO_POOL.submit(_wrap, fn) for fn in (_ton_from_binance, _ton_from_okx, _ton_from_kucoin)]
    for fut in as_completed(futures):
        res = fut.result()
        if res:
            last_usd, chg = res
            if vs_l == "usd":
                price = last_usd
            else:
                rate = fx_rate("USD", (vs_l or "usd").upper())
                if not isinstance(rate, (int, float)) or rate <= 0:
                    continue
                price = last_usd * rate
            return {"price": float(price), "chg": chg}
    return None

# ========== TON history from exchanges (for charts) ==========
def _ton_history_binance(days: int) -> List[Tuple[datetime, float]]:
    """
    Binance klines: prefer 1h for <=2 days, else 1d. Returns list of (UTC datetime, close in USDT≈USD).
    """
    try:
        interval = "1h" if days <= 2 else "1d"
        limit = 24 if days <= 2 else max(2, min(days, 500))
        data = http_get_json(
            "https://api.binance.com/api/v3/klines",
            params={"symbol": "TONUSDT", "interval": interval, "limit": limit},
            timeout=10,
        )
        out: List[Tuple[datetime, float]] = []
        if isinstance(data, list):
            for row in data:
                # [ openTime, open, high, low, close, volume, closeTime, ... ]
                ts = row[0]
                close = row[4]
                try:
                    dt = datetime.utcfromtimestamp(float(ts) / 1000.0)
                    out.append((dt, float(close)))
                except Exception:
                    continue
        return out
    except Exception:
        return []

def _ton_history_okx(days: int) -> List[Tuple[datetime, float]]:
    """
    OKX candles: prefer 1H for <=2 days, else 1D. Returns list of (UTC datetime, close in USDT≈USD).
    """
    try:
        bar = "1H" if days <= 2 else "1D"
        limit = 24 if days <= 2 else max(2, min(days, 300))
        data = http_get_json(
            "https://www.okx.com/api/v5/market/candles",
            params={"instId": "TON-USDT", "bar": bar, "limit": str(limit)},
            timeout=10,
        )
        out: List[Tuple[datetime, float]] = []
        arr = (data or {}).get("data") or []
        # OKX возвращает от новых к старым — развернём
        for row in reversed(arr):
            # [ ts, o, h, l, c, vol, ... ], ts в мс
            ts = row[0]
            close = row[4]
            try:
                dt = datetime.utcfromtimestamp(float(ts) / 1000.0)
                out.append((dt, float(close)))
            except Exception:
                continue
        return out
    except Exception:
        return []

def _ton_history_kucoin(days: int) -> List[Tuple[datetime, float]]:
    """
    KuCoin candles: prefer 1hour for <=2 days, else 1day. Returns list of (UTC datetime, close in USDT≈USD).
    """
    try:
        ktype = "1hour" if days <= 2 else "1day"
        data = http_get_json(
            "https://api.kucoin.com/api/v1/market/candles",
            params={"symbol": "TON-USDT", "type": ktype},
            timeout=10,
        )
        out: List[Tuple[datetime, float]] = []
        arr = (data or {}).get("data") or []
        # KuCoin может возвращать от новых к старым — развернём
        for row in reversed(arr):
            # формат: [time, open, close, high, low, volume, turnover]
            ts = row[0]
            close = row[2]
            try:
                # KuCoin time в секундах
                ts_f = float(ts)
                dt = datetime.utcfromtimestamp(ts_f if ts_f > 2_000_000_000 else ts_f)
                out.append((dt, float(close)))
            except Exception:
                continue
        # ограничим по дням/часам приблизительно
        if days > 2 and len(out) > days:
            out = out[-days:]
        if days <= 2 and len(out) > 24:
            out = out[-24:]
        return out
    except Exception:
        return []

def ton_history_direct(vs_l: str, days: int) -> List[Tuple[datetime, float]]:
    """
    История TON в выбранной базе: собираем с бирж (USDT≈USD) и при необходимости конвертируем.
    """
    series_usd: List[Tuple[datetime, float]] = []
    for fn in (_ton_history_binance, _ton_history_okx, _ton_history_kucoin):
        series_usd = fn(days)
        if series_usd:
            break
    if not series_usd:
        return []
    if vs_l == "usd":
        return series_usd
    rate = fx_rate("USD", (vs_l or "usd").upper())
    if not isinstance(rate, (int, float)) or rate <= 0:
        return []
    return [(dt, v * rate) for dt, v in series_usd]

# ========== AI Assistant ==========
def _strip_code_fences(s: str) -> str:
    if not isinstance(s, str):
        return ""
    s = s.strip()
    if s.startswith("```") and s.endswith("```"):
        s = s[3:-3].strip()
        if s.lower().startswith("json"):
            s = s[4:].strip()
    return s

def _openai_chat(messages: List[Dict], temperature: float = 0.2, max_tokens: int = 700) -> Optional[str]:
    if not OPENAI_API_KEY:
        logger.warning("OPENAI_API_KEY is not set")
        return None

    # breaker: если недавно был таймаут — быстро выходим
    if time.time() < globals().get('_AI_DOWN_UNTIL', 0.0):
        return None

    url = f"{OPENAI_BASE.rstrip('/')}/chat/completions"
    headers = {
        "Authorization": f"Bearer {OPENAI_API_KEY}",
        "Content-Type": "application/json",
    }
    if OPENAI_ORG:
        headers["OpenAI-Organization"] = OPENAI_ORG
    if OPENAI_PROJECT:
        headers["OpenAI-Project"] = OPENAI_PROJECT

    timeout_s = float(os.getenv("OPENAI_TIMEOUT", "35"))
    retries = max(1, int(os.getenv("OPENAI_RETRIES", "2")))

    def _do_call(model_name: str):
        body = {
            "model": model_name,
            "messages": messages,
            "temperature": float(temperature),
            "max_tokens": int(max_tokens),
        }
        last_err = None
        for i in range(retries):
            try:
                r = _SESSION.post(url, json=body, headers=headers, timeout=timeout_s)
                if r.status_code >= 400:
                    try:
                        err = r.json()
                    except Exception:
                        err = {"text": (r.text or "")[:500]}
                    logger.warning("OpenAI %s for %s: %s", r.status_code, model_name, err)
                    last_err = Exception(str(err))
                else:
                    data = r.json()
                    return (data or {}).get("choices", [{}])[0].get("message", {}).get("content")
            except requests.exceptions.ReadTimeout as e:
                last_err = e
            except Exception as e:
                last_err = e
            time.sleep(0.3 * (i + 1))
        # после провала ставим breaker, чтобы не долбить API
        globals()["_AI_DOWN_UNTIL"] = time.time() + float(os.getenv("AI_BREAKER_SEC", "45"))
        logger.warning("OpenAI request failed for %s after %d tries: %s", model_name, retries, last_err)
        return None

    out = _do_call(OPENAI_MODEL)
    if out is not None:
        return out

    if OPENAI_ALLOW_FALLBACK and OPENAI_MODEL != "gpt-4o-mini":
        logger.info("Trying fallback model gpt-4o-mini")
        return _do_call("gpt-4o-mini")

    return None
def ai_route_and_reply(user: Dict, history: List[Dict], text: str) -> Tuple[str, Optional[Dict]]:
    """
    Returns (reply_text, route_dict_or_None).
    route_dict schema:
      {
        "action": "answer"|"price"|"chart"|"convert"|"add"|"remove"|"setbase",
        "args": {...},
        "reply": "natural language answer if action=answer or brief confirmation"
      }
    """
    base = (user.get("base") or DEFAULT_BASE).upper()
    user_tickers = ", ".join(user.get("tickers", [])) or "(пусто)"
    sys = (
        "Ты — ассистент роутер внутри финансового Telegram-бота. "
        "Отвечай строго JSON-объектом БЕЗ лишнего текста. Допустимые поля: action, args, reply.\n"
        "action одно из: 'answer', 'price', 'chart', 'convert', 'add', 'remove', 'setbase', 'analyze'.\n"
        "Правила маршрутизации:\n"
        "• Если у пользователя прямой вопрос, который не требует действия (объяснить термин, дать справку) — action='answer' и краткий текст в reply.\n"
        "• 'price' когда просят показать цены/котировки (args: {tickers:[\"AAPL\", \"BTC\"]} или пусто — тогда взять список пользователя).\n"
        "• 'chart' когда просят график (args: {ticker:\"BTC\", period:\"7d|30d|90d|1y\"}, period по умолчанию '7d').\n"
        "• 'convert' когда просят конвертацию валют (args: {amount:число, frm:\"USD\", to:\"RUB\"}).\n"
        "• 'add'/'remove' когда просят добавить/удалить тикеры (args: {tickers:[...]}).\n"
        "• 'setbase' когда просят сменить базовую валюту (args: {base:\"USD|EUR|RUB|GBP|JPY|CHF|CNY|AUD|CAD|TRY\"}).\n"
        "• 'analyze' когда просят обзор/анализ рынка (args: {tickers:[...], horizon:\"7d|30d|90d|1y\"}).\n"
        "Никаких инвестиционных советов. Краткость отвечает. Только валюта в кодах (USD, EUR и т.д.)."
    )
    # Сжимаем историю до последних 6 сообщений (3 пары)
    hist = history[-6:] if history else []
    msgs = [{"role": "system", "content": sys}]
    msgs.extend(hist)
    msgs.append({
        "role": "user",
        "content": (
            f"user_base={base}; known_tickers=[{user_tickers}]\n"
            "Натуральный язык: " + (text or "").strip()
        ),
    })
    raw = _openai_chat(msgs) or ""
    if not raw:
        if not OPENAI_API_KEY:
            return ("Чтобы включить помощника, задай переменную окружения OPENAI_API_KEY.", None)
        return ("Не удалось получить ответ от модели. Попробуйте позже.", None)
    try:
        body = _strip_code_fences(raw)
        obj = json.loads(body)
        action = (obj.get("action") or "answer").lower()
        args = obj.get("args") or {}
        reply = (obj.get("reply") or "").strip()
        return (reply or "Ок.", {"action": action, "args": args})
    except Exception:
        # Если модель вернула не-JSON, просто ответим текстом
        return (raw.strip(), None)

# ========== UI helpers ==========
# Supported base currencies (10)
SUPPORTED_BASES = [
    "USD", "EUR", "RUB", "GBP", "JPY", "CHF", "CNY", "AUD", "CAD", "TRY",
]

def ccy_symbol(code: str) -> str:
    c = (code or "").upper()
    symbols = {
        "USD": "$",   # US Dollar
        "EUR": "€",   # Euro
        "RUB": "₽",   # Russian Ruble
        "GBP": "£",   # British Pound
        "JPY": "¥",   # Japanese Yen
        "CHF": "Fr",  # Swiss Franc
        "CNY": "¥",   # Chinese Yuan (символ общий с JPY)
        "AUD": "A$",  # Australian Dollar
        "CAD": "C$",  # Canadian Dollar
        "TRY": "₺",   # Turkish Lira
    }
    return symbols.get(c, c)

def format_amount(x: float, decimals: int = 2) -> str:
    try:
        return f"{x:,.{decimals}f}".replace(",", " ")
    except Exception:
        return str(x)

def fmt_with_symbol(amount: float, code: str, decimals: int = 2) -> str:
    return f"{ccy_symbol(code)}{format_amount(amount, decimals)}"

def main_menu_markup(user: Dict) -> InlineKeyboardMarkup:
    base = (user.get("base") or DEFAULT_BASE).upper()
    rows = [
        [InlineKeyboardButton("📊 Цены", callback_data="ACT:PRICE"),
         InlineKeyboardButton("🧾 Список", callback_data="ACT:LIST")],
        [InlineKeyboardButton("➕ Добавить", callback_data="ACT:ADD"),
         InlineKeyboardButton("➖ Удалить", callback_data="ACT:REMOVE")],
        [InlineKeyboardButton("📈 График", callback_data="ACT:CHART"),
         InlineKeyboardButton(f"💱 Валюта: {ccy_symbol(base)}", callback_data="ACT:BASE")],
        [InlineKeyboardButton("🔁 Конвертер", callback_data="ACT:CONVERT")],
    ]
    rows.append([InlineKeyboardButton("🤖 Помощник", callback_data="ACT:ASSIST")])
    rows.append([InlineKeyboardButton("🧠 Аналитик", callback_data="ACT:ANALYST")])
    if (user.get("tickers") or []):
        rows.append([InlineKeyboardButton("🧹 Очистить", callback_data="ACT:CLEAR")])
    return InlineKeyboardMarkup(rows)

def cancel_markup() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("◀️ Назад", callback_data="ACT:BACK")]])

def base_menu_markup(user: Dict) -> InlineKeyboardMarkup:
    base = (user.get("base") or DEFAULT_BASE).upper()
    buttons = []
    row = []
    for code in SUPPORTED_BASES:
        label = ("✅ " if base == code else "") + code
        row.append(InlineKeyboardButton(label, callback_data=f"BASE:{code}"))
        if len(row) == 5:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)
    buttons.append([InlineKeyboardButton("◀️ Назад", callback_data="ACT:BACK")])
    return InlineKeyboardMarkup(buttons)

# ========== Converter flow button menus ==========
def convert_from_markup() -> InlineKeyboardMarkup:
    buttons, row = [], []
    for code in SUPPORTED_BASES:
        row.append(InlineKeyboardButton(code, callback_data=f"CONV:FROM:{code}"))
        if len(row) == 5:
            buttons.append(row); row = []
    if row:
        buttons.append(row)
    buttons.append([InlineKeyboardButton("◀️ Назад", callback_data="ACT:BACK")])
    return InlineKeyboardMarkup(buttons)

def convert_to_markup(from_ccy: str) -> InlineKeyboardMarkup:
    f = (from_ccy or "").upper()
    def label(code: str) -> str:
        return ("✅ " if code == f else "") + code
    buttons, row = [], []
    for code in SUPPORTED_BASES:
        row.append(InlineKeyboardButton(label(code), callback_data=f"CONV:TO:{code}"))
        if len(row) == 5:
            buttons.append(row); row = []
    if row:
        buttons.append(row)
    buttons.append([InlineKeyboardButton("◀️ Назад", callback_data="ACT:BACK")])
    return InlineKeyboardMarkup(buttons)

def convert_continue_markup() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔁 Продолжить", callback_data="CONV:AGAIN")],
        [InlineKeyboardButton("♻️ Сменить валюты", callback_data="ACT:CONVERT")],
        [InlineKeyboardButton("◀️ Назад", callback_data="ACT:BACK")],
    ])
def _fmt_pct(v: Optional[float]) -> Optional[str]:
    if isinstance(v, (int, float)):
        try:
            return f"{float(v):+.2%}"
        except Exception:
            return None
    return None

def _fmt_price(v: Optional[float], base: str) -> Optional[str]:
    if isinstance(v, (int, float)):
        return fmt_with_symbol(float(v), base, 2)
    return None

def _fmt_ticker_line(sym: str, row: Dict[str, Optional[float]], base: str) -> str:
    parts = []
    p = _fmt_price(row.get("price"), base)
    if p:
        parts.append(p)
    c24 = _fmt_pct(row.get("chg24h"))
    if c24:
        parts.append(f"24h {c24}")
    for key, label in (("chg7d", "7d"), ("chg30d", "30d"), ("chg90d", "90d"), ("chg365d", "1y")):
        val = _fmt_pct(row.get(key))
        if val:
            parts.append(f"{label} {val}")
    return f"• {sym}: " + " · ".join(parts) if parts else f"• {sym}: —"

def _format_snapshot_block(snapshot: Dict[str, Dict[str, Optional[float]]], base: str, horizon: str) -> str:
    base = (base or DEFAULT_BASE).upper()
    lines = ["Тикеры:"]
    for sym in sorted(snapshot.keys()):
        row = snapshot.get(sym) or {}
        lines.append(_fmt_ticker_line(sym, row, base))
    return "\n".join(lines)

# ========== Analyst UI & helpers ==========
@asynccontextmanager
async def typing_indicator(bot, chat_id: int, interval: float = 4.0):
    """Показывает 'печатает…' сразу и затем каждые interval секунд, пока идёт работа."""
    stop_event = asyncio.Event()

    async def _loop():
        while not stop_event.is_set():
            try:
                await bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
            except Exception:
                pass
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=interval)
            except asyncio.TimeoutError:
                pass

        # Мгновенно показать индикатор перед запуском фонового цикла
    try:
        await bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
    except Exception:
        pass

    task = asyncio.create_task(_loop())
    try:
        yield
    finally:
        stop_event.set()
        task.cancel()
def analyst_menu(h: str) -> InlineKeyboardMarkup:
    # simplified: only Back button for analyst mode
    return InlineKeyboardMarkup([[InlineKeyboardButton("◀️ Назад", callback_data="ACT:BACK")]])

def _pct_change(a: float, b: float) -> Optional[float]:
    try:
        if a and b and a > 0:
            return (b - a) / a
    except Exception:
        pass
    return None

def change_over_period(symbol: str, base: str, days: int) -> Optional[float]:
    series = history_for_chart(symbol, base, days)
    if series and len(series) >= 2:
        return _pct_change(series[0][1], series[-1][1])
    return None

async def build_snapshot(tickers: List[str], base: str, horizons: List[int]) -> Dict[str, Dict[str, Optional[float]]]:
    prices = await fetch_prices(tickers, base.lower())
    snap: Dict[str, Dict[str, Optional[float]]] = {}

    # Initialize per-ticker metrics and prepare concurrent tasks for horizon changes
    tasks = []
    keys: List[Tuple[str, int]] = []
    for t in tickers:
        row = prices.get(t) or {}
        snap[t] = {
            "price": row.get("price"),
            "chg24h": row.get("chg"),
        }
        for d in horizons:
            d_i = int(d)
            tasks.append(asyncio.to_thread(change_over_period, t, base, d_i))
            keys.append((t, d_i))

    if tasks:
        results = await asyncio.gather(*tasks, return_exceptions=True)
        for (t, d), res in zip(keys, results):
            val = res if isinstance(res, (int, float)) else None
            if val is not None:
                snap.setdefault(t, {})[f"chg{d}d"] = float(val)

    return snap

def ai_generate_insights(user: Dict, base: str, snapshot: Dict[str, Dict[str, Optional[float]]], horizon: str, question: Optional[str] = None) -> str:
    """Просим модель выдать только краткие ИНСАЙТЫ (маркеры), без пересказа чисел и без разделов."""
    context_obj = {
        "base": base.upper(),
        "horizon": horizon,
        "tickers": list(snapshot.keys()),  # только список тикеров, без числовых полей
        "question": (question or "").strip(),
    }
    sys = (
        "Ты — рыночно-аналитический ассистент Telegram-бота. Пиши предельно кратко, по делу. "
        "Вывод ТОЛЬКО списком маркеров (каждый с '•'), без заголовков и прелюдий. "
        "Не повторяй цены и проценты — они уже в блоке тикеров. "
        "Дай 3–6 осмысленных пунктов: драйверы/риски/наблюдения, относящиеся к вопросу (если он есть). "
        "Запрещено давать персональные советы и приказы 'покупай/продавай'."
    )
    task = "Ответь именно на вопрос пользователя, исходя из его тикеров и горизонта. Только маркеры." if context_obj["question"] else "Сделай общий обзор по тикерам и горизонту. Только маркеры."
    usr = "Дано в JSON:\n" + json.dumps(context_obj, ensure_ascii=False) + "\n" + task
    out = _openai_chat(
        [{"role": "system", "content": sys}, {"role": "user", "content": usr}],
        temperature=0.3,
        max_tokens=400,
    )
    if not out:
        return "• Данных от модели нет (повторите запрос позже)."
    lines = [ln.strip() for ln in out.strip().splitlines() if ln.strip()]
    lines = [ln if ln.startswith("•") else ("• " + ln) for ln in lines]
    return "\n".join(lines[:6])

def ai_generate_analysis(user: Dict, base: str, snapshot: Dict[str, Dict[str, Optional[float]]], horizon: str, question: Optional[str] = None) -> str:
    insights = ai_generate_insights(user, base, snapshot, horizon, question)
    tickers_block = _format_snapshot_block(snapshot, base, horizon)
    header = f"🧠 Аналитика (база: {base.upper()} · горизонт: {horizon})"
    if question and question.strip():
        header += f"\n❓ Вопрос: {question.strip()}"
    return f"{header}\n\n{tickers_block}\n\n{insights}\n\n⚠️ Не является инвестиционной рекомендацией."
async def run_analyst_reply(message_obj, context: ContextTypes.DEFAULT_TYPE, user: Dict, tickers: Optional[List[str]] = None, horizon: str = "30d", question: Optional[str] = None):
    h = (horizon or "30d").lower()
    base = (user.get("base") or DEFAULT_BASE).upper()
    tlist = [normalize_ticker(t) for t in (tickers or user.get("tickers") or []) if normalize_ticker(t)]
    if not tlist:
        tlist = ["BTC", "ETH", "AAPL", "NVDA", "TON"]
    horizons = {7, 30, 90, 365}
    try:
        if h.endswith("d") and h[:-1].isdigit():
            horizons.add(int(h[:-1]))
        elif h in {"1y", "12m"}:
            horizons.add(365)
    except Exception:
        pass
        # Показать индикатор сразу, до плейсхолдера
    try:
        await context.bot.send_chat_action(chat_id=message_obj.chat_id, action=ChatAction.TYPING)
    except Exception:
        pass

    # Плейсхолдер + индикатор набора (видно, что ИИ работает)
    placeholder = await message_obj.reply_text("🧠 Аналитик думает…", reply_markup=cancel_markup())
    chat_id = message_obj.chat_id
    async with typing_indicator(context.bot, chat_id):
        snap = await build_snapshot(tlist, base, sorted(list(horizons)))
        text = await asyncio.to_thread(ai_generate_analysis, user, base, snap, h, question)
    try:
        await placeholder.edit_text(text, reply_markup=cancel_markup())
    except Exception:
        await message_obj.reply_text(text, reply_markup=cancel_markup())
    if time.time() < globals().get("_AI_DOWN_UNTIL", 0.0):
        try:
            await placeholder.edit_text("🧠 Аналитик временно недоступен. Попробуйте позже.", reply_markup=cancel_markup())
        except Exception:
            await message_obj.reply_text("🧠 Аналитик временно недоступен. Попробуйте позже.", reply_markup=cancel_markup())
    return
# ========== Converter helpers ==========
_CCY_ALIASES = {
    # Symbols
    "$": "USD", "€": "EUR", "₽": "RUB", "£": "GBP", "¥": "JPY", "₺": "TRY",
    "a$": "AUD", "c$": "CAD", "fr": "CHF",
    # Codes (case-insensitive)
    "usd": "USD", "eur": "EUR", "rub": "RUB", "gbp": "GBP", "jpy": "JPY",
    "chf": "CHF", "cny": "CNY", "aud": "AUD", "cad": "CAD", "try": "TRY",
}

def _norm_ccy(s: str) -> Optional[str]:
    if not s:
        return None
    k = s.strip().replace(".", "").replace(",", "").lower()
    return (_CCY_ALIASES.get(k) or k.upper()) if len(k) <= 4 else None

def _parse_amount(s: str) -> Optional[float]:
    if not s:
        return None
    try:
        s = s.replace(" ", "").replace(",", ".")
        return float(s)
    except Exception:
        return None

def _parse_convert(text: str, default_from: str) -> Optional[Tuple[float, str, str]]:
    """
    Поддерживаемые варианты:
      '100 usd rub', '100 usd->rub', '100 usd to rub', '100 rub usd'
      '100 rub' — тогда из default_from в RUB.
    """
    t = (text or "").strip()
    if not t:
        return None
    t = re.sub(r"\s*(->|to|в)\s*", " ", t, flags=re.IGNORECASE)
    parts = [p for p in re.split(r"\s+", t) if p]
    if len(parts) == 3:
        amt = _parse_amount(parts[0]); frm = _norm_ccy(parts[1]); to = _norm_ccy(parts[2])
        if amt is not None and frm and to:
            return amt, frm, to
        return None
    if len(parts) == 2:
        amt = _parse_amount(parts[0]); to = _norm_ccy(parts[1]); frm = _norm_ccy(default_from)
        if amt is not None and to and frm:
            return amt, frm, to
    return None

def _do_convert(amount: float, frm: str, to: str) -> Optional[Tuple[float, float]]:
    """
    Возвращает (converted_amount, rate), где rate — множитель FRM->TO.
    """
    if not frm or not to:
        return None
    frm_u, to_u = frm.upper(), to.upper()
    if frm_u == to_u:
        return amount, 1.0
    r = fx_rate(frm_u, to_u)
    if not isinstance(r, (int, float)) or r <= 0:
        return None
    return amount * float(r), float(r)

# ========== Storage ==========
def _json_loads(s: str):
    if ujson_mod:
        return ujson_mod.loads(s)
    return json.loads(s)

def _json_dumps(o) -> str:
    if ujson_mod:
        return ujson_mod.dumps(o, ensure_ascii=False, indent=2)
    return json.dumps(o, ensure_ascii=False, indent=2)

def load_db() -> Dict[str, Dict]:
    if not DATA_PATH.exists():
        return {}
    try:
        return _json_loads(DATA_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}

def save_db(db: Dict[str, Dict]):
    tmp_fd, tmp_path = tempfile.mkstemp(prefix="data.", suffix=".json")
    with os.fdopen(tmp_fd, "w", encoding="utf-8") as f:
        f.write(_json_dumps(db))
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp_path, DATA_PATH)

db: Dict[str, Dict] = load_db()

def get_user(db_ref: Dict[str, Dict], uid: int) -> Dict:
    return db_ref.get(str(uid), {"tickers": [], "base": DEFAULT_BASE})

def set_user(db_ref: Dict[str, Dict], uid: int, user: Dict):
    db_ref[str(uid)] = user
    save_db(db_ref)

# ========== Logic ==========
def normalize_ticker(s: str) -> str:
    s = s.strip().upper()
    s = re.sub(r'^\$+', '', s)
    s = re.sub(r'[^A-Z0-9\.\-]', '', s)
    s = s.replace('.', '-')
    return s

def add_tickers(db_ref: Dict[str, Dict], uid: int, symbols: List[str]) -> List[str]:
    user = db_ref.get(str(uid), {"tickers": [], "base": DEFAULT_BASE})
    exist = set(normalize_ticker(t) for t in user.get("tickers", []))
    for s in symbols:
        s = normalize_ticker(s)
        if s:
            exist.add(s)
    user["tickers"] = sorted(exist)
    db_ref[str(uid)] = user
    save_db(db_ref)
    return user["tickers"]

def remove_tickers(db_ref: Dict[str, Dict], uid: int, symbols: List[str]) -> List[str]:
    user = db_ref.get(str(uid), {"tickers": [], "base": DEFAULT_BASE})
    current = set(user.get("tickers", []))
    to_remove = {normalize_ticker(s) for s in symbols if normalize_ticker(s)}
    removed = sorted(list(current & to_remove))
    current -= to_remove
    user["tickers"] = sorted(current)
    db_ref[str(uid)] = user
    save_db(db_ref)
    return removed

def clear_tickers(db_ref: Dict[str, Dict], uid: int) -> None:
    user = db_ref.get(str(uid), {"tickers": [], "base": DEFAULT_BASE})
    user["tickers"] = []
    db_ref[str(uid)] = user
    save_db(db_ref)

def _chg_from_history_sync(symbol: str, vs: str) -> Optional[float]:
    """
    Синхронно вычисляет относительное изменение цены за 24ч (долю) для symbol/vs.
    """
    # 1) Пытаемся взять готовый % через /coins/markets
    chg = cg_change_24h(symbol, vs)
    if isinstance(chg, (int, float)):
        return chg

    # 2) Фолбэк: считаем по истории /market_chart (days=1)
    series = cg_history(symbol, vs, 1)
    if not series or len(series) < 2:
        return None
    first = series[0][1]
    last = series[-1][1]
    if isinstance(first, (int, float)) and first > 0 and isinstance(last, (int, float)):
        return (last - first) / first
    return None

async def _fill_crypto_changes(result: Dict[str, Dict[str, float]], crypto: List[str], vs_l: str):
    tasks = []
    for sym in crypto:
        row = result.get(sym)
        if row and row.get("chg") is None:
            tasks.append(asyncio.to_thread(_chg_from_history_sync, sym, vs_l))
    if not tasks:
        return
    changes = await asyncio.gather(*tasks, return_exceptions=True)
    idx = 0
    for sym in crypto:
        row = result.get(sym)
        if row and row.get("chg") is None:
            chg_val = changes[idx]
            idx += 1
            if isinstance(chg_val, (int, float)):
                row["chg"] = chg_val

# --- Ensure TON is present in result ---
def _ensure_ton(result: Dict[str, Dict[str, float]], vs_l: str):
    """
    Гарантирует наличие TON в результате:
    1) Если цену уже получили — досчитывает % при необходимости.
    2) Иначе делает прямой запрос к /simple/price для toncoin.
    3) Последний фолбэк — берёт последнюю точку из истории (days=1) и считает %.
    """
    # 1) Если TON уже есть — дополним chg при необходимости
    row = result.get("TON")
    if isinstance(row, dict) and isinstance(row.get("price"), (int, float)):
        if row.get("chg") is None:
            chg_val = cg_change_24h("TON", vs_l) or _chg_from_history_sync("TON", vs_l)
            if isinstance(chg_val, (int, float)):
                row["chg"] = chg_val
        return

    # 2) Прямой запрос к CoinGecko simple/price по id 'toncoin'
    try:
        data = http_get_json(
            f"{COINGECKO_BASE}/simple/price",
            params={"ids": "toncoin", "vs_currencies": vs_l, "include_24hr_change": "true"},
            headers=cg_headers(),
            timeout=15,
        )
        if isinstance(data, dict):
            r = data.get("toncoin")
            if isinstance(r, dict) and vs_l in r:
                price = r.get(vs_l)
                chg = r.get(f"{vs_l}_24h_change")
                if isinstance(price, (int, float)):
                    result["TON"] = {
                        "price": float(price),
                        "chg": float(chg) / 100.0 if isinstance(chg, (int, float)) else None,
                    }
                    # Если процента нет — посчитаем по истории
                    if result["TON"]["chg"] is None:
                        chg_val = cg_change_24h("TON", vs_l) or _chg_from_history_sync("TON", vs_l)
                        if isinstance(chg_val, (int, float)):
                            result["TON"]["chg"] = chg_val
                    return
    except Exception as e:
        logger.warning("TON direct fetch failed: %s", e)

    # 3) Фолбэк: возьмём цену из последней точки истории и посчитаем % изменения
    series = cg_history("TON", vs_l, 1)
    if series:
        last = series[-1][1]
        if isinstance(last, (int, float)):
            chg_val = _chg_from_history_sync("TON", vs_l)
            result["TON"] = {"price": float(last), "chg": chg_val}

async def fetch_prices(symbols: List[str], vs: str) -> Dict[str, Dict[str, float]]:
    symbols_norm = [normalize_ticker(s) for s in symbols if normalize_ticker(s)]
    if not symbols_norm:
        return {}
    vs_l = (vs or "usd").lower()
    vs_u = vs_l.upper()
    # Short-lived aggregation cache (avoid repeated identical requests for a few seconds)
    cache_key = (tuple(sorted(symbols_norm)), vs_l)
    cached = _PRICE_CACHE.get(cache_key)
    now = time.time()
    if cached and (now - cached[0]) < _PRICE_TTL:
        return dict(cached[1])
    result: Dict[str, Dict[str, float]] = {}
    crypto = [s for s in symbols_norm if is_crypto_symbol(s)]
    equity = [s for s in symbols_norm if s not in crypto]
    if crypto:
        try:
            cg = await asyncio.to_thread(cg_fetch_prices, crypto, vs_l)
            if isinstance(cg, dict):
                result.update(cg)
        except Exception as e:
            logger.error("Error fetching crypto prices from CG: %s", e)
        try:
            await _fill_crypto_changes(result, crypto, vs_l)
        except Exception as e:
            logger.warning("Failed to backfill crypto 24h change: %s", e)
        # TON: получаем напрямую с бирж (Binance → OKX → KuCoin), без CoinGecko
        if any(s.upper() == "TON" for s in crypto):
            ton_row = ton_price_direct(vs_l)
            if ton_row:
                result["TON"] = ton_row
            else:
                # крайний фолбэк — старый CG-механизм
                _ensure_ton(result, vs_l)
    if equity:
        equity_ru = [s for s in equity if _is_ru_ticker(s)]
        equity_us = [s for s in equity if s not in equity_ru]
        # RU via MOEX
        if equity_ru:
            try:
                ru = await asyncio.to_thread(fetch_moex_prices, equity_ru, vs_u)
                if isinstance(ru, dict):
                    result.update(ru)
            except Exception as e:
                logger.error("Error fetching RU stock prices (MOEX): %s", e)
        # US via Stooq
        if equity_us:
            try:
                stq = await asyncio.to_thread(fetch_stock_prices, equity_us, vs_u)
                if isinstance(stq, dict):
                    result.update(stq)
            except Exception as e:
                logger.error("Error fetching US stock prices (Stooq): %s", e)
    _PRICE_CACHE[cache_key] = (time.time(), dict(result))
    return result
# ========== Yahoo fallback (US stocks) ==========
def _yahoo_fetch_prices(symbols: List[str], vs: str) -> Dict[str, Dict[str, float]]:
    out: Dict[str, Dict[str, float]] = {}
    if not symbols:
        return out
    try:
        syms = ",".join([s.upper() for s in symbols])
        data = http_get_json(
            "https://query1.finance.yahoo.com/v7/finance/quote",
            params={"symbols": syms},
            timeout=10,
        )
        arr = ((data or {}).get("quoteResponse") or {}).get("result") or []
        if not arr:
            return out
        vs_u = (vs or "USD").upper()
        usd_to_vs = 1.0 if vs_u == "USD" else (fx_rate("USD", vs_u) or 0.0)
        if vs_u != "USD" and (not isinstance(usd_to_vs, (int, float)) or usd_to_vs <= 0):
            return out
        for q in arr:
            sym = (q.get("symbol") or "").upper()
            price = q.get("regularMarketPrice")
            chg_pct = q.get("regularMarketChangePercent")
            if isinstance(price, (int, float)):
                p_vs = float(price) if vs_u == "USD" else float(price) * float(usd_to_vs)
                out[sym] = {"price": p_vs, "chg": float(chg_pct)/100.0 if isinstance(chg_pct, (int, float)) else None}
        return out
    except Exception as e:
        logger.warning("Yahoo fallback failed: %s", e)
        return {}

# ========== Charts ==========
def parse_period(arg: str) -> int:
    s = (arg or "").lower().strip()
    if s.endswith("d") and s[:-1].isdigit(): return max(1, int(s[:-1]))
    if s in {"1y","12m"}: return 365
    if s in {"3m"}: return 90
    if s in {"6m"}: return 180
    return 7

def history_for_chart(ticker: str, base: str, days: int):
    """Return time series for ticker in base currency with TTL caching."""
    t = (ticker or "").upper()
    base_u = (base or DEFAULT_BASE).upper()
    key = (t, base_u, int(days or 7))

    cached = _hist_cache_get(key)
    if cached is not None:
        return cached

    series: List[Tuple[datetime, float]] = []

    # 0) Crypto first — избегаем Stooq для BTC/ETH/etc.
    if t == "TON":
        series = ton_history_direct(base.lower(), days)
    elif is_crypto_symbol(t):
        series = cg_history(t, base.lower(), days)

    # 1) RU акции — MOEX
    if not series and _is_ru_ticker(t):
        series = moex_history(t, base, days)

    # 2) US акции — Stooq (USD) с конвертацией
    if not series:
        stq = stooq_history(t, days)
        if stq and t not in CG_ID_MAP_STATIC:
            rate = fx_rate("USD", base_u)
            if isinstance(rate, (int, float)) and rate > 0:
                series = [(d, v * rate) for (d, v) in stq]
        elif stq:
            rate = fx_rate("USD", base_u)
            if isinstance(rate, (int, float)) and rate > 0:
                series = [(d, v * rate) for (d, v) in stq]

    _hist_cache_set(key, series or [])
    return series
def make_chart(series: List[Tuple[datetime, float]], ticker: str, base: str) -> bytes:
    if not series: return b""
    if len(series)==1:
        d0, v0 = series[0]; series = [(d0 - timedelta(hours=1), v0), (d0, v0)]
    dates = [d for d,_ in series]; values = [v for _,v in series]
    fig, ax = plt.subplots(figsize=(6,3))
    ax.plot(dates, values, linewidth=2)
    ax.set_title(f"{ticker} in {ccy_symbol(base)}"); ax.set_xlabel("Date"); ax.set_ylabel(ccy_symbol(base))
    ax.grid(True, linestyle="--", linewidth=0.5, alpha=0.5)
    ax.xaxis.set_major_locator(mdates.AutoDateLocator())
    ax.xaxis.set_major_formatter(mdates.ConciseDateFormatter(ax.xaxis.get_major_locator()))
    fig.tight_layout()
    buf = io.BytesIO(); fig.savefig(buf, format="png", dpi=160); plt.close(fig); buf.seek(0)
    return buf.read()

# ========== Handlers ==========
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = get_user(db, update.effective_user.id)
    text = (
        "Привет! Я — твой финансовый помощник 👋\n\n"
        "Что могу:\n"
        "• 📊 Цены — покажу текущие котировки и 24h изменение по твоим тикерам.\n"
        "• 📈 Графики — строю историю по тикеру на 7d/30d/90d/1y; для 1d — почасовые точки.\n"
        "• 🔁 Конвертер — быстро переведу сумму из одной валюты в другую; можно продолжать без повторного входа.\n"
        "• 🧾 Список — добавляй/удаляй тикеры, я запомню их за тобой.\n"
        "• 💱 Базовая валюта — выбери USD, EUR, RUB, GBP, JPY, CHF, CNY, AUD, CAD или TRY.\n"
        "• 🤖 AI Помощник — понимает свободный текст и сам запускает нужное действие.\n"
        "• 🧠 AI Аналитик — краткие инсайты по рынку и ответ на твой вопрос.\n\n"
        "Как пользоваться: нажимай кнопки ниже или просто нажми помощник и напиши, например:\n"
        "- цены aapl nvda btc\n"
        "- график TON 30d\n"
        "- конвертируй 250 eur в rub\n\n"
        "Важно: это не инвестиционная рекомендация.\n"
    )
    if update.message:
        await update.message.reply_text(text, reply_markup=main_menu_markup(user))

async def add_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if not args:
        await update.message.reply_text("Использование: /add AAPL NVDA BTC")
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
    prices = await fetch_prices(tickers, base)
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
        price_txt = fmt_with_symbol(p["price"], base, 2)
        lines.append(f"{k}: {price_txt}{chg_txt}")
    await update.message.reply_text("📊 Котировки:\n" + ("\n".join(lines) if lines else "Нет данных."))

async def setbase_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    codes = ", ".join(SUPPORTED_BASES)
    if not context.args:
        await update.message.reply_text(f"Использование: /setbase <валюта>\nДоступны: {codes}")
        return
    base_arg = (context.args[0] or "").upper()
    if base_arg not in SUPPORTED_BASES:
        await update.message.reply_text(f"Неподдерживаемая валюта. Доступны: {codes}")
        return
    user = get_user(db, update.effective_user.id)
    user["base"] = base_arg.lower()
    set_user(db, update.effective_user.id, user)
    await update.message.reply_text(f"Базовая валюта установлена: {base_arg}")

async def chart_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if not args:
        await update.message.reply_text("Использование: /chart TICKER [7d|30d|90d|1y]")
        return
    ticker = normalize_ticker(args[0])
    period_days = parse_period(args[1]) if len(args) > 1 else 7
    user = get_user(db, update.effective_user.id)
    base = (user.get("base") or DEFAULT_BASE).lower()
    series = await asyncio.to_thread(history_for_chart, ticker, base, period_days)
    if not series:
        await update.message.reply_text("Не удалось получить историю для графика.")
        return
    png = await asyncio.to_thread(make_chart, series, ticker, base)
    if not png:
        await update.message.reply_text("Не удалось построить график.")
        return
    await update.message.reply_photo(photo=png, caption=f"{ticker} · {period_days}d")
    # включим постоянный режим графиков
    context.user_data["mode"] = "chart"
    await update.message.reply_text(
        "Ещё график? Напишите `TICKER [7d|30d|90d|1y]` или нажмите Назад.",
        parse_mode="Markdown",
        reply_markup=cancel_markup(),
    )

async def convert_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Примеры:
      /convert 100 usd rub
      /convert 250 eur -> rub
      /convert 1000 rub usd
    Если указать только сумму и цель (например, '100 RUB'), исходная валюта — базовая у пользователя.
    """
    user = get_user(db, update.effective_user.id)
    default_from = (user.get("base") or DEFAULT_BASE).upper()
    args_text = " ".join(context.args or [])
    parsed = _parse_convert(args_text, default_from)
    if not parsed:
        await update.message.reply_text(
            "Использование: /convert 100 usd rub\n"
            "Также работает: `100 usd->rub`, `250 eur to rub`, `100 rub usd`.\n"
            f"Можно коротко: `100 RUB` — тогда из {default_from} в RUB.",
            parse_mode="Markdown",
        )
        return
    amount, frm, to = parsed
    res = _do_convert(amount, frm, to)
    if not res:
        await update.message.reply_text("Не удалось получить курс. Попробуй позже.")
        return
    converted, rate = res
    frm_s, to_s = ccy_symbol(frm), ccy_symbol(to)
    msg = (
        f"{format_amount(amount, 2)} {frm_s} ≈ {format_amount(converted, 2)} {to_s}\n"
        f"Курс: 1 {frm_s} = {format_amount(rate, 4)} {to_s}"
    )
    await update.message.reply_text(msg)

# ========== Callback handler ==========
async def on_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    data = q.data or ""
    uid = update.effective_user.id
    user = get_user(db, uid)

    if data == "ACT:ANALYST":
        context.user_data["mode"] = "analyst"
        context.user_data.setdefault("anl_h", "30d")
        await q.message.reply_text(
            "Режим аналитика активирован. Напишите вопрос (например: 'что с NVDA на 30d?', 'BTC и ставки ФРС?').",
            reply_markup=cancel_markup(),
        )
        return

    if data.startswith("ANL:H:"):
        h = data.split(":", 2)[2]
        context.user_data["anl_h"] = h
        await run_analyst_reply(q.message, context, user, horizon=h, question=None)
        return

    if data == "ANL:REFRESH":
        h = context.user_data.get("anl_h", "30d")
        await run_analyst_reply(q.message, context, user, horizon=h, question=None)
        return

    if data == "ACT:BACK":
        # Exit any active tool modes
        context.user_data.pop("mode", None)
        context.user_data.pop("conv_from", None)
        context.user_data.pop("conv_to", None)
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
        prices = await fetch_prices(tickers, base)
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
            price_txt = fmt_with_symbol(p["price"], base, 2)
            lines.append(f"{k}: {price_txt}{chg_txt}")
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
        base_code = data.split(":", 1)[1].upper()
        if base_code in SUPPORTED_BASES:
            user["base"] = base_code.lower()
            set_user(db, uid, user)
            await q.message.reply_text(f"Базовая валюта: {base_code}", reply_markup=main_menu_markup(user))
        else:
            await q.message.reply_text("Неподдерживаемая валюта. Доступны: " + ", ".join(SUPPORTED_BASES), reply_markup=main_menu_markup(user))
        return
    if data == "ACT:CHART":
        context.user_data["mode"] = "chart"
        await q.message.reply_text("Введи тикер и период (пример: `BTC 30d`). По умолчанию — 7d.", parse_mode="Markdown", reply_markup=cancel_markup())
        return
    if data == "ACT:CONVERT":
        # Start simple step-by-step converter: choose FROM -> choose TO -> enter amount
        context.user_data.pop("conv_from", None)
        context.user_data.pop("conv_to", None)
        context.user_data.pop("mode", None)
        await q.message.reply_text(
            "Выберите валюту ИЗ:",
            reply_markup=convert_from_markup(),
        )
        return

    if data == "ACT:ASSIST":
        context.user_data["mode"] = "assist"
        context.user_data.setdefault("assist_hist", [])
        await q.message.reply_text(
            "Задайте вопрос: про тикеры, графики, конвертацию или термин. Помощник может сам запустить нужное действие.",
            reply_markup=cancel_markup(),
        )
        return

    if data.startswith("CONV:FROM:"):
        frm = data.split(":", 2)[2].upper()
        context.user_data["conv_from"] = frm
        await q.message.reply_text(
            f"ИЗ: {frm}\nТеперь выберите валюту В:",
            reply_markup=convert_to_markup(frm),
        )
        return

    if data.startswith("CONV:TO:"):
        to = data.split(":", 2)[2].upper()
        frm = context.user_data.get("conv_from")
        if not frm:
            # If user somehow chose TO first, ask FROM again
            await q.message.reply_text("Сначала выберите валюту ИЗ:", reply_markup=convert_from_markup())
            return
        context.user_data["conv_to"] = to
        context.user_data["mode"] = "convert_amount"
        await q.message.reply_text(
            f"Сколько конвертируем из {frm} в {to}?\nВведите число, например: `100.5`",
            parse_mode="Markdown",
            reply_markup=cancel_markup(),
        )
        return

    if data == "CONV:AGAIN":
        frm = context.user_data.get("conv_from")
        to = context.user_data.get("conv_to")
        if not frm or not to:
            await q.message.reply_text("Сначала выберите валюты ИЗ/В:", reply_markup=convert_from_markup())
            return
        context.user_data["mode"] = "convert_amount"
        await q.message.reply_text(
            f"Сколько конвертируем из {frm} в {to}?\nВведите число, например: `100.5`",
            parse_mode="Markdown",
            reply_markup=cancel_markup(),
        )
        return

# ========== Text handler ==========
async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    user = get_user(db, uid)
    mode = str(context.user_data.get("mode", ""))

    # Надёжно достаём текст из сообщения
    msg = update.effective_message or update.message
    text = (getattr(msg, "text", None) or getattr(msg, "caption", None) or "").strip()

    if mode == "analyst":
        h = context.user_data.get("anl_h", "30d")
        await run_analyst_reply(update.message, context, user, horizon=h, question=text)
        return

    # ... остальная логика on_text ...
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
        series = await asyncio.to_thread(history_for_chart, ticker, base, period_days)
        if not series:
            await update.message.reply_text("Не удалось получить историю для графика.", reply_markup=main_menu_markup(user))
            return
        png = await asyncio.to_thread(make_chart, series, ticker, base)
        if not png:
            await update.message.reply_text("Не удалось построить график.", reply_markup=main_menu_markup(user))
            return
        # остаёмся в режиме графика — можно вводить следующий тикер и период
        await update.message.reply_photo(photo=png, caption=f"{ticker} · {period_days}d")
        await update.message.reply_text(
            "Ещё график? Напишите `TICKER [7d|30d|90d|1y]` или нажмите Назад.",
            parse_mode="Markdown",
            reply_markup=cancel_markup(),
        )
        return

    if mode == "convert":
        user = get_user(db, uid)
        default_from = (user.get("base") or DEFAULT_BASE).upper()
        parsed = _parse_convert(text, default_from)
        if not parsed:
            await update.message.reply_text(
                "Не понял ввод. Примеры:\n"
                "• `100 USD RUB`\n"
                "• `250 EUR -> RUB`\n"
                f"• `100 RUB` — из {default_from} в RUB",
                parse_mode="Markdown",
                reply_markup=cancel_markup(),
            )
            return
        amount, frm, to = parsed
        res = _do_convert(amount, frm, to)
        if not res:
            await update.message.reply_text("Не удалось получить курс. Попробуй позже.", reply_markup=cancel_markup())
            return
        converted, rate = res
        frm_s, to_s = ccy_symbol(frm), ccy_symbol(to)
        context.user_data.pop("mode", None)
        await update.message.reply_text(
            f"{format_amount(amount, 2)} {frm_s} ≈ {format_amount(converted, 2)} {to_s}\n"
            f"Курс: 1 {frm_s} = {format_amount(rate, 4)} {to_s}",
            reply_markup=main_menu_markup(get_user(db, uid)),
        )
        return

    if mode == "convert_amount":
        frm = (context.user_data.get("conv_from") or "").upper()
        to = (context.user_data.get("conv_to") or "").upper()
        amt = _parse_amount(text)
        if not frm or not to:
            context.user_data.pop("mode", None)
            await update.message.reply_text("Сначала выберите валюты.", reply_markup=main_menu_markup(user))
            return
        if amt is None:
            await update.message.reply_text("Не понял число. Пример: 100 или 100.5", reply_markup=cancel_markup())
            return
        res = _do_convert(amt, frm, to)
        if not res:
            await update.message.reply_text("Не удалось получить курс. Попробуй позже.", reply_markup=cancel_markup())
            return
        converted, rate = res
        # остаёмся в режиме конвертера с выбранными валютами
        await update.message.reply_text(
            f"{format_amount(amt, 2)} {frm} ≈ {format_amount(converted, 2)} {to}\n"
            f"Курс: 1 {frm} = {format_amount(rate, 4)} {to}",
            reply_markup=convert_continue_markup(),
        )
        return

    if mode == "assist":
        history = context.user_data.setdefault("assist_hist", [])
        # Ограничим историю, чтобы не разрасталась
        if len(history) > 10:
            history[:] = history[-10:]

        # Плейсхолдер + индикатор набора для помощника
        placeholder = await update.message.reply_text("🤖 Помощник думает…", reply_markup=cancel_markup())
        chat_id = update.effective_chat.id

        sent = False
        reply_text = None
        try:
            async with typing_indicator(context.bot, chat_id):
                # Генерируем маршрут и короткий ответ
                reply_text, route = await asyncio.to_thread(ai_route_and_reply, user, history, text)

                if route and isinstance(route, dict):
                    action = route.get("action")
                    args = route.get("args") or {}
                    base = (user.get("base") or DEFAULT_BASE).lower()

                    if action == "price":
                        tickers = args.get("tickers") or user.get("tickers") or []
                        tickers = [normalize_ticker(t) for t in tickers if normalize_ticker(t)]
                        if tickers:
                            prices = await fetch_prices(tickers, base)
                            lines = []
                            for k in tickers:
                                p = prices.get(k)
                                if not p:
                                    continue
                                chg = p.get("chg")
                                chg_txt = f" ({chg:+.2%})" if isinstance(chg, (int, float)) else ""
                                price_txt = fmt_with_symbol(p["price"], base, 2)
                                lines.append(f"{k}: {price_txt}{chg_txt}")
                            if lines:
                                await update.message.reply_text("📊 Котировки:\n" + "\n".join(lines), reply_markup=cancel_markup())
                                sent = True
                        if (not sent) and reply_text:
                            await update.message.reply_text(reply_text, reply_markup=cancel_markup())
                            sent = True

                    elif action == "analyze":
                        tickers = args.get("tickers") or user.get("tickers") or []
                        horizon = args.get("horizon") or context.user_data.get("anl_h", "30d")
                        await run_analyst_reply(update.message, context, user, tickers=tickers, horizon=horizon, question=text)
                        sent = True

                    elif action == "chart":
                        ticker = normalize_ticker(args.get("ticker") or "")
                        period = args.get("period") or "7d"
                        days = parse_period(period)
                        if ticker:
                            series = history_for_chart(ticker, base, days)
                            if series:
                                png = make_chart(series, ticker, base)
                                if png:
                                    await update.message.reply_photo(photo=png, caption=f"{ticker} · {days}d")
                                    sent = True
                        if (not sent) and reply_text:
                            await update.message.reply_text(reply_text, reply_markup=cancel_markup())
                            sent = True

                    elif action == "convert":
                        amt = args.get("amount")
                        frm = args.get("frm")
                        to = args.get("to")
                        if isinstance(amt, (int, float)) and frm and to:
                            res = _do_convert(float(amt), str(frm), str(to))
                            if res:
                                converted, rate = res
                                frm_s, to_s = ccy_symbol(frm), ccy_symbol(to)
                                await update.message.reply_text(
                                    f"{format_amount(float(amt), 2)} {frm_s} ≈ {format_amount(converted, 2)} {to_s}\n"
                                    f"Курс: 1 {frm_s} = {format_amount(rate, 4)} {to_s}",
                                    reply_markup=cancel_markup(),
                                )
                                sent = True
                        if (not sent) and reply_text:
                            await update.message.reply_text(reply_text, reply_markup=cancel_markup())
                            sent = True

                    elif action == "add":
                        tickers = args.get("tickers") or []
                        tickers = [normalize_ticker(t) for t in tickers if normalize_ticker(t)]
                        if tickers:
                            new_list = add_tickers(db, uid, tickers)
                            await update.message.reply_text("Добавил. Текущий список: " + (", ".join(new_list) if new_list else "пусто"), reply_markup=cancel_markup())
                            sent = True
                        if (not sent) and reply_text:
                            await update.message.reply_text(reply_text, reply_markup=cancel_markup())
                            sent = True

                    elif action == "remove":
                        tickers = args.get("tickers") or []
                        tickers = [normalize_ticker(t) for t in tickers if normalize_ticker(t)]
                        if tickers:
                            removed = remove_tickers(db, uid, tickers)
                            await update.message.reply_text(("Удалил: " + ", ".join(removed)) if removed else "Ничего не удалил.", reply_markup=cancel_markup())
                            sent = True
                        if (not sent) and reply_text:
                            await update.message.reply_text(reply_text, reply_markup=cancel_markup())
                            sent = True

                    elif action == "setbase":
                        base_code = (args.get("base") or "").upper()
                        if base_code in SUPPORTED_BASES:
                            user["base"] = base_code.lower()
                            set_user(db, uid, user)
                            await update.message.reply_text(f"Базовая валюта: {base_code}", reply_markup=cancel_markup())
                            sent = True
                        if (not sent) and reply_text:
                            await update.message.reply_text(reply_text, reply_markup=cancel_markup())
                            sent = True

                    else:
                        # Unknown or 'answer' fallback
                        if reply_text:
                            await update.message.reply_text(reply_text, reply_markup=cancel_markup())
                            sent = True
                else:
                    # Нет маршрута — просто ответим текстом, если есть
                    if reply_text:
                        await update.message.reply_text(reply_text, reply_markup=cancel_markup())
                        sent = True

        except Exception as e:
            logger.warning("AI route handling failed: %s", e)
            if reply_text:
                await update.message.reply_text(reply_text, reply_markup=cancel_markup())
                sent = True

        # Append turn to history and stay in assistant mode
        try:
            history.append({"role": "user", "content": text})
            history.append({"role": "assistant", "content": (reply_text or "ok")[:500]})
            if len(history) > 10:
                history[:] = history[-10:]
        except Exception:
            pass

        # Обновим плейсхолдер: если уже что-то отправили — короткое подтверждение, иначе вставим ответ
        try:
            if sent:
                await placeholder.edit_text("✅ Готово", reply_markup=cancel_markup())
            else:
                await placeholder.edit_text(reply_text or "Готово.", reply_markup=cancel_markup())
        except Exception:
            pass
        return

    await update.message.reply_text("Используй кнопки ниже ⤵️", reply_markup=main_menu_markup(user))

# ========== Run ==========
async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.exception("Unhandled error: %s", context.error)

def main():
    if not BOT_TOKEN:
        raise SystemExit("Нет BOT_TOKEN в .env")

    # Build Telegram Application with robust HTTPX timeouts
    try:
        from telegram.request import HTTPXRequest
        req = HTTPXRequest(
            connect_timeout=float(os.getenv("TG_CONNECT_TIMEOUT", "10")),
            read_timeout=float(os.getenv("TG_READ_TIMEOUT", "25")),
            write_timeout=float(os.getenv("TG_WRITE_TIMEOUT", "10")),
            pool_timeout=float(os.getenv("TG_POOL_TIMEOUT", "5")),
        )
        app = (
            Application.builder()
            .token(BOT_TOKEN)
            .request(req)
            .get_updates_request_timeout(int(os.getenv("TG_UPDATES_TIMEOUT", "30")))
            .build()
        )
    except Exception:
        app = Application.builder().token(BOT_TOKEN).build()

    # Set default executor for blocking I/O if pool defined
    try:
        loop = asyncio.get_event_loop()
        if "_IO_POOL" in globals():
            loop.set_default_executor(_IO_POOL)
    except Exception:
        pass


    # хэндлеры (как у тебя уже есть)
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("add", add_cmd))
    app.add_handler(CommandHandler("remove", remove_cmd))
    app.add_handler(CommandHandler("clear", clear_cmd))
    app.add_handler(CommandHandler("list", list_cmd))
    app.add_handler(CommandHandler("price", price_cmd))
    app.add_handler(CommandHandler("chart", chart_cmd))
    app.add_handler(CommandHandler("convert", convert_cmd))
    app.add_handler(CallbackQueryHandler(on_cb))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    app.add_error_handler(error_handler)

    mode = (os.getenv("MODE", "polling") or "polling").lower()
    if mode == "webhook":
        # Для Koyeb/любого PaaS с HTTPS
        port = int(os.getenv("PORT", "8080"))
        base = os.getenv("WEBHOOK_BASE", "").strip()
        if not base.startswith("https://"):
            raise SystemExit(
                "Для webhook установи переменную окружения WEBHOOK_BASE, "
                "например: https://<app>.koyeb.app"
            )
        url_path = f"/bot{BOT_TOKEN}"  # уникальный путь
        app.run_webhook(
            listen="0.0.0.0",
            port=port,
            url_path=url_path,
            webhook_url=base.rstrip("/") + url_path,
            allowed_updates=Update.ALL_TYPES,
            drop_pending_updates=True,
        )
    else:
        # локально можно продолжать запускать через polling
        app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)


if __name__ == "__main__":
    main()