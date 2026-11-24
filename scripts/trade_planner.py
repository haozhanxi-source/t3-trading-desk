#!/usr/bin/env python3
"""Layer-4 trade expression, carry analysis, four-quadrant gate, and position sizing engine.

Pure, deterministic execution planning built on top of Layer 2 (Daily Chanlun direction +
30min Chanlun entry/stop) and Layer 3 (fundamental / supply-demand / rate-diff research).

Key survival rules codified here:
  1. Backwardation (大B结构 / 短期低库存) is NEVER a veto on shorting commodities.
     When fundamentals point to future oversupply ("强现实 vs 弱预期"劈叉), shorts remain
     actionable while term-structure carry quantifies roll drag and guides front vs far-month
     contract selection.
  2. Equities are structurally long-biased (outright shorts blocked unless explicitly marked
     hedge_only or pair_trade); commodities, FX, and crypto are fully symmetric long/short.
  3. 30min Chanlun tactical stops size the trade with strict floor-rounded lot discipline
     (oversized single contracts/lots are NEVER rounded up to breach risk budgets), and
     tranches 2+ enforce "仅允许浮盈确认后顺势加仓，严禁浮亏摊平".
"""
from __future__ import annotations

import argparse
import inspect
import json
import math
import os
import re
import sys
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, ROUND_FLOOR
from typing import Any
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(__file__))
from market_snapshot import normalize_symbol  # noqa: E402


# ---------------------------------------------------------------------------
# 1. Exact Quote-Unit Contract Specifications
# ---------------------------------------------------------------------------

CN_FUTURE_SPECS: dict[str, dict[str, Any]] = {
    # Non-ferrous & Precious Metals (SHFE)
    "CU": {
        "root": "CU",
        "asset_class": "cn_future",
        "multiplier": 5.0,
        "lot_size": 1,
        "currency": "CNY",
        "margin_rate": 0.12,
        "fractional": False,
        "unit_note": "CNY/mt * 5 mt/hand (SHFE 沪铜)",
    },
    "AL": {
        "root": "AL",
        "asset_class": "cn_future",
        "multiplier": 5.0,
        "lot_size": 1,
        "currency": "CNY",
        "margin_rate": 0.12,
        "fractional": False,
        "unit_note": "CNY/mt * 5 mt/hand (SHFE 沪铝)",
    },
    "ZN": {
        "root": "ZN",
        "asset_class": "cn_future",
        "multiplier": 5.0,
        "lot_size": 1,
        "currency": "CNY",
        "margin_rate": 0.12,
        "fractional": False,
        "unit_note": "CNY/mt * 5 mt/hand (SHFE 沪锌)",
    },
    "NI": {
        "root": "NI",
        "asset_class": "cn_future",
        "multiplier": 1.0,
        "lot_size": 1,
        "currency": "CNY",
        "margin_rate": 0.14,
        "fractional": False,
        "unit_note": "CNY/mt * 1 mt/hand (SHFE 沪镍)",
    },
    "SN": {
        "root": "SN",
        "asset_class": "cn_future",
        "multiplier": 1.0,
        "lot_size": 1,
        "currency": "CNY",
        "margin_rate": 0.14,
        "fractional": False,
        "unit_note": "CNY/mt * 1 mt/hand (SHFE 沪锡)",
    },
    "AU": {
        "root": "AU",
        "asset_class": "cn_future",
        "multiplier": 1000.0,
        "lot_size": 1,
        "currency": "CNY",
        "margin_rate": 0.10,
        "fractional": False,
        "unit_note": "CNY/g * 1,000 g/hand (SHFE 沪金)",
    },
    "AG": {
        "root": "AG",
        "asset_class": "cn_future",
        "multiplier": 15.0,
        "lot_size": 1,
        "currency": "CNY",
        "margin_rate": 0.14,
        "fractional": False,
        "unit_note": "CNY/kg * 15 kg/hand (SHFE 沪银)",
    },
    # Ferrous (SHFE / DCE)
    "RB": {
        "root": "RB",
        "asset_class": "cn_future",
        "multiplier": 10.0,
        "lot_size": 1,
        "currency": "CNY",
        "margin_rate": 0.12,
        "fractional": False,
        "unit_note": "CNY/mt * 10 mt/hand (SHFE 螺纹钢)",
    },
    "HC": {
        "root": "HC",
        "asset_class": "cn_future",
        "multiplier": 10.0,
        "lot_size": 1,
        "currency": "CNY",
        "margin_rate": 0.12,
        "fractional": False,
        "unit_note": "CNY/mt * 10 mt/hand (SHFE 热轧卷板)",
    },
    "I": {
        "root": "I",
        "asset_class": "cn_future",
        "multiplier": 100.0,
        "lot_size": 1,
        "currency": "CNY",
        "margin_rate": 0.15,
        "fractional": False,
        "unit_note": "CNY/mt * 100 mt/hand (DCE 铁矿石)",
    },
    "J": {
        "root": "J",
        "asset_class": "cn_future",
        "multiplier": 100.0,
        "lot_size": 1,
        "currency": "CNY",
        "margin_rate": 0.15,
        "fractional": False,
        "unit_note": "CNY/mt * 100 mt/hand (DCE 焦炭)",
    },
    "JM": {
        "root": "JM",
        "asset_class": "cn_future",
        "multiplier": 60.0,
        "lot_size": 1,
        "currency": "CNY",
        "margin_rate": 0.15,
        "fractional": False,
        "unit_note": "CNY/mt * 60 mt/hand (DCE 焦煤)",
    },
    # New Energy Metals (GFEX)
    "LC": {
        "root": "LC",
        "asset_class": "cn_future",
        "multiplier": 1.0,
        "lot_size": 1,
        "currency": "CNY",
        "margin_rate": 0.15,
        "fractional": False,
        "unit_note": "CNY/mt * 1 mt/hand (GFEX 碳酸锂)",
    },
    "SI": {
        "root": "SI",
        "asset_class": "cn_future",
        "multiplier": 5.0,
        "lot_size": 1,
        "currency": "CNY",
        "margin_rate": 0.14,
        "fractional": False,
        "unit_note": "CNY/mt * 5 mt/hand (GFEX 工业硅)",
    },
    "PS": {
        "root": "PS",
        "asset_class": "cn_future",
        "multiplier": 3.0,
        "lot_size": 1,
        "currency": "CNY",
        "margin_rate": 0.14,
        "fractional": False,
        "unit_note": "CNY/mt * 3 mt/hand (GFEX 多晶硅)",
    },
    # Energy & Chemicals (INE / CZCE / DCE / SHFE)
    "SC": {
        "root": "SC",
        "asset_class": "cn_future",
        "multiplier": 1000.0,
        "lot_size": 1,
        "currency": "CNY",
        "margin_rate": 0.12,
        "fractional": False,
        "unit_note": "CNY/bbl * 1,000 bbl/hand (INE 上海原油)",
    },
    "TA": {
        "root": "TA",
        "asset_class": "cn_future",
        "multiplier": 5.0,
        "lot_size": 1,
        "currency": "CNY",
        "margin_rate": 0.10,
        "fractional": False,
        "unit_note": "CNY/mt * 5 mt/hand (CZCE PTA)",
    },
    "MA": {
        "root": "MA",
        "asset_class": "cn_future",
        "multiplier": 10.0,
        "lot_size": 1,
        "currency": "CNY",
        "margin_rate": 0.10,
        "fractional": False,
        "unit_note": "CNY/mt * 10 mt/hand (CZCE 甲醇)",
    },
    "V": {
        "root": "V",
        "asset_class": "cn_future",
        "multiplier": 5.0,
        "lot_size": 1,
        "currency": "CNY",
        "margin_rate": 0.10,
        "fractional": False,
        "unit_note": "CNY/mt * 5 mt/hand (DCE PVC)",
    },
    "SA": {
        "root": "SA",
        "asset_class": "cn_future",
        "multiplier": 20.0,
        "lot_size": 1,
        "currency": "CNY",
        "margin_rate": 0.12,
        "fractional": False,
        "unit_note": "CNY/mt * 20 mt/hand (CZCE 纯碱)",
    },
    "FG": {
        "root": "FG",
        "asset_class": "cn_future",
        "multiplier": 20.0,
        "lot_size": 1,
        "currency": "CNY",
        "margin_rate": 0.12,
        "fractional": False,
        "unit_note": "CNY/mt * 20 mt/hand (CZCE 玻璃)",
    },
    "RU": {
        "root": "RU",
        "asset_class": "cn_future",
        "multiplier": 10.0,
        "lot_size": 1,
        "currency": "CNY",
        "margin_rate": 0.12,
        "fractional": False,
        "unit_note": "CNY/mt * 10 mt/hand (SHFE 天然橡胶)",
    },
    # Livestock & Agriculture (DCE / CZCE)
    "LH": {
        "root": "LH",
        "asset_class": "cn_future",
        "multiplier": 16.0,
        "lot_size": 1,
        "currency": "CNY",
        "margin_rate": 0.12,
        "fractional": False,
        "unit_note": "CNY/mt * 16 mt/hand (DCE 生猪)",
    },
    "M": {
        "root": "M",
        "asset_class": "cn_future",
        "multiplier": 10.0,
        "lot_size": 1,
        "currency": "CNY",
        "margin_rate": 0.10,
        "fractional": False,
        "unit_note": "CNY/mt * 10 mt/hand (DCE 豆粕)",
    },
    "Y": {
        "root": "Y",
        "asset_class": "cn_future",
        "multiplier": 10.0,
        "lot_size": 1,
        "currency": "CNY",
        "margin_rate": 0.10,
        "fractional": False,
        "unit_note": "CNY/mt * 10 mt/hand (DCE 豆油)",
    },
    "P": {
        "root": "P",
        "asset_class": "cn_future",
        "multiplier": 10.0,
        "lot_size": 1,
        "currency": "CNY",
        "margin_rate": 0.10,
        "fractional": False,
        "unit_note": "CNY/mt * 10 mt/hand (DCE 棕榈油)",
    },
    "OI": {
        "root": "OI",
        "asset_class": "cn_future",
        "multiplier": 10.0,
        "lot_size": 1,
        "currency": "CNY",
        "margin_rate": 0.10,
        "fractional": False,
        "unit_note": "CNY/mt * 10 mt/hand (CZCE 菜油)",
    },
    "SR": {
        "root": "SR",
        "asset_class": "cn_future",
        "multiplier": 10.0,
        "lot_size": 1,
        "currency": "CNY",
        "margin_rate": 0.10,
        "fractional": False,
        "unit_note": "CNY/mt * 10 mt/hand (CZCE 白糖)",
    },
    "CF": {
        "root": "CF",
        "asset_class": "cn_future",
        "multiplier": 5.0,
        "lot_size": 1,
        "currency": "CNY",
        "margin_rate": 0.10,
        "fractional": False,
        "unit_note": "CNY/mt * 5 mt/hand (CZCE 棉花)",
    },
    # Shipping (INE)
    "EC": {
        "root": "EC",
        "asset_class": "cn_future",
        "multiplier": 50.0,
        "lot_size": 1,
        "currency": "CNY",
        "margin_rate": 0.18,
        "fractional": False,
        "unit_note": "Index pts * 50 CNY/pt (INE 集运欧线)",
    },
}

OVERSEAS_FUTURE_SPECS: dict[str, dict[str, Any]] = {
    "CL=F": {
        "root": "CL",
        "asset_class": "commodity",
        "multiplier": 1000.0,
        "lot_size": 1,
        "currency": "USD",
        "margin_rate": 0.10,
        "fractional": False,
        "unit_note": "USD/bbl * 1,000 bbl = 1,000.0 USD per 1.00 quote point (NYMEX WTI Crude Oil)",
    },
    "BZ=F": {
        "root": "BZ",
        "asset_class": "commodity",
        "multiplier": 1000.0,
        "lot_size": 1,
        "currency": "USD",
        "margin_rate": 0.10,
        "fractional": False,
        "unit_note": "USD/bbl * 1,000 bbl = 1,000.0 USD per 1.00 quote point (Brent Crude Oil)",
    },
    "NG=F": {
        "root": "NG",
        "asset_class": "commodity",
        "multiplier": 10000.0,
        "lot_size": 1,
        "currency": "USD",
        "margin_rate": 0.15,
        "fractional": False,
        "unit_note": "USD/MMBtu * 10,000 MMBtu = 10,000.0 USD per 1.00 quote point (NYMEX Natural Gas)",
    },
    "HG=F": {
        "root": "HG",
        "asset_class": "commodity",
        "multiplier": 25000.0,
        "lot_size": 1,
        "currency": "USD",
        "margin_rate": 0.10,
        "fractional": False,
        "unit_note": "USD/lb * 25,000 lb = 25,000.0 USD per 1.00 quote point (COMEX Copper)",
    },
    "GC=F": {
        "root": "GC",
        "asset_class": "commodity",
        "multiplier": 100.0,
        "lot_size": 1,
        "currency": "USD",
        "margin_rate": 0.08,
        "fractional": False,
        "unit_note": "USD/troy oz * 100 oz = 100.0 USD per 1.00 quote point (COMEX Gold)",
    },
    "SI=F": {
        "root": "SI",
        "asset_class": "commodity",
        "multiplier": 5000.0,
        "lot_size": 1,
        "currency": "USD",
        "margin_rate": 0.12,
        "fractional": False,
        "unit_note": "USD/troy oz * 5,000 oz = 5,000.0 USD per 1.00 quote point (COMEX Silver)",
    },
    "ALI=F": {
        "root": "ALI",
        "asset_class": "commodity",
        "multiplier": 25.0,
        "lot_size": 1,
        "currency": "USD",
        "margin_rate": 0.10,
        "fractional": False,
        "unit_note": "USD/mt * 25 mt = 25.0 USD per 1.00 quote point (COMEX Aluminum)",
    },
    "ZC=F": {
        "root": "ZC",
        "asset_class": "commodity",
        "multiplier": 50.0,
        "lot_size": 1,
        "currency": "USD",
        "margin_rate": 0.08,
        "fractional": False,
        "unit_note": "Quoted in US cents/bu, 5,000 bu/contract => 50.0 USD per 1.00 quote point (CBOT Corn)",
    },
    "ZS=F": {
        "root": "ZS",
        "asset_class": "commodity",
        "multiplier": 50.0,
        "lot_size": 1,
        "currency": "USD",
        "margin_rate": 0.08,
        "fractional": False,
        "unit_note": "Quoted in US cents/bu, 5,000 bu/contract => 50.0 USD per 1.00 quote point (CBOT Soybeans)",
    },
    "ZW=F": {
        "root": "ZW",
        "asset_class": "commodity",
        "multiplier": 50.0,
        "lot_size": 1,
        "currency": "USD",
        "margin_rate": 0.08,
        "fractional": False,
        "unit_note": "Quoted in US cents/bu, 5,000 bu/contract => 50.0 USD per 1.00 quote point (CBOT SRW Wheat)",
    },
    "SB=F": {
        "root": "SB",
        "asset_class": "commodity",
        "multiplier": 1120.0,
        "lot_size": 1,
        "currency": "USD",
        "margin_rate": 0.10,
        "fractional": False,
        "unit_note": "Quoted in US cents/lb, 112,000 lb/contract => 1,120.0 USD per 1.00 quote point (ICE Sugar No. 11)",
    },
    "CT=F": {
        "root": "CT",
        "asset_class": "commodity",
        "multiplier": 500.0,
        "lot_size": 1,
        "currency": "USD",
        "margin_rate": 0.10,
        "fractional": False,
        "unit_note": "Quoted in US cents/lb, 50,000 lb/contract => 500.0 USD per 1.00 quote point (ICE Cotton No. 2)",
    },
    "KC=F": {
        "root": "KC",
        "asset_class": "commodity",
        "multiplier": 375.0,
        "lot_size": 1,
        "currency": "USD",
        "margin_rate": 0.12,
        "fractional": False,
        "unit_note": "Quoted in US cents/lb, 37,500 lb/contract => 375.0 USD per 1.00 quote point (ICE Coffee C)",
    },
    "CC=F": {
        "root": "CC",
        "asset_class": "commodity",
        "multiplier": 10.0,
        "lot_size": 1,
        "currency": "USD",
        "margin_rate": 0.15,
        "fractional": False,
        "unit_note": "USD/mt * 10 mt = 10.0 USD per 1.00 quote point (ICE Cocoa)",
    },
    "LE=F": {
        "root": "LE",
        "asset_class": "commodity",
        "multiplier": 400.0,
        "lot_size": 1,
        "currency": "USD",
        "margin_rate": 0.08,
        "fractional": False,
        "unit_note": "Quoted in US cents/lb, 40,000 lb/contract => 400.0 USD per 1.00 quote point (CME Live Cattle)",
    },
    "HE=F": {
        "root": "HE",
        "asset_class": "commodity",
        "multiplier": 400.0,
        "lot_size": 1,
        "currency": "USD",
        "margin_rate": 0.08,
        "fractional": False,
        "unit_note": "Quoted in US cents/lb, 40,000 lb/contract => 400.0 USD per 1.00 quote point (CME Lean Hogs)",
    },
}

