from __future__ import annotations

import re
from typing import Dict, List
from .config import DEFAULT_BASE
from .providers.coingecko import fetch_prices as cg_fetch_prices, is_crypto_symbol
from .providers.tonapi import fetch_ton_rates
from .providers.stooq import fetch_stock_prices

def normalize_ticker(s: str) -> str:
    s = s.strip().upper()
    s = re.sub(r'^\$+', '', s)
    s = re.sub(r'[^A-Z0-9\.\-]', '', s)
    s = s.replace('.', '-')
    return s

def add_tickers(db: Dict[str, Dict], uid: int, symbols: List[str]) -> List[str]:
    user = db.get(str(uid), {"tickers": [], "base": DEFAULT_BASE})
    exist = set(normalize_ticker(t) for t in user.get("tickers", []))
    for s in symbols:
        s = normalize_ticker(s)
        if s:
            exist.add(s)
    user["tickers"] = sorted(exist)
    db[str(uid)] = user
    return user["tickers"]

def remove_tickers(db: Dict[str, Dict], uid: int, symbols: List[str]) -> List[str]:
    user = db.get(str(uid), {"tickers": [], "base": DEFAULT_BASE})
    current = set(user.get("tickers", []))
    to_remove = {normalize_ticker(s) for s in symbols if normalize_ticker(s)}
    removed = sorted(list(current & to_remove))
    current -= to_remove
    user["tickers"] = sorted(current)
    db[str(uid)] = user
    return removed

def clear_tickers(db: Dict[str, Dict], uid: int) -> None:
    user = db.get(str(uid), {"tickers": [], "base": DEFAULT_BASE})
    user["tickers"] = []
    db[str(uid)] = user

def fetch_prices_sync(symbols: List[str], vs: str) -> Dict[str, Dict[str, float]]:
    symbols_norm = [normalize_ticker(s) for s in symbols if normalize_ticker(s)]
    if not symbols_norm:
        return {}
    vs_l = (vs or "usd").lower()
    vs_u = vs_l.upper()

    result: Dict[str, Dict[str, float]] = {}
    crypto = [s for s in symbols_norm if is_crypto_symbol(s)]
    equity = [s for s in symbols_norm if s not in crypto]

    ton_res = fetch_ton_rates(crypto, vs_l)
    if ton_res:
        result.update(ton_res)
    remaining_crypto = [s for s in crypto if s not in result]
    if remaining_crypto:
        cg = cg_fetch_prices(remaining_crypto, vs_l)
        if cg:
            result.update(cg)
    if equity:
        stq = fetch_stock_prices(equity, vs_u)
        if stq:
            result.update(stq)
    remaining_crypto = [s for s in crypto if s not in result]
    if remaining_crypto:
        cg2 = cg_fetch_prices(remaining_crypto, vs_l)
        if cg2:
            result.update(cg2)
    return result