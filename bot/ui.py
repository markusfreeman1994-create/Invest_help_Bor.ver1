from __future__ import annotations

from typing import Dict
from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from .config import DEFAULT_BASE

def format_amount(x: float, decimals: int = 2) -> str:
    try:
        return f"{x:,.{decimals}f}".replace(",", " ")
    except Exception:
        return str(x)

def main_menu_markup(user: Dict) -> InlineKeyboardMarkup:
    base = (user.get("base") or DEFAULT_BASE).upper()
    rows = [
        [InlineKeyboardButton("📊 Цены", callback_data="ACT:PRICE"),
         InlineKeyboardButton("🧾 Список", callback_data="ACT:LIST")],
        [InlineKeyboardButton("➕ Добавить", callback_data="ACT:ADD"),
         InlineKeyboardButton("➖ Удалить", callback_data="ACT:REMOVE")],
        [InlineKeyboardButton("📈 График", callback_data="ACT:CHART"),
         InlineKeyboardButton(f"💱 Валюта: {base}", callback_data="ACT:BASE")],
    ]
    if (user.get("tickers") or []):
        rows.append([InlineKeyboardButton("🧹 Очистить", callback_data="ACT:CLEAR")])
    return InlineKeyboardMarkup(rows)

def cancel_markup() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("◀️ Назад", callback_data="ACT:BACK")]])

def base_menu_markup(user: Dict) -> InlineKeyboardMarkup:
    base = (user.get("base") or DEFAULT_BASE).upper()
    rows = [[
        InlineKeyboardButton(("✅ " if base=="USD" else "") + "USD", callback_data="BASE:USD"),
        InlineKeyboardButton(("✅ " if base=="EUR" else "") + "EUR", callback_data="BASE:EUR"),
        InlineKeyboardButton(("✅ " if base=="RUB" else "") + "RUB", callback_data="BASE:RUB"),
    ], [InlineKeyboardButton("◀️ Назад", callback_data="ACT:BACK")]]
    return InlineKeyboardMarkup(rows)