for _spec_map in (CN_FUTURE_SPECS, OVERSEAS_FUTURE_SPECS):
    for _spec_entry in _spec_map.values():
        _step = _spec_entry.get("lot_step", _spec_entry.get("lot_size", 1))
        _spec_entry.setdefault("min_order_qty", _step)
        _spec_entry.setdefault("lot_step", _step)
        _spec_entry["lot_size"] = _spec_entry["lot_step"]
        _spec_entry.setdefault("quote_currency", _spec_entry["currency"])

CONTRACT_SPECS: dict[str, dict[str, Any]] = {
    **CN_FUTURE_SPECS,
    **OVERSEAS_FUTURE_SPECS,
}

_CN_DATED_CONTRACT_RE = re.compile(r"^([A-Z]{1,2})(0|\d{3,4})$")
_OVERSEAS_DATED_CONTRACT_RE = re.compile(r"^([A-Z]{2,3})([FGHJKMNQUVXZ])(\d{2})\.(NYM|CMX|CBT|NYB|CME|ICE)$")
_BARE_OVERSEAS_FUTURE_RE = re.compile(r"^([A-Z]{2,3})([FGHJKMNQUVXZ])(\d{2})$")

VALID_DIRECTIONS = {"long", "short"}
VALID_ASSET_CLASSES = {"us_stock_or_etf", "a_share", "cn_future", "commodity", "fx", "crypto", "macro"}


def _enrich_spec(spec: dict[str, Any]) -> dict[str, Any]:
    """Ensure every contract spec exposes min_order_qty, lot_step, lot_size, currency, and quote_currency."""
    out = dict(spec)
    step = out.get("lot_step", out.get("lot_size", 1))
    min_qty = out.get("min_order_qty", step)
    out["min_order_qty"] = min_qty
    out["lot_step"] = step
    out["lot_size"] = step
    ccy = out.get("quote_currency") or out.get("currency")
    if ccy:
        out["currency"] = ccy
        out["quote_currency"] = ccy
    return out


def _resolve_fx_currencies(norm_sym: str) -> tuple[str | None, str]:
    """Resolve (base_currency, quote_currency) from normalized FX ticker like EURUSD=X or USDJPY=X."""
    cleaned = (norm_sym or "").strip().upper()
    if cleaned.endswith("=X"):
        cleaned = cleaned[:-2]
    cleaned = cleaned.replace("/", "").replace("-", "")
    if re.fullmatch(r"[A-Z]{6}", cleaned):
        return cleaned[:3], cleaned[3:]
    if re.fullmatch(r"[A-Z]{3}", cleaned):
        # Yahoo 3-letter FX shorthand (e.g. JPY=X -> USD/JPY, quote currency is JPY)
        return "USD", cleaned
    return None, "UNKNOWN_QUOTE"


def _require_finite_float(val: Any, name: str, *, positive: bool = False, non_negative: bool = False) -> float:
    if isinstance(val, bool) or not isinstance(val, (int, float)):
        raise ValueError(f"{name} must be a finite number, got {val!r}")
    fval = float(val)
    if not math.isfinite(fval):
        raise ValueError(f"{name} must be finite, got {val!r}")
    if positive and fval <= 0:
        raise ValueError(f"{name} must be > 0, got {val!r}")
    if non_negative and fval < 0:
        raise ValueError(f"{name} must be >= 0, got {val!r}")
    return fval


def _opt_finite_float(val: Any) -> float | None:
    if val is None or isinstance(val, bool):
        return None
    if isinstance(val, (int, float)):
        fval = float(val)
        return fval if math.isfinite(fval) else None
    try:
        fval = float(str(val).strip().rstrip("%"))
        return fval if math.isfinite(fval) else None
    except ValueError:
        return None


def _extract_cn_future_root(sym: str) -> str | None:
    """Extract CN future root from continuous (CU0) or dated (CU2701 / TA609) symbol."""
    upper = (sym or "").strip().upper()
    if upper.startswith("CN:"):
        upper = upper[3:].strip()
    m = _CN_DATED_CONTRACT_RE.fullmatch(upper)
    if not m:
        return None
    root, suffix = m.group(1), m.group(2)
    if suffix == "0":
        return root
    mm = int(suffix[-2:])
    if 1 <= mm <= 12:
        return root
    return None


def resolve_fx_to_account(
    spec: dict[str, Any],
    account_currency: str,
    fx_to_account: float | None,
) -> float:
    """Resolve quote->account FX conversion rate; fail closed on cross-currency trades without explicit rate."""
    acct_ccy = (account_currency or "").strip().upper()
    if not acct_ccy:
        raise ValueError("account_currency must be a non-empty string")
    quote_ccy = str(spec.get("quote_currency") or spec.get("currency") or "").strip().upper()
    if quote_ccy and quote_ccy == acct_ccy:
        if fx_to_account is not None:
            rate = _require_finite_float(fx_to_account, "fx_to_account", positive=True)
            if abs(rate - 1.0) > 1e-9:
                raise ValueError(
                    f"Same-currency trade ({quote_ccy} == {acct_ccy}) must use fx_to_account=1.0 (got {rate})"
                )
        return 1.0
    if fx_to_account is not None:
        return _require_finite_float(fx_to_account, "fx_to_account", positive=True)
    raise ValueError(
        f"Cross-currency trade ({quote_ccy or 'UNKNOWN'} -> {acct_ccy}) requires explicit fx_to_account"
    )


def validate_order_quantity(
    quantity: float,
    spec: dict[str, Any],
    *,
    field_name: str = "quantity",
    enforce_min_order_qty: bool = True,
) -> int | float:
    """Validate quantity against spec min_order_qty and lot_step using exact Decimal math."""
    qty_f = _require_finite_float(quantity, field_name, positive=True)
    dec_qty = Decimal(str(qty_f))
    min_qty = spec.get("min_order_qty", spec.get("lot_step", spec.get("lot_size", 1)))
    lot_step = spec.get("lot_step", spec.get("lot_size", 1))
    dec_min = Decimal(str(min_qty))
    dec_step = Decimal(str(lot_step))

    if enforce_min_order_qty and dec_qty < dec_min:
        raise ValueError(
            f"invalid_quantity_granularity: {field_name} ({qty_f}) is below min_order_qty ({min_qty}) "
            f"for {spec.get('symbol')!r} (asset_class={spec.get('asset_class')})"
        )

    if enforce_min_order_qty:
        delta = dec_qty - dec_min
        if delta % dec_step != 0:
            raise ValueError(
                f"invalid_quantity_granularity: {field_name} ({qty_f}) must satisfy "
                f"min_order_qty={min_qty} + k * lot_step={lot_step} for {spec.get('symbol')!r}"
            )
    else:
        if dec_qty % dec_step != 0:
            raise ValueError(
                f"invalid_quantity_granularity: {field_name} ({qty_f}) must be a multiple of "
                f"lot_step={lot_step} for {spec.get('symbol')!r}"
            )

    if not spec.get("fractional", False):
        if dec_qty != int(dec_qty):
            raise ValueError(
                f"invalid_quantity_granularity: {field_name} ({qty_f}) must be an integer for {spec.get('symbol')!r}"
            )
        return int(dec_qty)
    return float(dec_qty.normalize())


def get_contract_spec(symbol: str, asset_class: str | None = None) -> dict[str, Any]:
    """Return exact quote-unit contract specification for a symbol.

    For unknown or ambiguous futures contracts (including bare overseas futures codes
    without exchange suffix such as ``CLZ26``), returns ``known_spec=False`` and
    ``multiplier=None`` so callers must supply an exchange-suffixed symbol or explicit
    multiplier instead of silently falling back to equity multiplier 1.0.
    """
    raw = (symbol or "").strip()
    if not raw:
        raise ValueError("symbol must be a non-empty string")

    raw_upper = raw.upper()
    explicit_ac = str(asset_class).strip().lower() if asset_class is not None else None
    if explicit_ac is not None and explicit_ac not in VALID_ASSET_CLASSES:
        raise ValueError(f"Unsupported asset_class {asset_class!r}; expected one of {sorted(VALID_ASSET_CLASSES)}")

    # Strip explicit CN: prefix for dated CN contracts like CN:CU2701
    cn_prefixed = raw_upper.startswith("CN:")
    norm_sym, detected_ac = normalize_symbol(raw)
    resolved_ac = explicit_ac or detected_ac

    # Detect dated CN futures contracts (e.g. CU2701, TA609, CN:CU2701) even when normalize_symbol
    # did not match ^[A-Z]{1,2}0$
    cn_root_candidate = _extract_cn_future_root(raw_upper if cn_prefixed else norm_sym)
    if (
        cn_root_candidate
        and cn_root_candidate in CN_FUTURE_SPECS
        and explicit_ac is not None
        and explicit_ac != "cn_future"
    ):
        clean_sym = raw_upper[3:].strip() if cn_prefixed else norm_sym.upper()
        return _enrich_spec({
            "symbol": clean_sym,
            "root": cn_root_candidate,
            "asset_class": "cn_future",
            "multiplier": None,
            "min_order_qty": 1,
            "lot_step": 1,
            "lot_size": 1,
            "currency": "CNY",
            "margin_rate": None,
            "fractional": False,
            "known_spec": False,
            "warning": f"Conflicting asset_class={explicit_ac!r} for CN futures contract {raw_upper!r}.",
            "unit_note": f"Conflicting asset_class={explicit_ac!r} for CN futures contract {raw_upper!r}",
        })
    if cn_root_candidate and (
        explicit_ac == "cn_future"
        or (
            explicit_ac is None
            and (cn_prefixed or detected_ac == "cn_future" or cn_root_candidate in CN_FUTURE_SPECS)
        )
    ):
        resolved_ac = "cn_future"
        clean_sym = raw_upper[3:].strip() if cn_prefixed else norm_sym.upper()
        spec = CN_FUTURE_SPECS.get(cn_root_candidate)
        if spec is not None:
            return _enrich_spec({"symbol": clean_sym, "known_spec": True, **spec})
        return _enrich_spec({
            "symbol": clean_sym,
            "root": cn_root_candidate,
            "asset_class": "cn_future",
            "multiplier": None,
            "min_order_qty": 1,
            "lot_step": 1,
            "lot_size": 1,
            "currency": "CNY",
            "margin_rate": None,
            "fractional": False,
            "known_spec": False,
            "unit_note": f"Unknown CN futures root {cn_root_candidate!r}; explicit multiplier required.",
        })

    # Explicit cn_future with bare root like get_contract_spec("CU", asset_class="cn_future")
    if resolved_ac == "cn_future":
        bare = raw_upper[3:].strip() if cn_prefixed else norm_sym.upper()
        root = _extract_cn_future_root(bare) or bare
        spec = CN_FUTURE_SPECS.get(root)
        if spec is not None:
            return _enrich_spec({"symbol": bare, "known_spec": True, **spec})
        return _enrich_spec({
            "symbol": bare,
            "root": root,
            "asset_class": "cn_future",
            "multiplier": None,
            "min_order_qty": 1,
            "lot_step": 1,
            "lot_size": 1,
            "currency": "CNY",
            "margin_rate": None,
            "fractional": False,
            "known_spec": False,
            "unit_note": f"Unknown CN futures contract {bare!r}; explicit multiplier required.",
        })

    # Reject bare overseas futures contract codes without exchange suffix (e.g. CLZ26, BZG27, HGZ26, ALIZ26)
    # BEFORE honoring any asset_class override (such as asset_class="us_stock_or_etf") so they NEVER
    # silently fall through to equity multiplier=1.0.
    m_bare_ov = _BARE_OVERSEAS_FUTURE_RE.fullmatch(raw_upper)
    if m_bare_ov and not cn_prefixed:
        bare_root = m_bare_ov.group(1)
        return _enrich_spec({
            "symbol": raw_upper,
            "root": bare_root,
            "asset_class": "commodity",
            "multiplier": None,
            "min_order_qty": 1,
            "lot_step": 1,
            "lot_size": 1,
            "currency": "USD",
            "margin_rate": None,
            "fractional": False,
            "known_spec": False,
            "warning": (
                f"Bare overseas futures contract {raw_upper!r} lacks exchange suffix "
                "(e.g. .NYM/.CMX/.CBT/.NYB/.CME); provide exchange suffix or explicit --multiplier."
            ),
            "unit_note": (
                f"Bare overseas futures contract {raw_upper!r} without exchange suffix; "
                "supply exchange-suffixed contract code or explicit multiplier."
            ),
        })

    # Detect dated overseas futures WITH exchange suffix (e.g. CLZ26.NYM, HGH27.CMX, ALIZ26.CMX)
    # Also guard against misclassifying dated overseas futures as equities via asset_class override.
    m_dated_ov = _OVERSEAS_DATED_CONTRACT_RE.fullmatch(raw_upper)
    if m_dated_ov:
        ov_root = m_dated_ov.group(1)
        ov_key = f"{ov_root}=F"
        if explicit_ac not in (None, "commodity"):
            return _enrich_spec({
                "symbol": raw_upper,
                "root": ov_root,
                "asset_class": "commodity",
                "multiplier": None,
                "min_order_qty": 1,
                "lot_step": 1,
                "lot_size": 1,
                "currency": "USD",
                "margin_rate": None,
                "fractional": False,
                "known_spec": False,
                "warning": (
                    f"Dated overseas futures contract {raw_upper!r} cannot be overridden to asset_class={explicit_ac!r}."
                ),
                "unit_note": (
                    f"Dated overseas futures contract {raw_upper!r} conflicts with asset_class={explicit_ac!r}."
                ),
            })
        resolved_ac = "commodity"
        spec = OVERSEAS_FUTURE_SPECS.get(ov_key)
        if spec is not None:
            return _enrich_spec({"symbol": raw_upper, "known_spec": True, **spec})
        return _enrich_spec({
            "symbol": raw_upper,
            "root": ov_root,
            "asset_class": "commodity",
            "multiplier": None,
            "min_order_qty": 1,
            "lot_step": 1,
            "lot_size": 1,
            "currency": "USD",
            "margin_rate": None,
            "fractional": False,
            "known_spec": False,
            "unit_note": f"Unknown overseas futures contract {raw_upper!r}; explicit multiplier required.",
        })

    # Guard continuous overseas futures symbols ending in =F against non-commodity asset_class overrides
    if raw_upper.endswith("=F") and explicit_ac not in (None, "commodity"):
        root = raw_upper[:-2]
        return _enrich_spec({
            "symbol": raw_upper,
            "root": root,
            "asset_class": "commodity",
            "multiplier": None,
            "min_order_qty": 1,
            "lot_step": 1,
            "lot_size": 1,
            "currency": "USD",
            "margin_rate": None,
            "fractional": False,
            "known_spec": False,
            "warning": f"Futures symbol {raw_upper!r} cannot be overridden to asset_class={explicit_ac!r}.",
            "unit_note": f"Futures symbol {raw_upper!r} conflicts with asset_class={explicit_ac!r}.",
        })

    if resolved_ac == "commodity":
        key = norm_sym.upper()
        if not key.endswith("=F") and f"{key}=F" in OVERSEAS_FUTURE_SPECS:
            key = f"{key}=F"
        spec = OVERSEAS_FUTURE_SPECS.get(key)
        if spec is not None:
            return _enrich_spec({"symbol": key, "known_spec": True, **spec})
        root = key[:-2] if key.endswith("=F") else key
        return _enrich_spec({
            "symbol": key,
            "root": root,
            "asset_class": "commodity",
            "multiplier": None,
            "min_order_qty": 1,
            "lot_step": 1,
            "lot_size": 1,
            "currency": "USD",
            "margin_rate": None,
            "fractional": False,
            "known_spec": False,
            "unit_note": f"Unknown overseas futures symbol {key!r}; explicit multiplier required.",
        })

    if resolved_ac == "a_share":
        a_code = norm_sym.split(".", 1)[0]
        if a_code.startswith("688") and (norm_sym.endswith(".SS") or "." not in norm_sym):
            min_qty = 200
            step = 1
            note = "A-share STAR Market equity (CNY/share, min 200 shares, 1-share increment)"
        elif norm_sym.endswith(".BJ") or a_code.startswith(("4", "8", "92")):
            min_qty = 100
            step = 1
            note = "A-share BSE equity (CNY/share, min 100 shares, 1-share increment)"
        else:
            min_qty = 100
            step = 100
            note = "A-share cash equity (CNY/share, 100-share board lot)"
        return _enrich_spec({
            "symbol": norm_sym,
            "root": norm_sym,
            "asset_class": "a_share",
            "multiplier": 1.0,
            "min_order_qty": min_qty,
            "lot_step": step,
            "lot_size": step,
            "currency": "CNY",
            "margin_rate": 1.0,
            "fractional": False,
            "known_spec": True,
            "unit_note": note,
        })

    if resolved_ac == "us_stock_or_etf":
        return _enrich_spec({
            "symbol": norm_sym,
            "root": norm_sym,
            "asset_class": "us_stock_or_etf",
            "multiplier": 1.0,
            "min_order_qty": 1,
            "lot_step": 1,
            "lot_size": 1,
            "currency": "USD",
            "margin_rate": 1.0,
            "fractional": False,
            "known_spec": True,
            "unit_note": "US equity/ETF (USD/share, 1-share lot)",
        })

    if resolved_ac == "fx":
        base_ccy, quote_ccy = _resolve_fx_currencies(norm_sym)
        return _enrich_spec({
            "symbol": norm_sym,
            "root": norm_sym,
            "asset_class": "fx",
            "multiplier": 1.0,
            "min_order_qty": 1000,
            "lot_step": 1000,
            "lot_size": 1000,
            "base_currency": base_ccy,
            "currency": quote_ccy,
            "quote_currency": quote_ccy,
            "margin_rate": 0.05,
            "fractional": False,
            "known_spec": True,
            "unit_note": (
                f"FX spot/CFD notional base units (1,000-unit micro lot; PnL denominated in {quote_ccy})"
            ),
        })

    if resolved_ac == "crypto":
        return _enrich_spec({
            "symbol": norm_sym,
            "root": norm_sym.replace("-USD", ""),
            "asset_class": "crypto",
            "multiplier": 1.0,
            "min_order_qty": 0.001,
            "lot_step": 0.001,
            "lot_size": 0.001,
            "currency": "USD",
            "margin_rate": 1.0,
            "fractional": True,
            "known_spec": True,
            "unit_note": "Crypto spot/linear contract (USD/coin, 0.001 fractional lot)",
        })

    # Macro indices (e.g. DX-Y.NYB, ^VIX) require explicit trading vehicle multiplier
    return _enrich_spec({
        "symbol": norm_sym,
        "root": norm_sym,
        "asset_class": resolved_ac,
        "multiplier": None,
        "min_order_qty": 1,
        "lot_step": 1,
        "lot_size": 1,
        "currency": "USD",
        "margin_rate": None,
        "fractional": False,
        "known_spec": False,
        "unit_note": f"Macro index {norm_sym!r} is not directly tradeable; specify vehicle multiplier explicitly.",
    })


