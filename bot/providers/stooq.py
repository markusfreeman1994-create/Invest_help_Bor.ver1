from __future__ import annotations

from typing import Dict, List, Tuple
from datetime import datetime
import requests
from ..config import logger
from .fx import fx_rate

def _close_of(row: str):
    parts = row.split(",")
    if len(parts) >= 5 and parts[4] not in ("", "null", "None"):
        try:
            return float(parts[4])
        except Exception:
            return None
    return None

def _open_of(row: str):
    parts = row.split(",")
    if len(parts) >= 2 and parts[1] not in ("", "null", "None"):
        try:
            return float(parts[1])
        except Exception:
            return None
    return None

def fetch_stock_prices(symbols: List[str], vs: str) -> Dict[str, Dict[str, float]]:
    out: Dict[str, Dict[str, float]] = {}
    if not symbols:
        return out
    rate = fx_rate("USD", (vs or "USD").upper())
    if not isinstance(rate, (int, float)) or rate <= 0:
        logger.warning("FX rate unavailable for USD -> %s", vs)
        return out
    for sym in symbols:
        try:
            stooq_sym = f"{sym.replace('-', '.').lower()}.us"
            url = "https://stooq.com/q/d/l/"
            params = {"s": stooq_sym, "i": "d"}
            r = requests.get(url, params=params, timeout=10)
            r.raise_for_status()
            txt = (r.text or "").strip()
            lines = txt.splitlines()
            if len(lines) < 2:
                continue
            last_close = _close_of(lines[-1])
            if len(lines) >= 3:
                prev_close = _close_of(lines[-2])
            else:
                prev_close = _open_of(lines[-1])
            if not isinstance(last_close, (int, float)) or not isinstance(prev_close, (int, float)) or prev_close == 0:
                continue
            price_vs = float(last_close) * float(rate)
            chg = (last_close - prev_close) / prev_close
            out[sym.upper()] = {"price": price_vs, "chg": chg}
        except Exception as e:
            logger.warning("Stooq fetch failed for %s: %s", sym, e)
            continue
    return out

def fetch_history(symbol: str, days: int) -> List[Tuple[datetime, float]]:
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
        return []