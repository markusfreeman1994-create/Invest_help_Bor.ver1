from __future__ import annotations

from typing import Dict, List, Tuple
from datetime import datetime
from ..config import COINGECKO_BASE, COINGECKO_API_KEY, logger
from ..utils_http import http_get_json

CG_ID_MAP_STATIC: Dict[str, str] = {
    "BTC": "bitcoin", "ETH": "ethereum", "TON": "toncoin",
    "USDT": "tether", "USDC": "usd-coin", "BNB": "binancecoin",
    "SOL": "solana", "TRX": "tron", "DOGE": "dogecoin",
    "XRP": "ripple", "ADA": "cardano", "DOT": "polkadot",
    "AVAX": "avalanche-2", "MATIC": "polygon-pos", "LINK": "chainlink",
    "XLM": "stellar", "ATOM": "cosmos", "NEAR": "near", "APT": "aptos",
    "ARB": "arbitrum", "OP": "optimism", "TIA": "celestia", "SUI": "sui",
    "ICP": "internet-computer", "ETC": "ethereum-classic", "BCH": "bitcoin-cash",
    "LTC": "litecoin", "PEPE": "pepe", "SHIB": "shiba-inu",
    "WBTC": "wrapped-bitcoin", "WETH": "weth",
}
_symbol_to_id_cache: Dict[str, str] = {}

def cg_headers() -> Dict[str, str]:
    h = {"Accept": "application/json"}
    if COINGECKO_API_KEY:
        h["x-cg-pro-api-key"] = COINGECKO_API_KEY
    return h

def is_crypto_symbol(sym: str) -> bool:
    s = (sym or "").upper()
    return s in CG_ID_MAP_STATIC or s in _symbol_to_id_cache or s == "TON"

def map_symbols_to_ids(symbols: List[str]) -> Dict[str, str]:
    out, unknown = {}, []
    for s in symbols:
        k = s.upper()
        if k in CG_ID_MAP_STATIC:
            out[k] = CG_ID_MAP_STATIC[k]
        elif k in _symbol_to_id_cache:
            out[k] = _symbol_to_id_cache[k]
        else:
            unknown.append(k)
    if unknown:
        data = http_get_json(f"{COINGECKO_BASE}/coins/list", headers=cg_headers(), timeout=20)
        if isinstance(data, list):
            tmp = {}
            for it in data:
                sym = (it.get("symbol") or "").upper()
                cid = it.get("id")
                if sym and cid and sym not in tmp:
                    tmp[sym] = cid
            for sym in unknown:
                cid = tmp.get(sym)
                if cid:
                    _symbol_to_id_cache[sym] = cid
                    out[sym] = cid
    return out

def fetch_prices(symbols: List[str], vs: str) -> Dict[str, Dict[str, float]]:
    ids_map = map_symbols_to_ids(symbols)
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

def fetch_history(symbol: str, vs: str, days: int) -> List[Tuple[datetime, float]]:
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