# ---------------------------------------------------------------------------
# 2. Trade Expression & Carry Analysis (Backwardation NEVER Vetoes Shorting)
# ---------------------------------------------------------------------------

def _normalize_curve_structure(raw_structure: Any, roll_yield_pct: float | None) -> str:
    s = str(raw_structure or "").strip().lower()
    if "backwardation" in s or "现货升水" in s:
        return "Backwardation"
    if "contango" in s or "远月升水" in s:
        return "Contango"
    if "flat" in s or "平坦" in s:
        return "Flat"
    if roll_yield_pct is not None:
        if roll_yield_pct > 0.2:
            return "Backwardation"
        if roll_yield_pct < -0.2:
            return "Contango"
        return "Flat"
    return "unknown"


def _extract_crypto_annualized_funding(crypto_funding: dict[str, Any] | None) -> tuple[float | None, list[str]]:
    if not isinstance(crypto_funding, dict) or not crypto_funding:
        return None, []
    direct = _opt_finite_float(crypto_funding.get("funding_annualized_pct"))
    if direct is not None:
        return direct, ["direct"]
    direct_8h = _opt_finite_float(crypto_funding.get("funding_rate_8h_pct"))
    if direct_8h is not None:
        return round(direct_8h * 3.0 * 365.0, 4), ["direct_8h"]

    vals: list[float] = []
    exchanges: list[str] = []
    for ex_name, payload in crypto_funding.items():
        if not isinstance(payload, dict) or payload.get("error"):
            continue
        ann = _opt_finite_float(payload.get("funding_annualized_pct"))
        if ann is None:
            r8 = _opt_finite_float(payload.get("funding_rate_8h_pct"))
            if r8 is not None:
                ann = r8 * 3.0 * 365.0
        if ann is not None:
            vals.append(ann)
            exchanges.append(str(ex_name))
    if not vals:
        return None, []
    return round(sum(vals) / len(vals), 4), exchanges


def _extract_fx_rate_diff_pct(rate_diff: dict[str, Any] | None) -> tuple[float | None, str | None]:
    if not isinstance(rate_diff, dict) or not rate_diff:
        return None, None
    # Prefer shorter-tenor / policy differential when available, then 10Y benchmark
    pct_keys = (
        "policy_rate_diff_pct",
        "base_minus_quote_2y_pct",
        "us_minus_cn_2y_pct",
        "base_minus_quote_10y_pct",
        "us_minus_cn_10y_pct",
        "rate_diff_pct",
    )
    for k in pct_keys:
        val = _opt_finite_float(rate_diff.get(k))
        if val is not None:
            return val, k
    bp_keys = (
        "base_minus_quote_2y_bp",
        "us_minus_cn_2y_bp",
        "base_minus_quote_10y_bp",
        "us_minus_cn_10y_bp",
        "rate_diff_bp",
    )
    for k in bp_keys:
        val = _opt_finite_float(rate_diff.get(k))
        if val is not None:
            return round(val / 100.0, 4), k
    return None, None


def evaluate_expression_and_carry(
    direction: str,
    asset_class: str,
    term_structure: dict[str, Any] | None = None,
    rate_diff: dict[str, Any] | None = None,
    crypto_funding: dict[str, Any] | None = None,
    holding_horizon_days: int = 90,
    expected_move_pct: float | None = None,
    stop_distance_pct: float | None = None,
) -> dict[str, Any]:
    """Evaluate carry / roll-yield impact and optimal contract/instrument expression.

    HARD SURVIVAL RULE: Commodity Backwardation (大B结构 / 短期低库存) is NEVER a veto
    on shorting. It explicitly tags ``divergence_regime='strong_reality_weak_expectation'``,
    quantifies roll drag over ``holding_horizon_days`` using front-aware / most-binding
    carry, guards stale/error curve data, and recommends front vs far-month expression.
    """
    norm_dir = str(direction or "").strip().lower()
    if norm_dir not in VALID_DIRECTIONS:
        raise ValueError(f"direction must be 'long' or 'short', got {direction!r}")
    norm_ac = str(asset_class or "").strip().lower()
    if norm_ac not in VALID_ASSET_CLASSES:
        raise ValueError(f"Unsupported asset_class {asset_class!r}")
    horizon_days = _require_finite_float(holding_horizon_days, "holding_horizon_days", positive=True)
    exp_move = (
        _require_finite_float(expected_move_pct, "expected_move_pct", positive=True)
        if expected_move_pct is not None
        else None
    )
    stop_dist = (
        _require_finite_float(stop_distance_pct, "stop_distance_pct", positive=True)
        if stop_distance_pct is not None
        else None
    )

    def _finish_carry(ann_carry: float | None) -> tuple[float | None, float | None, float | None, float | None]:
        if ann_carry is None:
            return None, None, None, None
        ann_rounded = round(float(ann_carry), 4)
        hor = round(ann_rounded * (horizon_days / 365.0), 4)
        ratio = round(hor / exp_move, 4) if (exp_move is not None and exp_move > 0) else None
        carry_adj_rr = (
            round((exp_move + hor) / stop_dist, 4)
            if (exp_move is not None and stop_dist is not None and stop_dist > 0)
            else None
        )
        return ann_rounded, hor, ratio, carry_adj_rr

    # --- Commodities (CN futures & overseas futures) ---
    if norm_ac in {"commodity", "cn_future"}:
        ts = term_structure if isinstance(term_structure, dict) else {}
        ts_status = str(ts.get("status") or "").strip().lower()
        warnings: list[str] = []
        monitoring_points: list[str] = []
        curve_data_warning: str | None = None

        if ts_status in {"error", "stale"}:
            curve_data_warning = (
                f"curve_data_warning: term_structure status={ts_status!r}"
                + (f" ({ts.get('error') or ts.get('warning')})" if (ts.get("error") or ts.get("warning")) else "")
                + " — 期限结构数据异常或陈旧，严禁视作新鲜实时曲线"
            )
            warnings.append(curve_data_warning)
            return {
                "asset_class": norm_ac,
                "direction": norm_dir,
                "vetoed": False,
                "curve_data_status": ts_status,
                "curve_data_warning": curve_data_warning,
                "curve_structure": "unknown",
                "divergence_regime": "unknown_curve",
                "annualized_carry_pct": None,
                "near_annualized_carry_pct": None,
                "curve_annualized_carry_pct": None,
                "holding_horizon_days": horizon_days,
                "horizon_carry_pct": None,
                "near_horizon_carry_pct": None,
                "curve_horizon_carry_pct": None,
                "carry_to_target_ratio": None,
                "carry_adjusted_rr_ratio": None,
                "curve_carry_adjusted_rr_ratio": None,
                "near_carry_stress_rr_ratio": None,
                "expression_recommendation": "data_stale_refresh_required",
                "contract_guidance": (
                    "期限结构数据异常或已过期（stale/error），拒绝基于陈旧曲线计算展期收益或推荐具体近远月合约，"
                    "请先刷新实时期货曲线后再做合约表达决策。"
                ),
                "monitoring_points": [
                    "刷新实时期货期限结构曲线并核验近远月活跃合约报价后再评估合约表达与展期损益"
                ],
                "warnings": warnings,
                "front_contract": None,
                "far_contract": None,
            }

        roll_near = _opt_finite_float(ts.get("annualized_roll_yield_near_pct"))
        roll_curve = _opt_finite_float(ts.get("annualized_roll_yield_curve_pct"))
        if roll_near is None and roll_curve is None:
            roll_curve = _opt_finite_float(ts.get("roll_yield_pct"))
        raw_struct = ts.get("structure")

        primary_roll = roll_curve if roll_curve is not None else roll_near
        norm_struct = _normalize_curve_structure(raw_struct, primary_roll)

        # Positive roll yield = Backwardation (tailwind for long, headwind/drag for short)
        sign = 1.0 if norm_dir == "long" else -1.0
        near_carry = round(sign * roll_near, 4) if roll_near is not None else None
        curve_carry = round(sign * roll_curve, 4) if roll_curve is not None else None
        _, near_horizon_carry, _, near_carry_stress_rr = _finish_carry(near_carry)
        _, curve_horizon_carry, _, curve_carry_adj_rr = _finish_carry(curve_carry)

        # Survival Rule #1 ("大B结构绝不否决做空"): for 1Q~1Y medium-frequency trades,
        # evaluate carry-adjusted R:R using full-curve/far-month carry (falling back to near_carry
        # only when curve_carry is unavailable), while exposing near_carry_stress_rr_ratio as an
        # explicit front-month squeeze stress test so a 1-month near squeeze (e.g. CU0 -39.8% front
        # vs -5.4% curve) guides the trader into far-month contracts instead of vetoing the short!
        primary_carry_raw = curve_carry if curve_carry is not None else near_carry
        ann_carry, horizon_carry, carry_to_target, carry_adj_rr = _finish_carry(primary_carry_raw)

        # Explicitly tag "强现实 vs 弱预期" / "弱现实 vs 强预期" divergence regimes
        if norm_dir == "short" and norm_struct == "Backwardation":
            divergence_regime = "strong_reality_weak_expectation"
        elif norm_dir == "long" and norm_struct == "Contango":
            divergence_regime = "weak_reality_strong_expectation"
        elif norm_dir == "long" and norm_struct == "Backwardation":
            divergence_regime = "aligned_strong_reality_and_expectation"
        elif norm_dir == "short" and norm_struct == "Contango":
            divergence_regime = "aligned_weak_reality_and_expectation"
        elif norm_struct == "Flat":
            if norm_dir == "short" and near_carry is not None and near_carry <= -8.0:
                divergence_regime = "strong_reality_weak_expectation"
            elif norm_dir == "long" and near_carry is not None and near_carry <= -8.0:
                divergence_regime = "weak_reality_strong_expectation"
            else:
                divergence_regime = "flat_curve_neutral_carry"
        else:
            divergence_regime = "unknown_curve"

        drags = [abs(x) for x in (ann_carry, near_carry, curve_carry) if x is not None and x < 0]
        max_ann_drag = max(drags) if drags else 0.0

        front_steep_short_back = (
            norm_dir == "short" and near_carry is not None and near_carry <= -8.0
        )
        front_steep_long_contango = (
            norm_dir == "long" and near_carry is not None and near_carry <= -8.0
        )

        if divergence_regime == "strong_reality_weak_expectation":
            if max_ann_drag >= 8.0 or front_steep_short_back:
                expr_rec = "prefer_far_month_short_or_bear_spread"
                if norm_struct == "Flat" and front_steep_short_back:
                    guidance = (
                        f"全曲线整体平坦但近月端陡峭大B结构（近月空头年化展期损耗 {near_carry:+.2f}%/年）："
                        "绝不因近月大B结构否决做空，但前段移仓损耗与近月低库存挤仓风险高，"
                        "优先评估【空远月合约】或熊市价差直接定价未来过剩并规避近月挤仓。"
                    )
                else:
                    guidance = (
                        f"强现实（大B结构/短期低库存）vs 弱预期劈叉：绝不因大B结构否决做空，"
                        f"但当前空头年化展期损耗偏陡（最紧约束 {ann_carry:+.2f}%/年，最大段约 -{max_ann_drag:.2f}%/年）。"
                        "优先评估【空远月合约】直接定价未来供需过剩并规避近月低库存挤仓与前段移仓损耗，"
                        "或在下跌动能占优时使用主力合约并预留显性展期成本预算。"
                        if ann_carry is not None
                        else "强现实（大B结构）vs 弱预期劈叉：绝不否决做空，优先评估空远月合约以规避近月挤仓与移仓损耗。"
                    )
            else:
                expr_rec = "short_active_or_far_month_with_carry_budget"
                guidance = (
                    f"强现实（大B结构）vs 弱预期劈叉：绝不因大B结构否决做空；"
                    f"当前年化展期损耗温和（{ann_carry:+.2f}%/年），可在主力合约或次远月表达空头并跟踪月差收敛。"
                    if ann_carry is not None
                    else "强现实（大B结构）vs 弱预期劈叉：绝不否决做空，缺失年化月差数值时优先核对近远月价差后再定合约月份。"
                )
            if front_steep_short_back:
                warnings.append(
                    f"steep_front_backwardation_squeeze_risk: 近月空头年化展期损耗达 {near_carry:+.2f}%/年，"
                    "警惕近月低库存挤仓与前段高移仓成本，优先评估远月空头或熊市价差"
                )
            monitoring_points = [
                "若大B结构向平水/Contango收敛，视为弱预期逐步兑现为弱现实的顺风确认",
                "若近月库存继续骤降且近远月大B价差暴力走阔，立即警惕近月挤仓风险（考虑移仓远月或降杠杆）",
            ]
            if carry_to_target is not None and carry_to_target <= -0.35:
                warnings.append(
                    f"周期展期损耗占目标跌幅比例达 {abs(carry_to_target) * 100:.1f}%，建议优先空远月或等待更佳30min反抽卖点"
                )
        elif divergence_regime == "weak_reality_strong_expectation":
            if max_ann_drag >= 8.0 or front_steep_long_contango:
                expr_rec = "prefer_far_month_long_or_bull_spread"
                guidance = (
                    f"弱现实（Contango/库存宽松）vs 强预期劈叉：多头面临较陡负展期损耗（约 -{max_ann_drag:.2f}%/年），"
                    "建议评估远月合约、跨期正套或等待现货端出现去库拐点再放大仓位。"
                )
                if front_steep_long_contango:
                    warnings.append(
                        f"steep_front_contango_roll_drag: 近月多头年化展期损耗达 {near_carry:+.2f}%/年，警惕前段移仓磨损"
                    )
            else:
                expr_rec = "long_active_or_far_month_with_carry_budget"
                guidance = "弱现实（Contango）vs 强预期：多头存在温和展期损耗，需将其计入持有周期收益门槛。"
            monitoring_points = ["跟踪显性库存去化与基差走强，验证Contango是否向Backwardation收敛"]
        elif divergence_regime == "aligned_strong_reality_and_expectation":
            expr_rec = "long_active_contract_positive_carry"
            guidance = "现货升水（大B结构）与多头方向一致，移仓提供正展期顺风，优先使用流动性最好的近/主力合约表达。"
            monitoring_points = ["若大B结构突然坍塌转Contango，警惕现货紧张证伪"]
        elif divergence_regime == "aligned_weak_reality_and_expectation":
            if front_steep_short_back:
                expr_rec = "prefer_far_month_short_or_bear_spread"
                guidance = (
                    f"虽远端呈Contango，但近月存在陡峭大B结构（近月空头年化损耗 {near_carry:+.2f}%/年），"
                    "警惕近月挤仓与移仓成本，优先空远月或熊市价差。"
                )
                warnings.append(
                    f"steep_front_backwardation_squeeze_risk: 近月空头年化展期损耗达 {near_carry:+.2f}%/年，警惕近月挤仓"
                )
            else:
                expr_rec = "short_active_contract_positive_carry"
                guidance = "远月升水（Contango）与空头方向一致，移仓提供正展期顺风，主力合约顺势做空表达清晰。"
            monitoring_points = ["若库存骤降导致Contango快速收窄转大B，警惕供给冲击"]
        elif divergence_regime == "flat_curve_neutral_carry":
            expr_rec = f"{norm_dir}_active_contract_neutral_carry"
            guidance = "期限结构整体平坦，展期损益影响轻微，优先选流动性最优的主力合约。"
        else:
            expr_rec = f"{norm_dir}_active_contract_verify_curve"
            guidance = "暂缺完整期限结构曲线数据，默认跟踪主力合约并在下单前人工核对近远月升贴水。"
            warnings.append("missing_term_structure_data")

        return {
            "asset_class": norm_ac,
            "direction": norm_dir,
            "vetoed": False,
            "curve_data_status": ts_status or ("ok" if ts else "missing"),
            "curve_data_warning": curve_data_warning,
            "curve_structure": norm_struct,
            "divergence_regime": divergence_regime,
            "annualized_carry_pct": ann_carry,
            "near_annualized_carry_pct": near_carry,
            "curve_annualized_carry_pct": curve_carry,
            "holding_horizon_days": horizon_days,
            "horizon_carry_pct": horizon_carry,
            "near_horizon_carry_pct": near_horizon_carry,
            "curve_horizon_carry_pct": curve_horizon_carry,
            "carry_to_target_ratio": carry_to_target,
            "carry_adjusted_rr_ratio": carry_adj_rr,
            "curve_carry_adjusted_rr_ratio": curve_carry_adj_rr,
            "near_carry_stress_rr_ratio": near_carry_stress_rr,
            "expression_recommendation": expr_rec,
            "contract_guidance": guidance,
            "monitoring_points": monitoring_points,
            "warnings": warnings,
            "front_contract": ts.get("front_contract"),
            "far_contract": ts.get("far_contract") or ts.get("back_contract"),
        }

    # --- Foreign Exchange (FX) ---
    if norm_ac == "fx":
        rd = rate_diff if isinstance(rate_diff, dict) else {}
        rd_status = str(rd.get("status") or "").strip().lower()
        if rd_status in {"error", "stale"}:
            rd_warn = (
                f"curve_data_warning: rate_differential status={rd_status!r}"
                + (f" ({rd.get('error') or rd.get('warning')})" if (rd.get("error") or rd.get("warning")) else "")
                + " — 国债/政策利差数据异常或陈旧，严禁基于陈旧利差计算 Carry"
            )
            return {
                "asset_class": norm_ac,
                "direction": norm_dir,
                "vetoed": False,
                "curve_data_status": rd_status,
                "curve_data_warning": rd_warn,
                "rate_diff_source_key": None,
                "base_minus_quote_rate_pct": None,
                "annualized_carry_pct": None,
                "holding_horizon_days": horizon_days,
                "horizon_carry_pct": None,
                "carry_to_target_ratio": None,
                "carry_adjusted_rr_ratio": None,
                "expression_recommendation": "data_stale_refresh_required",
                "contract_guidance": "外汇利差数据异常或已过期（stale/error），请刷新实时国债与政策利差后再做 Carry 与直盘/交叉盘决策。",
                "warnings": [rd_warn],
            }
        raw_diff_pct, diff_key = _extract_fx_rate_diff_pct(rate_diff)
        sign = 1.0 if norm_dir == "long" else -1.0
        ann_carry, horizon_carry, carry_to_target, carry_adj_rr = _finish_carry(
            sign * raw_diff_pct if raw_diff_pct is not None else None
        )
        warnings = []
        if ann_carry is None:
            expr_rec = f"{norm_dir}_fx_verify_carry_and_cross"
            guidance = "暂缺国债/政策利差数据；下单前核对隔夜掉期 Carry 及直盘 vs 交叉盘（是否需剔除美元 Beta）。"
            warnings.append("missing_fx_rate_differential")
        elif ann_carry >= 0:
            expr_rec = f"{norm_dir}_direct_or_cross_positive_carry"
            guidance = (
                f"当前方向享有约 {ann_carry:+.2f}%/年 正利差顺风（基于 {diff_key}）。"
                "若主逻辑为非美货币独立周期而非美元单边因子，可对比交叉盘以剔除美元 Beta 噪点。"
            )
        elif ann_carry <= -3.0:
            expr_rec = f"{norm_dir}_prefer_cross_or_catalyst_window"
            guidance = (
                f"当前方向承受较明显负 Carry 损耗（约 {ann_carry:+.2f}%/年，基于 {diff_key}）。"
                "建议优先评估交叉盘（降低对高息基准货币的隔夜贴水损耗）、聚焦央行议息/数据催化窗口，"
                "或结合期权控制时间损耗，同时紧盯 CFTC 杠杆基金套息踩踏信号。"
            )
            warnings.append("steep_negative_fx_carry")
        else:
            expr_rec = f"{norm_dir}_direct_pair_manageable_carry"
            guidance = (
                f"当前方向存在温和负 Carry（约 {ann_carry:+.2f}%/年，基于 {diff_key}），"
                "在趋势动能充足时可接受，同时评估直盘与交叉盘的波动率与相关性。"
            )

        return {
            "asset_class": norm_ac,
            "direction": norm_dir,
            "vetoed": False,
            "rate_diff_source_key": diff_key,
            "base_minus_quote_rate_pct": raw_diff_pct,
            "annualized_carry_pct": ann_carry,
            "holding_horizon_days": horizon_days,
            "horizon_carry_pct": horizon_carry,
            "carry_to_target_ratio": carry_to_target,
            "carry_adjusted_rr_ratio": carry_adj_rr,
            "expression_recommendation": expr_rec,
            "contract_guidance": guidance,
            "warnings": warnings,
        }

    # --- Crypto ---
    if norm_ac == "crypto":
        ann_funding, exchanges_used = _extract_crypto_annualized_funding(crypto_funding)
        # Positive funding => longs pay shorts
        sign = -1.0 if norm_dir == "long" else 1.0
        ann_carry, horizon_carry, carry_to_target, carry_adj_rr = _finish_carry(
            sign * ann_funding if ann_funding is not None else None
        )
        warnings = []
        if ann_carry is None:
            expr_rec = f"{norm_dir}_crypto_verify_funding"
            guidance = "暂缺永续资金费率数据；做多优先现货，做空/杠杆前核对永续费率与 OI 拥挤度。"
            warnings.append("missing_crypto_funding")
        elif norm_dir == "long" and ann_funding is not None and ann_funding >= 15.0:
            expr_rec = "long_spot_avoid_crowded_perp"
            guidance = (
                f"永续年化资金费率高达 {ann_funding:+.2f}%（做多永续年化损耗约 {ann_carry:+.2f}%），"
                "杠杆多头拥挤，优先采用现货分批表达而非高费率永续合约。"
            )
            warnings.append("crowded_long_perp_funding")
        elif norm_dir == "short" and ann_funding is not None and ann_funding <= -15.0:
            expr_rec = "short_cautious_crowded_negative_funding"
            guidance = (
                f"永续年化资金费率深度转负（{ann_funding:+.2f}%，空头反付约 {ann_carry:+.2f}%/年），"
                "空头交易拥挤、逼空风险升高，需严格控制杠杆并收紧 30min 止损。"
            )
            warnings.append("crowded_short_negative_funding")
        elif ann_carry >= 0:
            expr_rec = f"{norm_dir}_perp_or_spot_positive_carry"
            guidance = f"永续资金费率方向构成正 Carry 顺风（约 {ann_carry:+.2f}%/年），同时持续监控 OI 与 ETF 净流。"
        else:
            expr_rec = f"{norm_dir}_spot_or_low_leverage_perp"
            guidance = f"永续资金费率构成温和负 Carry（约 {ann_carry:+.2f}%/年），做多优先现货以消除资金费率摩擦。"

        return {
            "asset_class": norm_ac,
            "direction": norm_dir,
            "vetoed": False,
            "funding_annualized_pct": ann_funding,
            "exchanges_used": exchanges_used,
            "annualized_carry_pct": ann_carry,
            "holding_horizon_days": horizon_days,
            "horizon_carry_pct": horizon_carry,
            "carry_to_target_ratio": carry_to_target,
            "carry_adjusted_rr_ratio": carry_adj_rr,
            "expression_recommendation": expr_rec,
            "contract_guidance": guidance,
            "warnings": warnings,
        }

    # --- Equities & Macro ---
    _, _, _, eq_carry_adj_rr = _finish_carry(0.0)
    return {
        "asset_class": norm_ac,
        "direction": norm_dir,
        "vetoed": False,
        "annualized_carry_pct": 0.0,
        "holding_horizon_days": horizon_days,
        "horizon_carry_pct": 0.0,
        "carry_to_target_ratio": 0.0 if exp_move is not None else None,
        "carry_adjusted_rr_ratio": eq_carry_adj_rr,
        "expression_recommendation": (
            "cash_equity_staggered_long"
            if norm_dir == "long"
            else "equity_hedge_or_pair_expression_only"
        ),
        "contract_guidance": (
            "正股分批顺势建仓为主；临近财报窗口可评估领口/保护性看跌期权对冲，不碰二三线跟风标的。"
            if norm_dir == "long"
            else "股票端仅在明确组合对冲或同产业链多空配对时表达空头，严禁裸空高成长龙头。"
        ),
        "warnings": [],
    }


# ---------------------------------------------------------------------------
# 3. Four-Quadrant Final Execution Gate (Fail-Closed)
# ---------------------------------------------------------------------------

def evaluate_four_quadrant_gate(
    direction: str,
    asset_class: str,
    has_l1_l2_evidence: bool,
    pending_verifications_count: int = 0,
    thesis_falsified: bool = False,
    daily_alignment: dict[str, Any] | None = None,
    rr_ratio: float | None = None,
    min_rr: float = 2.5,
    hedge_only: bool = False,
    is_pair_trade: bool = False,
    total_quantity: float | None = None,
    within_capital_limits: bool = True,
    upstream_errors: list[str] | None = None,
    pair_leg_verified: bool = False,
    carry_adjusted_rr_ratio: float | None = None,
    curve_stale_or_error: bool = False,
) -> dict[str, Any]:
    """Enforce fail-closed Layer-4 decision gate across thesis, alignment, sizing, capital, carry, and R:R."""
    norm_dir = str(direction or "").strip().lower()
    if norm_dir == "pair":
        is_pair_trade = True
    elif norm_dir not in VALID_DIRECTIONS:
        raise ValueError(f"direction must be 'long', 'short', or 'pair', got {direction!r}")

    norm_ac = str(asset_class or "").strip().lower()
    if norm_ac not in VALID_ASSET_CLASSES:
        raise ValueError(f"Unsupported asset_class {asset_class!r}")
    if (
        isinstance(pending_verifications_count, bool)
        or not isinstance(pending_verifications_count, int)
        or pending_verifications_count < 0
    ):
        raise ValueError(
            f"pending_verifications_count must be a non-negative int, got {pending_verifications_count!r}"
        )
    min_rr_val = _require_finite_float(min_rr, "min_rr", positive=True)
    rr_val = _require_finite_float(rr_ratio, "rr_ratio", non_negative=True) if rr_ratio is not None else None
    carry_adj_rr_val = (
        _require_finite_float(carry_adjusted_rr_ratio, "carry_adjusted_rr_ratio")
        if carry_adjusted_rr_ratio is not None
        else None
    )
    qty_val = (
        _require_finite_float(total_quantity, "total_quantity", non_negative=True)
        if total_quantity is not None
        else None
    )
    up_errors = [str(e).strip() for e in (upstream_errors or []) if str(e).strip()]
    if curve_stale_or_error and "curve_stale_or_error" not in up_errors:
        up_errors.append("curve_stale_or_error")

    alignment_dict = daily_alignment if isinstance(daily_alignment, dict) else None
    alignment_status = str(alignment_dict.get("alignment_status") or "").strip() if alignment_dict else ""
    daily_bias = str(alignment_dict.get("daily_bias") or "").strip().lower() if alignment_dict else ""
    intraday_action = str(alignment_dict.get("intraday_action") or "").strip().lower() if alignment_dict else ""

    # Fail-closed dual-timeframe verification: ALL THREE fields must be explicitly present & aligned
    if norm_dir in VALID_DIRECTIONS:
        daily_bias_matches = bool(daily_bias) and (daily_bias == norm_dir)
        intraday_action_matches = bool(intraday_action) and (intraday_action == f"entry_{norm_dir}")
    else:
        daily_bias_matches = daily_bias in VALID_DIRECTIONS
        intraday_action_matches = bool(daily_bias) and (intraday_action == f"entry_{daily_bias}")

    alignment_verified = (
        alignment_dict is not None
        and alignment_status == "aligned_trigger"
        and daily_bias_matches
        and intraday_action_matches
    )
    sizing_ok = (qty_val is not None and qty_val > 0) and bool(within_capital_limits)

    checks = {
        "thesis_falsified": bool(thesis_falsified),
        "equity_short_allowed": not (
            norm_ac in {"us_stock_or_etf", "a_share"} and norm_dir == "short" and not hedge_only and not is_pair_trade
        ),
        "upstream_errors": up_errors,
        "curve_stale_or_error": bool(curve_stale_or_error),
        "upstream_ok": len(up_errors) == 0,
        "total_quantity": qty_val,
        "within_capital_limits": bool(within_capital_limits),
        "sizing_ok": sizing_ok,
        "has_l1_l2_evidence": bool(has_l1_l2_evidence),
        "pending_verifications_ok": pending_verifications_count < 2,
        "pair_leg_verified": bool(pair_leg_verified) if is_pair_trade else None,
        "daily_alignment_status": alignment_status or None,
        "daily_bias": daily_bias or None,
        "intraday_action": intraday_action or None,
        "alignment_verified": alignment_verified,
        "rr_ratio": rr_val,
        "carry_adjusted_rr_ratio": carry_adj_rr_val,
        "min_rr": min_rr_val,
        "rr_ok": (rr_val >= min_rr_val) if rr_val is not None else False,
        "carry_adjusted_rr_ok": (carry_adj_rr_val >= min_rr_val) if carry_adj_rr_val is not None else None,
    }

    if thesis_falsified:
        return {
            "decision": "rejected",
            "actionable": False,
            "direction": norm_dir,
            "asset_class": norm_ac,
            "reason": "核心研究论点已被 L1 硬证据证伪，四象限终裁否决",
            "reasons": ["核心研究论点已被 L1 硬证据证伪，四象限终裁否决"],
            "checks": checks,
        }

    if norm_ac in {"us_stock_or_etf", "a_share"} and norm_dir == "short" and not hedge_only and not is_pair_trade:
        msg = "股票中长线以做多为主，不直接做空（除非明确对冲/配对）"
        return {
            "decision": "rejected",
            "actionable": False,
            "direction": norm_dir,
            "asset_class": norm_ac,
            "reason": msg,
            "reasons": [msg],
            "checks": checks,
        }

    if up_errors:
        msg = f"上游行情/缠论/曲线抓取存在异常（{len(up_errors)} 项），默认拒批以防脏数据误触发：{'；'.join(up_errors)}"
        return {
            "decision": "watchlist_upstream_error",
            "actionable": False,
            "direction": norm_dir,
            "asset_class": norm_ac,
            "reason": msg,
            "reasons": [msg],
            "checks": checks,
        }

    if (qty_val is not None and qty_val <= 0) or not within_capital_limits:
        reasons_cap: list[str] = []
        if qty_val is not None and qty_val <= 0:
            reasons_cap.append("风控/资金约束下可执行数量归零（严禁向上取整突破预算）")
        if not within_capital_limits:
            reasons_cap.append("预计保证金或名义杠杆超出账户资金上限")
        msg = "；".join(reasons_cap)
        return {
            "decision": "rejected_sizing_or_capital",
            "actionable": False,
            "direction": norm_dir,
            "asset_class": norm_ac,
            "reason": msg,
            "reasons": reasons_cap,
            "checks": checks,
        }

    evidence_reasons: list[str] = []
    if not has_l1_l2_evidence:
        evidence_reasons.append("缺少 L1/L2 一手硬证据支撑，禁止直接放行建仓")
    if pending_verifications_count >= 2:
        evidence_reasons.append(f"尚有 {pending_verifications_count} 项关键核验未完成（>=2 项），先入观察池")
    if evidence_reasons:
        return {
            "decision": "watchlist",
            "actionable": False,
            "direction": norm_dir,
            "asset_class": norm_ac,
            "reason": "；".join(evidence_reasons),
            "reasons": evidence_reasons,
            "checks": checks,
        }

    if is_pair_trade and not pair_leg_verified:
        msg = "同板块配对交易需同时完成多/空双腿验证（pair_leg_verified=True），严禁凭单腿参数放行配对"
        return {
            "decision": "watchlist_pair_incomplete",
            "actionable": False,
            "direction": norm_dir,
            "asset_class": norm_ac,
            "reason": msg,
            "reasons": [msg],
            "checks": checks,
        }

    if alignment_dict is None or not alignment_status:
        msg = "缺少日线定方向 + 30min 缠论触发对齐验证（daily_alignment 缺失），默认拒绝放行"
        return {
            "decision": "watchlist_unverified",
            "actionable": False,
            "direction": norm_dir,
            "asset_class": norm_ac,
            "reason": msg,
            "reasons": [msg],
            "checks": checks,
        }

    if alignment_status == "daily_opposes_direction":
        msg = "日线方向相反，严禁逆日线接飞刀/逆势硬扛"
        return {
            "decision": "watchlist",
            "actionable": False,
            "direction": norm_dir,
            "asset_class": norm_ac,
            "reason": msg,
            "reasons": [msg],
            "checks": checks,
        }

    if alignment_status in {"daily_ok_wait_30m", "neutral_wait"}:
        msg = (
            "日线方向确认，等待 30min 缠论结构诞生入场点，减少日线噪点浮亏"
            if alignment_status == "daily_ok_wait_30m"
            else "日线结构尚处中性震荡，继续等待日线方向与 30min 缠论结构共振"
        )
        return {
            "decision": "watchlist_wait_30m",
            "actionable": False,
            "direction": norm_dir,
            "asset_class": norm_ac,
            "reason": msg,
            "reasons": [msg],
            "checks": checks,
        }

    if alignment_status != "aligned_trigger":
        msg = f"未识别或未确认的日线/30min 对齐状态 {alignment_status!r}（仅 'aligned_trigger' 可放行），默认转入待核验观察池"
        return {
            "decision": "watchlist_unverified",
            "actionable": False,
            "direction": norm_dir,
            "asset_class": norm_ac,
            "reason": msg,
            "reasons": [msg],
            "checks": checks,
        }

    if not daily_bias_matches or not intraday_action_matches:
        msg = (
            f"大小级别对齐方向缺失或与交易方向不一致：daily_bias={daily_bias or 'MISSING'}, "
            f"intraday_action={intraday_action or 'MISSING'}, 计划方向={norm_dir}（必须三项齐全且一致）"
        )
        return {
            "decision": "watchlist_unverified",
            "actionable": False,
            "direction": norm_dir,
            "asset_class": norm_ac,
            "reason": msg,
            "reasons": [msg],
            "checks": checks,
        }

    if rr_val is None:
        msg = f"缺少明确目标位与盈亏比（rr_ratio=None），无法核验盈亏比 >= {min_rr_val:.2f}，默认拒绝放行"
        return {
            "decision": "watchlist_missing_rr",
            "actionable": False,
            "direction": norm_dir,
            "asset_class": norm_ac,
            "reason": msg,
            "reasons": [msg],
            "checks": checks,
        }

    if rr_val < min_rr_val:
        msg = f"30min 紧凑止损下盈亏比 {rr_val:.2f} 低于最低门槛 {min_rr_val:.2f}，等待更优入场位"
        return {
            "decision": "watchlist_poor_rr",
            "actionable": False,
            "direction": norm_dir,
            "asset_class": norm_ac,
            "reason": msg,
            "reasons": [msg],
            "checks": checks,
        }

    if carry_adj_rr_val is not None and carry_adj_rr_val < min_rr_val:
        msg = (
            f"计入展期/Carry 成本后的调整盈亏比 {carry_adj_rr_val:.4f} 低于最低门槛 {min_rr_val:.2f} "
            f"（名义盈亏比 {rr_val:.2f}），建议切换远月合约、熊/牛市价差或等待更佳 30min 入场位"
        )
        return {
            "decision": "watchlist_poor_carry_adjusted_rr",
            "actionable": False,
            "direction": norm_dir,
            "asset_class": norm_ac,
            "reason": msg,
            "reasons": [msg],
            "checks": checks,
        }

    if qty_val is None:
        msg = "缺少明确算仓数量（total_quantity=None），无法核验风险预算与最小交易手数，默认拒绝放行"
        return {
            "decision": "watchlist_missing_sizing",
            "actionable": False,
            "direction": norm_dir,
            "asset_class": norm_ac,
            "reason": msg,
            "reasons": [msg],
            "checks": checks,
        }

    if is_pair_trade:
        decision = "pair_trade"
        msg = "同板块多空配对四象限与双腿核验全部通过，按配对风险预算执行"
    elif norm_dir == "long":
        decision = "actionable_long"
        msg = "第三层 L1/L2 论据、日线方向、30min 触发与盈亏比全部通过，放行做多"
    else:
        decision = "actionable_short"
        msg = "第三层 L1/L2 论据、日线方向、30min 触发与盈亏比全部通过，放行做空"

    return {
        "decision": decision,
        "actionable": True,
        "direction": norm_dir,
        "asset_class": norm_ac,
        "reason": msg,
        "reasons": [msg],
        "checks": checks,
    }


# ---------------------------------------------------------------------------
# 4. Risk + Capital/Margin + Leverage Position Sizing & Exact Tranche Splitter
# ---------------------------------------------------------------------------

def _hamilton_split(total_units: int, weights: tuple[float, ...]) -> list[int]:
    """Deterministically split integer units across normalized Decimal weights with exact sum == total_units."""
    if total_units <= 0:
        return [0 for _ in weights]
    dec_weights = [Decimal(str(w)) for w in weights]
    w_sum = sum(dec_weights, Decimal("0"))
    raw = [(Decimal(total_units) * w) / w_sum for w in dec_weights]
    base = [int(x.to_integral_value(rounding=ROUND_FLOOR)) for x in raw]
    remainder = total_units - sum(base)
    if remainder > 0:
        order = sorted(range(len(weights)), key=lambda i: (-(raw[i] - base[i]), i))
        for idx in order[:remainder]:
            base[idx] += 1
    return base


def _split_steps_across_tranches(
    total_steps: int,
    min_steps: int,
    weights: tuple[float, ...],
) -> list[int]:
    """Split integer lot-step units across tranches so EVERY non-zero tranche >= min_steps,
    tranche 1 >= min_steps (whenever total_steps >= min_steps), and sum(result) == total_steps ALWAYS.
    """
    n = len(weights)
    if total_steps <= 0 or total_steps < min_steps:
        return [0 for _ in range(n)]

    # 1. Try direct proportional Hamilton split first
    direct = _hamilton_split(total_steps, weights)
    if direct[0] >= min_steps and all((x == 0 or x >= min_steps) for x in direct):
        return direct

    # 2. Collapse to the maximum executable number of active tranches (1..min(n, total_steps // min_steps))
    max_active = max(1, min(n, total_steps // min_steps))
    if max_active == 1:
        return [total_steps] + [0] * (n - 1)

    active_weights = weights[:max_active]
    active_prop = _hamilton_split(total_steps, active_weights)
    if all(x >= min_steps for x in active_prop):
        return active_prop + [0] * (n - max_active)

    # 3. Guarantee min_steps floor for each active tranche, distribute remaining surplus proportionally
    surplus = total_steps - max_active * min_steps
    surplus_split = _hamilton_split(surplus, active_weights)
    return [min_steps + s for s in surplus_split] + [0] * (n - max_active)


def _decimal_to_plain_str(val: Decimal) -> str:
    """Format a Decimal without scientific notation or redundant trailing zeros."""
    if val == 0:
        return "0"
    s = format(val.normalize(), "f")
    if "." in s:
        s = s.rstrip("0").rstrip(".")
    return s or "0"


def _lots_to_quantity_decimal(steps: int, lot_step: float | int) -> Decimal:
    return Decimal(steps) * Decimal(str(lot_step))


def _lots_to_quantity(steps: int, lot_step: float | int, fractional: bool) -> int | float:
    dec_qty = _lots_to_quantity_decimal(steps, lot_step)
    if not fractional:
        return int(dec_qty)
    return round(float(dec_qty), 8)


def calculate_position_plan(
    symbol: str,
    direction: str,
    entry_price: float,
    tactical_stop_30m: float,
    structural_stop_daily: float | None = None,
    target_price: float | None = None,
    account_equity: float = 1_000_000.0,
    account_currency: str = "USD",
    risk_budget_pct: float = 1.0,
    slot_risk_share: float = 1.0,
    tranche_weights: tuple[float, ...] | list[float] = (0.4, 0.4, 0.2),
    atr14_30m: float | None = None,
    atr14_daily: float | None = None,
    multiplier: float | None = None,
    margin_rate: float | None = None,
    fx_to_account: float | None = None,
    asset_class: str | None = None,
    max_margin_pct: float = 100.0,
    max_leverage: float | None = None,
    horizon_carry_pct: float | None = None,
    allow_equity_margin: bool = False,
) -> dict[str, Any]:
    """Calculate exact lot-safe position size bounded jointly by risk budget, margin cap, and leverage cap."""
    norm_dir = str(direction or "").strip().lower()
    if norm_dir not in VALID_DIRECTIONS:
        raise ValueError(f"direction must be 'long' or 'short', got {direction!r}")

    entry = _require_finite_float(entry_price, "entry_price", positive=True)
    stop_30m = _require_finite_float(tactical_stop_30m, "tactical_stop_30m", positive=True)
    equity = _require_finite_float(account_equity, "account_equity", positive=True)
    risk_pct = _require_finite_float(risk_budget_pct, "risk_budget_pct", positive=True)
    if risk_pct > 10.0:
        raise ValueError(f"risk_budget_pct must be <= 10.0, got {risk_budget_pct!r}")
    slot_share = _require_finite_float(slot_risk_share, "slot_risk_share", positive=True)
    if slot_share > 1.0:
        raise ValueError(f"slot_risk_share must be <= 1.0, got {slot_risk_share!r}")
    eff_max_margin_pct = _require_finite_float(max_margin_pct, "max_margin_pct", positive=True)
    if eff_max_margin_pct > 100.0:
        raise ValueError(f"max_margin_pct must be <= 100.0, got {max_margin_pct!r}")

    if norm_dir == "long" and not (entry > stop_30m):
        raise ValueError(f"Long position requires entry_price ({entry}) > tactical_stop_30m ({stop_30m})")
    if norm_dir == "short" and not (entry < stop_30m):
        raise ValueError(f"Short position requires entry_price ({entry}) < tactical_stop_30m ({stop_30m})")

    stop_daily: float | None = None
    if structural_stop_daily is not None:
        stop_daily = _require_finite_float(structural_stop_daily, "structural_stop_daily", positive=True)
        if norm_dir == "long" and not (stop_daily < entry):
            raise ValueError(f"Long structural_stop_daily ({stop_daily}) must be < entry_price ({entry})")
        if norm_dir == "short" and not (stop_daily > entry):
            raise ValueError(f"Short structural_stop_daily ({stop_daily}) must be > entry_price ({entry})")

    target: float | None = None
    if target_price is not None:
        target = _require_finite_float(target_price, "target_price", positive=True)
        if norm_dir == "long" and not (target > entry):
            raise ValueError(f"Long target_price ({target}) must be > entry_price ({entry})")
        if norm_dir == "short" and not (target < entry):
            raise ValueError(f"Short target_price ({target}) must be < entry_price ({entry})")

    if not tranche_weights:
        raise ValueError("tranche_weights must be a non-empty sequence")
    weights = tuple(_require_finite_float(w, "tranche_weight", positive=True) for w in tranche_weights)
    if abs(sum(weights) - 1.0) > 1e-6:
        raise ValueError(f"tranche_weights must sum to 1.0 (within 1e-6), got sum={sum(weights):.8f}")

    atr_30m = _require_finite_float(atr14_30m, "atr14_30m", positive=True) if atr14_30m is not None else None
    atr_d = _require_finite_float(atr14_daily, "atr14_daily", positive=True) if atr14_daily is not None else None
    carry_hor = (
        _require_finite_float(horizon_carry_pct, "horizon_carry_pct")
        if horizon_carry_pct is not None
        else None
    )

    spec = get_contract_spec(symbol, asset_class=asset_class)
    if multiplier is not None:
        eff_mult = _require_finite_float(multiplier, "multiplier", positive=True)
    else:
        if not spec.get("known_spec") or spec.get("multiplier") is None:
            raise ValueError(
                f"Unknown contract specification for {symbol!r} (asset_class={spec['asset_class']}); "
                "pass exchange-suffixed symbol or explicit --multiplier / multiplier instead of guessing."
            )
        eff_mult = float(spec["multiplier"])

    if margin_rate is not None:
        eff_margin = _require_finite_float(margin_rate, "margin_rate", positive=True)
        if eff_margin > 1.0:
            raise ValueError(f"margin_rate must be <= 1.0, got {margin_rate!r}")
    else:
        eff_margin = float(spec["margin_rate"]) if spec.get("margin_rate") is not None else 1.0

    # Cross-currency FX protection: fail closed when quote_currency != account_currency and fx_to_account omitted
    acct_ccy = (account_currency or "").strip().upper()
    fx_rate = resolve_fx_to_account(spec, acct_ccy, fx_to_account)
    quote_ccy = str(spec.get("quote_currency") or spec.get("currency") or "").strip().upper()

    # Leverage cap: default 1.0x for cash equities, unbounded (None) for margin derivatives unless specified
    if max_leverage is not None:
        eff_max_leverage: float | None = _require_finite_float(max_leverage, "max_leverage", positive=True)
    elif spec["asset_class"] in {"us_stock_or_etf", "a_share"}:
        eff_max_leverage = 1.0
    else:
        eff_max_leverage = None

    # Cash-equity margin/leverage guard: forbid implicit margin_rate < 1.0 or max_leverage > 1.0
    if spec["asset_class"] in {"us_stock_or_etf", "a_share"}:
        if (eff_margin < 1.0 or (eff_max_leverage is not None and eff_max_leverage > 1.0)) and not allow_equity_margin:
            raise ValueError(
                "equity_margin_forbidden: 现金股票严禁隐式使用 margin_rate < 1.0 或 max_leverage > 1.0，"
                "若确需融资请显式传 allow_equity_margin=True / --allow-equity-margin"
            )

    min_order_qty = spec["min_order_qty"]
    lot_step = spec["lot_step"]
    fractional = bool(spec["fractional"])

    risk_budget_amount = equity * (risk_pct / 100.0) * slot_share
    margin_budget_amount = equity * (eff_max_margin_pct / 100.0)
    leverage_budget_amount = (equity * eff_max_leverage) if eff_max_leverage is not None else None

    stop_distance_30m = abs(entry - stop_30m)
    stop_distance_30m_pct = (stop_distance_30m / entry) * 100.0
    risk_per_unit_account = stop_distance_30m * eff_mult * fx_rate
    notional_per_unit_account = entry * eff_mult * fx_rate
    margin_per_unit_account = notional_per_unit_account * eff_margin
    single_lot_risk_amount = risk_per_unit_account * float(min_order_qty)

    # Joint sizing caps: risk budget, margin/cash cap, and leverage cap
    max_qty_by_risk = risk_budget_amount / risk_per_unit_account
    max_qty_by_margin = margin_budget_amount / margin_per_unit_account
    max_qty_by_leverage = (
        (leverage_budget_amount / notional_per_unit_account)
        if leverage_budget_amount is not None
        else None
    )

    cap_candidates = [max_qty_by_risk, max_qty_by_margin]
    if max_qty_by_leverage is not None:
        cap_candidates.append(max_qty_by_leverage)
    effective_raw_qty = min(cap_candidates)

    # Quantize down to exact valid order quantity: 0 or (min_order_qty + k * lot_step)
    dec_min = Decimal(str(min_order_qty))
    dec_step = Decimal(str(lot_step))
    min_steps = int((dec_min / dec_step).to_integral_value())

    if effective_raw_qty + 1e-12 < float(min_order_qty):
        total_steps = 0
    else:
        k_steps = int(math.floor(((effective_raw_qty - float(min_order_qty)) / float(lot_step)) + 1e-12))
        total_steps = (min_steps + k_steps) if k_steps >= 0 else 0

    def _steps_exceed_any_cap(steps_val: int) -> bool:
        if steps_val <= 0:
            return False
        q = float(_lots_to_quantity(steps_val, lot_step, fractional))
        if q * risk_per_unit_account > risk_budget_amount * (1.0 + 1e-9):
            return True
        if q * margin_per_unit_account > margin_budget_amount * (1.0 + 1e-9):
            return True
        if leverage_budget_amount is not None and q * notional_per_unit_account > leverage_budget_amount * (1.0 + 1e-9):
            return True
        return False

    while total_steps > 0 and _steps_exceed_any_cap(total_steps):
        total_steps -= 1
        if total_steps < min_steps:
            total_steps = 0

    total_qty_dec = _lots_to_quantity_decimal(total_steps, lot_step)
    total_quantity = _lots_to_quantity(total_steps, lot_step, fractional)
    total_quantity_str = _decimal_to_plain_str(total_qty_dec)
    actual_risk_amount = float(total_qty_dec) * risk_per_unit_account
    actual_risk_pct = (actual_risk_amount / equity) * 100.0
    notional_value_quote = float(total_qty_dec) * entry * eff_mult
    notional_value_account = notional_value_quote * fx_rate
    estimated_margin_account = notional_value_account * eff_margin
    margin_pct_of_equity = (estimated_margin_account / equity) * 100.0

    within_capital_limits = (
        estimated_margin_account <= margin_budget_amount + 1e-6
        and (leverage_budget_amount is None or notional_value_account <= leverage_budget_amount + 1e-6)
    )

    warnings: list[str] = []
    oversized_single_lot = total_steps == 0
    if oversized_single_lot:
        sizing_binding_constraint = "oversized_single_lot"
        if max_qty_by_risk + 1e-12 < float(min_order_qty):
            warnings.append(
                f"最小交易单位（min_order_qty = {min_order_qty}）单笔风险 {single_lot_risk_amount:,.2f} "
                f"已超过本次风险预算 {risk_budget_amount:,.2f}，严禁向上取整突破风控，数量归零（放弃或换更小合约/收紧30min止损）。"
            )
        else:
            warnings.append(
                f"最小交易单位（min_order_qty = {min_order_qty}）所需保证金/名义价值超过账户资金/杠杆上限，"
                "严禁突破资金约束，数量归零。"
            )
    else:
        if max_qty_by_leverage is not None and max_qty_by_leverage < max_qty_by_risk - 1e-9 and max_qty_by_leverage < max_qty_by_margin - 1e-9:
            sizing_binding_constraint = "leverage_cap"
            warnings.append(
                f"仓位受最大杠杆上限（{eff_max_leverage:.2f}x）约束截断至 {total_quantity}，低于风控预算理论数量 {max_qty_by_risk:,.2f}。"
            )
        elif max_qty_by_margin < max_qty_by_risk - 1e-9:
            sizing_binding_constraint = "margin_cap"
            warnings.append(
                f"仓位受保证金/现金上限（{eff_max_margin_pct:.1f}% 权益）约束截断至 {total_quantity}，低于风控预算理论数量 {max_qty_by_risk:,.2f}。"
            )
        else:
            sizing_binding_constraint = "risk_budget"

    if margin_pct_of_equity > 50.0:
        warnings.append(
            f"预计保证金/资金占用达账户权益 {margin_pct_of_equity:.1f}%，注意流动性与组合保证金压力。"
        )

    rr_ratio: float | None = None
    expected_move_pct: float | None = None
    carry_adjusted_rr_ratio: float | None = None
    if target is not None:
        reward_distance = abs(target - entry)
        rr_ratio = round(reward_distance / stop_distance_30m, 4)
        expected_move_pct = round((reward_distance / entry) * 100.0, 4)
        if carry_hor is not None and stop_distance_30m_pct > 0:
            carry_adjusted_rr_ratio = round((expected_move_pct + carry_hor) / stop_distance_30m_pct, 4)

    daily_structural_risk_amount: float | None = None
    daily_vs_30m_risk_ratio: float | None = None
    noise_risk_saved_amount: float | None = None
    if stop_daily is not None:
        stop_distance_daily = abs(entry - stop_daily)
        daily_structural_risk_amount = round(float(total_quantity) * stop_distance_daily * eff_mult * fx_rate, 2)
        daily_vs_30m_risk_ratio = round(stop_distance_daily / stop_distance_30m, 4)
        noise_risk_saved_amount = round(max(0.0, daily_structural_risk_amount - actual_risk_amount), 2)
        if stop_distance_daily < stop_distance_30m:
            warnings.append("日线结构止损距窄于 30min 战术止损距，请核对两级缠论止损锚点是否正确。")

    stop_vs_atr30m: float | None = None
    if atr_30m is not None:
        stop_vs_atr30m = round(stop_distance_30m / atr_30m, 4)
        if stop_vs_atr30m < 0.5:
            warnings.append(f"30min 止损距仅 {stop_vs_atr30m:.2f}x ATR(30m)，过窄易被盘中噪点扫损。")
        elif stop_vs_atr30m > 3.5:
            warnings.append(f"30min 止损距达 {stop_vs_atr30m:.2f}x ATR(30m)，偏宽，可等待回踩更优 30min 分型。")

    stop_vs_atr_daily: float | None = None
    if atr_d is not None:
        stop_vs_atr_daily = round(stop_distance_30m / atr_d, 4)

    tranche_steps = _split_steps_across_tranches(total_steps, min_steps, weights)
    tranches: list[dict[str, Any]] = []
    tranche_qty_dec_sum = Decimal("0")
    for idx, (w, t_steps) in enumerate(zip(weights, tranche_steps, strict=True), start=1):
        t_qty_dec = _lots_to_quantity_decimal(t_steps, lot_step)
        tranche_qty_dec_sum += t_qty_dec
        t_qty = _lots_to_quantity(t_steps, lot_step, fractional)
        rule = (
            "30min 缠论结构诞生精确买卖点首仓试探入场"
            if idx == 1
            else "仅允许浮盈确认后顺势加仓，严禁浮亏摊平"
        )
        tranches.append(
            {
                "tranche_index": idx,
                "weight": round(w, 4),
                "lots": t_steps,
                "quantity": t_qty,
                "quantity_str": _decimal_to_plain_str(t_qty_dec),
                "risk_amount": round(float(t_qty_dec) * risk_per_unit_account, 2),
                "notional_account": round(float(t_qty_dec) * entry * eff_mult * fx_rate, 2),
                "rule": rule,
            }
        )
    tranche_steps_sum = sum(tranche_steps)
    tranche_qty_sum_exact = (
        tranche_steps_sum == total_steps
        and tranche_qty_dec_sum == total_qty_dec
        and sum((Decimal(str(t["quantity"])) for t in tranches), Decimal("0")) == Decimal(str(total_quantity))
    )

    return {
        "symbol": spec["symbol"],
        "asset_class": spec["asset_class"],
        "direction": norm_dir,
        "account_currency": acct_ccy,
        "quote_currency": quote_ccy,
        "contract_spec": {
            **spec,
            "effective_multiplier": eff_mult,
            "effective_margin_rate": eff_margin,
        },
        "entry_price": round(entry, 6),
        "tactical_stop_30m": round(stop_30m, 6),
        "structural_stop_daily": round(stop_daily, 6) if stop_daily is not None else None,
        "target_price": round(target, 6) if target is not None else None,
        "stop_distance_30m": round(stop_distance_30m, 6),
        "stop_distance_30m_pct": round(stop_distance_30m_pct, 4),
        "account_equity": round(equity, 2),
        "risk_budget_pct": round(risk_pct, 4),
        "slot_risk_share": round(slot_share, 4),
        "max_margin_pct": round(eff_max_margin_pct, 4),
        "max_leverage": round(eff_max_leverage, 4) if eff_max_leverage is not None else None,
        "fx_to_account": round(fx_rate, 6),
        "risk_budget_amount": round(risk_budget_amount, 2),
        "risk_per_unit_account": round(risk_per_unit_account, 6),
        "single_lot_risk_amount": round(single_lot_risk_amount, 2),
        "min_order_qty": min_order_qty,
        "lot_step": lot_step,
        "lot_size": lot_step,
        "total_lots": total_steps,
        "total_quantity": total_quantity,
        "total_quantity_str": total_quantity_str,
        "tranche_steps_sum": tranche_steps_sum,
        "tranche_qty_sum_exact": tranche_qty_sum_exact,
        "max_qty_by_risk": round(max_qty_by_risk, 6),
        "max_qty_by_margin": round(max_qty_by_margin, 6),
        "max_qty_by_leverage": round(max_qty_by_leverage, 6) if max_qty_by_leverage is not None else None,
        "sizing_binding_constraint": sizing_binding_constraint,
        "within_capital_limits": within_capital_limits,
        "oversized_single_lot": oversized_single_lot,
        "actual_risk_amount": round(actual_risk_amount, 2),
        "actual_risk_pct": round(actual_risk_pct, 4),
        "notional_value_quote": round(notional_value_quote, 2),
        "notional_value_account": round(notional_value_account, 2),
        "estimated_margin_account": round(estimated_margin_account, 2),
        "margin_pct_of_equity": round(margin_pct_of_equity, 4),
        "rr_ratio": rr_ratio,
        "carry_adjusted_rr_ratio": carry_adjusted_rr_ratio,
        "expected_move_pct": expected_move_pct,
        "daily_structural_risk_amount": daily_structural_risk_amount,
        "daily_vs_30m_risk_ratio": daily_vs_30m_risk_ratio,
        "noise_risk_saved_amount": noise_risk_saved_amount,
        "stop_vs_atr30m": stop_vs_atr30m,
        "stop_vs_atr_daily": stop_vs_atr_daily,
        "tranches": tranches,
        "warnings": warnings,
    }


# ---------------------------------------------------------------------------
# 5. Live Data Bridge & CLI
# ---------------------------------------------------------------------------

def _fetch_live_context(symbol: str) -> dict[str, Any]:
    """Optional live market + research context loader with graceful degradation."""
    out: dict[str, Any] = {"snapshot": None, "research": None, "errors": []}
    try:
        from market_snapshot import get_snapshot

        sig = inspect.signature(get_snapshot)
        kwargs: dict[str, Any] = {"include_macro": False, "include_fundamentals": False}
        if "include_intraday" in sig.parameters:
            kwargs["include_intraday"] = True
        out["snapshot"] = get_snapshot(symbol, **kwargs)
    except Exception as exc:  # noqa: BLE001
        out["errors"].append(f"market_snapshot_error: {exc}")

    try:
        from research_data import collect_research_data

        out["research"] = collect_research_data(symbol, include_macro=False)
    except Exception as exc:  # noqa: BLE001
        out["errors"].append(f"research_data_error: {exc}")
    return out


def _parse_bar_timestamp(val: Any) -> tuple[datetime | None, date | None, bool]:
    """Parse bar timestamp into ``(dt_utc, local_date, has_time)``."""
    if val is None or isinstance(val, bool):
        return None, None, False
    if isinstance(val, datetime):
        if val.tzinfo is not None:
            return val.astimezone(timezone.utc), val.date(), True
        return val.replace(tzinfo=timezone.utc), val.date(), True
    if isinstance(val, date):
        return datetime(val.year, val.month, val.day, tzinfo=timezone.utc), val, False

    s = str(val).strip()
    if not s or s.lower() in {"none", "null", "nan", "nat", "n/a", "na"}:
        return None, None, False

    m_date = re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})", s)
    if m_date:
        try:
            d = date(int(m_date.group(1)), int(m_date.group(2)), int(m_date.group(3)))
            return datetime(d.year, d.month, d.day, tzinfo=timezone.utc), d, False
        except ValueError:
            return None, None, False

    m_date8 = re.fullmatch(r"(\d{4})(\d{2})(\d{2})", s)
    if m_date8:
        try:
            d = date(int(m_date8.group(1)), int(m_date8.group(2)), int(m_date8.group(3)))
            return datetime(d.year, d.month, d.day, tzinfo=timezone.utc), d, False
        except ValueError:
            return None, None, False

    s_norm = s
    if re.search(r"\s+(?:UTC|GMT)$", s_norm, flags=re.IGNORECASE):
        s_norm = re.sub(r"\s+(?:UTC|GMT)$", "+00:00", s_norm, flags=re.IGNORECASE)
    elif s_norm.endswith(("Z", "z")):
        s_norm = s_norm[:-1] + "+00:00"

    m_compact_tz = re.search(r"([+-])(\d{2})(\d{2})$", s_norm)
    if m_compact_tz:
        s_norm = (
            s_norm[: m_compact_tz.start()]
            + f"{m_compact_tz.group(1)}{m_compact_tz.group(2)}:{m_compact_tz.group(3)}"
        )

    try:
        dt = datetime.fromisoformat(s_norm)
        if dt.tzinfo is not None:
            return dt.astimezone(timezone.utc), dt.date(), True
        return dt.replace(tzinfo=timezone.utc), dt.date(), True
    except ValueError:
        return None, None, False


def _extract_source_status(entry: Any) -> tuple[str, str]:
    if isinstance(entry, dict):
        st = str(entry.get("status") or "").strip().lower()
        detail = str(entry.get("error") or entry.get("warning") or "").strip()
        return st, detail
    if isinstance(entry, str):
        return entry.strip().lower(), ""
    return "", ""


def _is_market_in_regular_session(eff_now_utc: datetime, asset_class: str | None) -> bool:
    """Return True if ``eff_now_utc`` falls inside active regular trading hours for ``asset_class``.

    - ``crypto``: 24/7 (always True).
    - ``us_stock_or_etf``: Mon-Fri 09:30-16:00 America/New_York.
    - ``a_share``: Mon-Fri 09:30-11:30 or 13:00-15:00 Asia/Shanghai.
    - ``cn_future``: Mon-Fri 09:00-11:30, 13:30-15:00, 21:00-24:00 or Tue-Sat 00:00-02:30 Asia/Shanghai.
    - ``commodity`` / ``fx``: Sun 18:00 through Fri 17:00 America/New_York (excluding daily 17:00-18:00 break).
    """
    norm_ac = str(asset_class or "").strip().lower()
    if norm_ac == "crypto":
        return True
    if norm_ac == "us_stock_or_etf":
        ny = eff_now_utc.astimezone(ZoneInfo("America/New_York"))
        if ny.weekday() >= 5:
            return False
        hm = (ny.hour, ny.minute)
        return (9, 30) <= hm < (16, 0)
    if norm_ac == "a_share":
        sh = eff_now_utc.astimezone(ZoneInfo("Asia/Shanghai"))
        if sh.weekday() >= 5:
            return False
        hm = (sh.hour, sh.minute)
        return ((9, 30) <= hm < (11, 30)) or ((13, 0) <= hm < (15, 0))
    if norm_ac == "cn_future":
        sh = eff_now_utc.astimezone(ZoneInfo("Asia/Shanghai"))
        wd = sh.weekday()  # Mon=0 .. Sun=6
        hm = (sh.hour, sh.minute)
        if wd <= 4:
            if ((9, 0) <= hm < (11, 30)) or ((13, 30) <= hm < (15, 0)) or (hm >= (21, 0)):
                return True
        if 1 <= wd <= 5 and hm < (2, 30):
            return True
        return False
    if norm_ac in {"commodity", "fx"}:
        ny = eff_now_utc.astimezone(ZoneInfo("America/New_York"))
        wd = ny.weekday()
        hm = (ny.hour, ny.minute)
        if wd == 5:  # Saturday closed
            return False
        if wd == 6:  # Sunday opens 18:00 ET
            return hm >= (18, 0)
        if wd == 4:  # Friday closes 17:00 ET
            return hm < (17, 0)
        # Mon-Thu: 23h trading excluding 17:00-18:00 daily maintenance
        return not ((17, 0) <= hm < (18, 0))
    return True


def _check_live_upstream_health(
    snap: dict[str, Any] | None,
    res: dict[str, Any] | None,
    asset_class: str | None,
    *,
    now_utc: datetime | None = None,
) -> tuple[list[str], bool, bool]:
    """Inspect live snapshot & research sources_status for unclosed bars, stale timestamps, or upstream errors."""
    errors: list[str] = []
    curve_bad = False
    rate_diff_bad = False

    if now_utc is None:
        eff_now = datetime.now(timezone.utc)
    elif now_utc.tzinfo is not None:
        eff_now = now_utc.astimezone(timezone.utc)
    else:
        eff_now = now_utc.replace(tzinfo=timezone.utc)

    norm_ac = str(asset_class or "").strip().lower()
    max_stale_days = 1.0 if norm_ac == "crypto" else 4.0
    max_intraday_stale_hours = 2.0 if norm_ac == "crypto" else 4.0
    in_session = _is_market_in_regular_session(eff_now, norm_ac)
    future_tolerance = timedelta(minutes=5)

    def _is_missing_ts(val: Any) -> bool:
        return val is None or (isinstance(val, str) and not val.strip())

    snap_dict = snap if isinstance(snap, dict) else {}
    raw_tech = snap_dict.get("technicals")
    tech: dict[str, Any] = raw_tech if isinstance(raw_tech, dict) else {}
    raw_intra_30m = snap_dict.get("intraday_30m")
    intraday_30m: dict[str, Any] = raw_intra_30m if isinstance(raw_intra_30m, dict) else {}
    raw_intra_tech = snap_dict.get("intraday_technicals")
    if isinstance(raw_intra_tech, dict):
        intraday_tech: dict[str, Any] = raw_intra_tech
    else:
        raw_nested_tech = intraday_30m.get("technicals")
        intraday_tech = raw_nested_tech if isinstance(raw_nested_tech, dict) else {}

    # 1. Unclosed 30min bar check
    if intraday_tech.get("bar_is_closed") is False or intraday_30m.get("bar_is_closed") is False:
        errors.append("intraday_bar_unclosed")

    # 2. Daily bar timestamp presence, future-timestamp, and staleness check (fail-closed)
    stale_flagged = False
    raw_daily_bar = tech.get("as_of_bar")
    raw_daily_utc = tech.get("as_of_bar_utc")
    if _is_missing_ts(raw_daily_bar) and _is_missing_ts(raw_daily_utc):
        errors.append("missing_daily_timestamp")
    else:
        d_dt_utc, d_local_u, d_has_time_u = _parse_bar_timestamp(raw_daily_utc)
        d_dt_bar, d_local_b, d_has_time_b = _parse_bar_timestamp(raw_daily_bar)
        if d_dt_utc is not None:
            d_dt, d_local, d_has_time = d_dt_utc, d_local_b or d_local_u, d_has_time_u
        else:
            d_dt, d_local, d_has_time = d_dt_bar, d_local_b, d_has_time_b

        if d_dt is None and d_local is None:
            stale_flagged = True
        elif d_has_time and d_dt is not None:
            if d_dt > eff_now + future_tolerance:
                errors.append("daily_future_timestamp")
            else:
                d_age = (eff_now - d_dt).total_seconds() / 86400.0
                if d_age > max_stale_days:
                    stale_flagged = True
        else:
            ref_d = d_local or (d_dt.date() if d_dt is not None else None)
            if ref_d is None:
                stale_flagged = True
            elif ref_d > eff_now.date() + timedelta(days=1):
                errors.append("daily_future_timestamp")
            else:
                d_age = float((eff_now.date() - ref_d).days)
                if d_age > max_stale_days:
                    stale_flagged = True

    # 3. Intraday 30min timestamp presence, date-only guard, future-timestamp, and hour-level staleness check
    raw_intra_bar = intraday_tech.get("as_of_bar")
    raw_intra_utc = intraday_tech.get("as_of_bar_utc")
    if _is_missing_ts(raw_intra_bar) and _is_missing_ts(raw_intra_utc):
        errors.append("missing_intraday_timestamp")
    else:
        i_dt_bar, _i_local_b, i_has_time_b = _parse_bar_timestamp(raw_intra_bar)
        i_dt_utc, _i_local_u, i_has_time_u = _parse_bar_timestamp(raw_intra_utc)

        # Date-only intraday as_of_bar string is explicitly treated as stale/degraded intraday feed
        if not _is_missing_ts(raw_intra_bar) and (i_dt_bar is None or not i_has_time_b):
            stale_flagged = True
            errors.append("stale_intraday_30m_snapshot")
        else:
            if i_dt_utc is not None and i_has_time_u:
                i_dt, i_has_time = i_dt_utc, True
            else:
                i_dt, i_has_time = i_dt_bar, i_has_time_b
            if i_dt is None or not i_has_time:
                stale_flagged = True
                errors.append("stale_intraday_30m_snapshot")
            elif i_dt > eff_now + future_tolerance:
                errors.append("intraday_future_timestamp")
            else:
                i_age_hours = (eff_now - i_dt).total_seconds() / 3600.0
                if in_session:
                    if i_age_hours > max_intraday_stale_hours:
                        errors.append("stale_intraday_30m_snapshot")
                else:
                    # After-hours / overnight / weekend planning mode: allow the latest closed 30m bar
                    # from the most recent trading session within max_stale_days (4d non-crypto).
                    if (i_age_hours / 24.0) > max_stale_days:
                        errors.append("stale_intraday_30m_snapshot")

    if stale_flagged:
        errors.append("stale_market_snapshot")

    # 4. Research sources_status inspection
    res_dict = res if isinstance(res, dict) else {}
    sources_status = res_dict.get("sources_status")
    if isinstance(sources_status, dict):
        ms_st, ms_det = _extract_source_status(sources_status.get("market_snapshot"))
        if ms_st in {"error", "stale"}:
            errors.append(
                f"sources_status_market_snapshot_{ms_st}" + (f": {ms_det}" if ms_det else "")
            )

        rd_gen_st, rd_gen_det = _extract_source_status(sources_status.get("research_data"))
        if rd_gen_st in {"error", "stale"}:
            errors.append(
                f"sources_status_research_data_{rd_gen_st}" + (f": {rd_gen_det}" if rd_gen_det else "")
            )

        if norm_ac in {"commodity", "cn_future"}:
            for k in ("term_structure", "overseas_term_structure"):
                if k in sources_status:
                    st, det = _extract_source_status(sources_status.get(k))
                    if st in {"error", "stale"}:
                        curve_bad = True
                        errors.append(
                            f"sources_status_{k}_{st}" + (f": {det}" if det else "")
                        )

        if norm_ac == "fx":
            for k in ("rate_differential", "fx_rate_differential"):
                if k in sources_status:
                    st, det = _extract_source_status(sources_status.get(k))
                    if st in {"error", "stale"}:
                        rate_diff_bad = True
                        errors.append(
                            f"sources_status_{k}_{st}" + (f": {det}" if det else "")
                        )

    return errors, curve_bad, rate_diff_bad


def _parse_tranches_arg(raw: str) -> tuple[float, ...]:
    parts = [p.strip() for p in raw.split(",") if p.strip()]
    if not parts:
        raise ValueError("--tranches must be a comma-separated list of positive numbers")
    return tuple(float(p) for p in parts)


class PlanInputError(ValueError):
    """Missing/contradictory planner inputs (CLI maps this to argparse usage errors)."""


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Layer-4 trade expression, carry evaluation, four-quadrant gate, and position sizer"
    )
    parser.add_argument("--symbol", required=True, help="Ticker or CN future (e.g. CU0, CL=F, NVDA, 600519, USDJPY)")
    parser.add_argument("--direction", required=True, choices=["long", "short"], help="Trade direction")
    parser.add_argument("--entry", type=float, default=None, help="Planned entry price (auto-filled in --live mode)")
    parser.add_argument("--stop", "--tactical-stop-30m", dest="stop", type=float, default=None, help="30min tactical stop (manual scenario mode only; forbidden with --live)")
    parser.add_argument("--daily-stop", "--structural-stop-daily", dest="daily_stop", type=float, default=None, help="Daily structural invalidation stop (manual scenario mode only; forbidden with --live)")
    parser.add_argument("--target", type=float, default=None, help="Target price")
    parser.add_argument("--equity", type=float, default=1_000_000.0, help="Account equity in account currency")
    parser.add_argument("--account-currency", default="USD", help="Account base currency (default: USD)")
    parser.add_argument("--risk-pct", type=float, default=1.0, help="Risk budget %% of equity (default: 1.0)")
    parser.add_argument("--slot-risk-share", type=float, default=1.0, help="Share of sector risk slot (0, 1]")
    parser.add_argument("--max-margin-pct", type=float, default=100.0, help="Max margin/cash %% of equity (default: 100.0)")
    parser.add_argument("--max-leverage", type=float, default=None, help="Max notional leverage (default: 1.0 for cash equities)")
    parser.add_argument("--tranches", default="0.4,0.4,0.2", help="Comma-separated tranche weights summing to 1.0")
    parser.add_argument("--atr30m", type=float, default=None, help="30min ATR14")
    parser.add_argument("--atr-daily", type=float, default=None, help="Daily ATR14")
    parser.add_argument("--multiplier", type=float, default=None, help="Explicit contract multiplier override")
    parser.add_argument("--margin-rate", type=float, default=None, help="Explicit margin rate override (0, 1]")
    parser.add_argument(
        "--allow-equity-margin",
        action="store_true",
        default=False,
        help="Explicitly allow margin_rate < 1.0 or max_leverage > 1.0 on cash equities",
    )
    parser.add_argument("--fx-to-account", type=float, default=None, help="Quote currency to account currency FX rate (required for cross-currency trades)")
    parser.add_argument("--asset-class", default=None, help="Optional asset_class override")
    parser.add_argument("--horizon-days", type=int, default=90, help="Expected holding horizon in days (default: 90)")
    parser.add_argument("--curve-structure", default=None, help="Override curve structure: Backwardation/Contango/Flat")
    parser.add_argument("--roll-yield", type=float, default=None, help="Override annualized curve roll yield pct (+Back/-Contango)")
    parser.add_argument("--near-roll-yield", type=float, default=None, help="Override annualized near roll yield pct")
    parser.add_argument("--rate-diff-pct", type=float, default=None, help="Override FX base-minus-quote rate diff pct")
    parser.add_argument("--funding-ann-pct", type=float, default=None, help="Override crypto annualized funding rate pct")
    parser.add_argument(
        "--has-l1-l2",
        "--confirm-l1-l2",
        dest="has_l1_l2",
        action="store_true",
        default=False,
        help="Explicitly assert Layer-3 thesis has verified L1/L2 support (default: False, fail-closed)",
    )
    parser.add_argument("--no-l1-l2", dest="has_l1_l2", action="store_false", help="Layer-3 thesis lacks L1/L2 support")
    parser.add_argument("--pending-verifications", type=int, default=0, help="Count of unverified thesis items")
    parser.add_argument("--thesis-falsified", action="store_true", help="Mark thesis as L1-falsified")
    parser.add_argument("--hedge-only", action="store_true", help="Mark equity short as explicit hedge")
    parser.add_argument("--pair-trade", action="store_true", help="Mark trade as same-sector pair trade")
    parser.add_argument("--pair-leg-verified", action="store_true", default=False, help="Explicitly confirm opposing pair leg passed verification")
    parser.add_argument("--min-rr", type=float, default=2.5, help="Minimum R:R gate threshold (default: 2.5)")
    parser.add_argument("--alignment-status", default=None, help="Manual daily/30m alignment_status (offline mode only; forbidden with --live)")
    parser.add_argument("--live", action="store_true", help="Fetch live market snapshot & Layer-3 curve/rate/funding data")
    return parser


def build_trade_plan(args: argparse.Namespace) -> dict[str, Any]:
    """Compose live context, position sizing, carry expression and four-quadrant gate.

    Raises ``PlanInputError`` for missing entry/stop inputs and ``ValueError`` for domain validation errors.
    """

    if args.live and args.alignment_status is not None:
        raise PlanInputError("--alignment-status cannot override live dual alignment in --live mode")
    if args.live and (args.stop is not None or args.daily_stop is not None):
        raise PlanInputError(
            "--stop and --daily-stop cannot override live 30m/daily Chanlun stops in --live mode; "
            "live mode strictly enforces live 30m Chanlun stops — run manual stop scenarios with --live disabled (live=False)"
        )

    live_ctx: dict[str, Any] | None = None
    upstream_errors: list[str] = []
    term_structure: dict[str, Any] | None = None
    rate_diff: dict[str, Any] | None = None
    crypto_funding: dict[str, Any] | None = None
    daily_alignment: dict[str, Any] | None = None

    entry_price = args.entry
    stop_price = args.stop
    daily_stop = args.daily_stop
    atr30m = args.atr30m
    atr_daily = args.atr_daily

    if args.live:
        live_ctx = _fetch_live_context(args.symbol)
        upstream_errors.extend(live_ctx.get("errors") or [])
        snap = live_ctx.get("snapshot") or {}
        if isinstance(snap, dict):
            if snap.get("error"):
                upstream_errors.append(f"snapshot_error: {snap['error']}")
            if snap.get("intraday_error"):
                upstream_errors.append(f"snapshot_intraday_error: {snap['intraday_error']}")
            intraday_30m_blk = snap.get("intraday_30m")
            if isinstance(intraday_30m_blk, dict) and intraday_30m_blk.get("error"):
                upstream_errors.append(f"snapshot_intraday_error: {intraday_30m_blk['error']}")

        tech = snap.get("technicals") or {}
        intraday_tech = snap.get("intraday_technicals")
        if not isinstance(intraday_tech, dict):
            intraday_30m = snap.get("intraday_30m") or {}
            intraday_tech = intraday_30m.get("technicals") if isinstance(intraday_30m, dict) else {}
        if not isinstance(intraday_tech, dict):
            intraday_tech = {}

        if entry_price is None:
            entry_price = _opt_finite_float(intraday_tech.get("last_close")) or _opt_finite_float(tech.get("last_close"))
        if atr_daily is None:
            atr_daily = _opt_finite_float(tech.get("atr14"))
        if atr30m is None:
            atr30m = _opt_finite_float(intraday_tech.get("atr14"))

        raw_chanlun_blk = snap.get("chanlun")
        chanlun_blk: dict[str, Any] = raw_chanlun_blk if isinstance(raw_chanlun_blk, dict) else {}
        daily_cl = chanlun_blk.get("daily")
        intraday_cl = chanlun_blk.get("intraday") or chanlun_blk.get("intraday_30m")
        if isinstance(daily_cl, dict) and daily_cl.get("error"):
            upstream_errors.append(f"daily_chanlun_error: {daily_cl['error']}")
        if isinstance(intraday_cl, dict) and intraday_cl.get("error"):
            upstream_errors.append(f"intraday_chanlun_error: {intraday_cl['error']}")
        if isinstance(daily_cl, dict) and isinstance(intraday_cl, dict) and not daily_cl.get("error") and not intraday_cl.get("error"):
            try:
                from chanlun import assess_dual_timeframe_alignment

                daily_alignment = assess_dual_timeframe_alignment(
                    daily_cl, intraday_cl, desired_direction=args.direction
                )
            except Exception as exc:  # noqa: BLE001
                err_msg = f"dual_alignment_error: {exc}"
                live_ctx["errors"].append(err_msg)
                upstream_errors.append(err_msg)
        if daily_alignment is None:
            for key in ("dual_alignment", "daily_30m_alignment"):
                cand = chanlun_blk.get(key)
                if isinstance(cand, dict):
                    daily_alignment = cand
                    break

        if isinstance(daily_alignment, dict) and entry_price is not None:
            def _valid_stop(val: float | None) -> bool:
                if val is None or not math.isfinite(val) or val <= 0:
                    return False
                return (val < entry_price) if args.direction == "long" else (val > entry_price)

            if stop_price is None:
                cand_30m = _opt_finite_float(daily_alignment.get("intraday_tactical_stop"))
                if _valid_stop(cand_30m):
                    stop_price = cand_30m
            if daily_stop is None:
                for d_key in ("daily_structural_stop", "daily_tactical_stop"):
                    cand_daily = _opt_finite_float(daily_alignment.get(d_key))
                    if _valid_stop(cand_daily):
                        daily_stop = cand_daily
                        break

        if isinstance(snap.get("crypto_perp_funding"), dict):
            crypto_funding = snap["crypto_perp_funding"]

        res = live_ctx.get("research") or {}
        if isinstance(res, dict) and res.get("error"):
            upstream_errors.append(f"research_error: {res['error']}")

        live_ac = args.asset_class or (snap.get("asset_class") if isinstance(snap, dict) else None)
        if not live_ac:
            try:
                live_ac = get_contract_spec(args.symbol)["asset_class"]
            except ValueError:
                live_ac = None
        health_errs, curve_bad_from_status, rd_bad_from_status = _check_live_upstream_health(
            snap if isinstance(snap, dict) else None,
            res if isinstance(res, dict) else None,
            live_ac,
        )
        upstream_errors.extend(health_errs)

        fas = res.get("fundamentals_and_supply") or {}
        ts_raw = fas.get("term_structure")
        if isinstance(ts_raw, dict):
            ts_st = str(ts_raw.get("status") or "").strip().lower()
            if ts_st in ("error", "stale"):
                ts_warn = (
                    f"term_structure_{ts_st}: {ts_raw.get('error') or ts_raw.get('warning') or 'unavailable or stale curve data'}"
                )
                live_ctx["errors"].append(ts_warn)
                upstream_errors.append(ts_warn)
                term_structure = {"status": ts_st, "error": ts_raw.get("error"), "warning": ts_raw.get("warning")}
            elif curve_bad_from_status:
                term_structure = {"status": "stale", "warning": "sources_status reported term_structure stale/error"}
            else:
                term_structure = dict(ts_raw)
        elif curve_bad_from_status:
            term_structure = {"status": "stale", "warning": "sources_status reported term_structure stale/error"}

        for rd_key in ("rate_differential", "fx_rate_differential"):
            rd_raw = fas.get(rd_key)
            if isinstance(rd_raw, dict):
                rd_st = str(rd_raw.get("status") or "").strip().lower()
                if rd_st in ("error", "stale"):
                    rd_warn = f"{rd_key}_{rd_st}: {rd_raw.get('error') or rd_raw.get('warning') or 'unavailable or stale rate diff'}"
                    live_ctx["errors"].append(rd_warn)
                    upstream_errors.append(rd_warn)
                    rate_diff = {"status": rd_st, "error": rd_raw.get("error"), "warning": rd_raw.get("warning")}
                elif rd_bad_from_status:
                    rate_diff = {"status": "stale", "warning": "sources_status reported rate_differential stale/error"}
                else:
                    rate_diff = dict(rd_raw)
                break
        else:
            if rd_bad_from_status:
                rate_diff = {"status": "stale", "warning": "sources_status reported rate_differential stale/error"}

    if entry_price is None:
        raise PlanInputError("--entry is required (or use --live when snapshot last_close is available)")
    if stop_price is None:
        if args.live:
            raise PlanInputError(
                "Live mode strictly enforces live 30min Chanlun stops, but no valid intraday_tactical_stop is available "
                "(run manual stop scenarios with --live disabled / live=False)"
            )
        raise PlanInputError("--stop is required (or use --live when a valid 30min tactical stop is available)")

    if args.curve_structure is not None or args.roll_yield is not None or args.near_roll_yield is not None:
        term_structure = dict(term_structure or {})
        term_structure.pop("status", None)
        if args.curve_structure is not None:
            term_structure["structure"] = args.curve_structure
        if args.roll_yield is not None:
            term_structure["annualized_roll_yield_curve_pct"] = args.roll_yield
        if args.near_roll_yield is not None:
            term_structure["annualized_roll_yield_near_pct"] = args.near_roll_yield

    if args.rate_diff_pct is not None:
        rate_diff = dict(rate_diff or {})
        rate_diff.pop("status", None)
        rate_diff["base_minus_quote_10y_pct"] = args.rate_diff_pct

    if args.funding_ann_pct is not None:
        crypto_funding = {"funding_annualized_pct": args.funding_ann_pct}

    if args.alignment_status is not None:
        daily_alignment = {"alignment_status": args.alignment_status}
        if args.alignment_status == "aligned_trigger":
            daily_alignment["daily_bias"] = args.direction
            daily_alignment["intraday_action"] = f"entry_{args.direction}"

    tranche_weights = _parse_tranches_arg(args.tranches)
    plan = calculate_position_plan(
        symbol=args.symbol,
        direction=args.direction,
        entry_price=entry_price,
        tactical_stop_30m=stop_price,
        structural_stop_daily=daily_stop,
        target_price=args.target,
        account_equity=args.equity,
        account_currency=args.account_currency,
        risk_budget_pct=args.risk_pct,
        slot_risk_share=args.slot_risk_share,
        tranche_weights=tranche_weights,
        atr14_30m=atr30m,
        atr14_daily=atr_daily,
        multiplier=args.multiplier,
        margin_rate=args.margin_rate,
        fx_to_account=args.fx_to_account,
        asset_class=args.asset_class,
        max_margin_pct=args.max_margin_pct,
        max_leverage=args.max_leverage,
        allow_equity_margin=args.allow_equity_margin,
    )
    expression = evaluate_expression_and_carry(
        direction=args.direction,
        asset_class=plan["asset_class"],
        term_structure=term_structure,
        rate_diff=rate_diff,
        crypto_funding=crypto_funding,
        holding_horizon_days=args.horizon_days,
        expected_move_pct=plan.get("expected_move_pct"),
        stop_distance_pct=plan.get("stop_distance_30m_pct"),
    )
    if expression.get("carry_adjusted_rr_ratio") is not None:
        plan["carry_adjusted_rr_ratio"] = expression["carry_adjusted_rr_ratio"]
    gate = evaluate_four_quadrant_gate(
        direction=args.direction,
        asset_class=plan["asset_class"],
        has_l1_l2_evidence=args.has_l1_l2,
        pending_verifications_count=args.pending_verifications,
        thesis_falsified=args.thesis_falsified,
        daily_alignment=daily_alignment,
        rr_ratio=plan.get("rr_ratio"),
        min_rr=args.min_rr,
        hedge_only=args.hedge_only,
        is_pair_trade=args.pair_trade,
        total_quantity=plan.get("total_quantity"),
        within_capital_limits=plan.get("within_capital_limits", True),
        upstream_errors=upstream_errors,
        pair_leg_verified=args.pair_leg_verified,
        carry_adjusted_rr_ratio=expression.get("carry_adjusted_rr_ratio"),
        curve_stale_or_error=bool(expression.get("curve_data_warning")),
    )

    stop_source = "live_30m_chanlun" if args.live else "manual_scenario"
    stop_timeframe = "30m" if args.live else "manual"
    plan["stop_source"] = stop_source
    plan["stop_timeframe"] = stop_timeframe

    output: dict[str, Any] = {
        "four_quadrant_gate": gate,
        "expression_and_carry": expression,
        "position_plan": plan,
        "stop_source": stop_source,
        "stop_timeframe": stop_timeframe,
    }
    dedup_warnings = list(dict.fromkeys(upstream_errors))
    if dedup_warnings:
        output["live_warnings"] = dedup_warnings
    return output


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        output = build_trade_plan(args)
    except PlanInputError as exc:
        parser.error(str(exc))
    except ValueError as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False, indent=2), file=sys.stderr)
        return 2

    print(json.dumps(output, ensure_ascii=False, indent=2, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
