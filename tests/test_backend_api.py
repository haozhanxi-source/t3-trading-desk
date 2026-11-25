"""Comprehensive offline-safe tests for the T3 Trading Desk FastAPI backend & OHLCV export."""
from __future__ import annotations

import json
import math
import os
import sys
import threading
import time
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import pytest
from fastapi.testclient import TestClient

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS_DIR = os.path.join(ROOT_DIR, "scripts")
for p in (ROOT_DIR, SCRIPTS_DIR):
    if p not in sys.path:
        sys.path.insert(0, p)

import chanlun
import daily_sentinel
import market_snapshot
import portfolio_manager as pm
import research_archive as ra
import research_data
import scan_opportunities as so
import trade_planner as tp

from backend.app import create_app
from backend.cache import CACHE, TTLCache
from backend.serializers import sanitize
from backend.tasks import TaskManager


# ---------------------------------------------------------------------------
# Helpers for deterministic synthetic OHLCV DataFrames
# ---------------------------------------------------------------------------
def _make_wave_ohlcv(n: int = 120, *, intraday: bool = False, tz: str | None = None) -> pd.DataFrame:
    """Create a realistic trending + oscillating OHLCV frame that generates Chanlun strokes & Zhongshu."""
    if intraday:
        idx = pd.date_range("2026-10-01 09:30", periods=n, freq="30min", tz=tz)
    else:
        idx = pd.bdate_range("2025-01-02", periods=n, tz=tz)

    # Clear multi-wave oscillation so Chanlun always finds fractals, strokes, and at least one Zhongshu.
    t = np.arange(n, dtype=float)
    base = 100.0 + 0.15 * t + 10.0 * np.sin(t / 4.5) + 4.0 * np.cos(t / 9.0)
    close = np.round(base, 4)
    open_ = np.round(np.roll(close, 1), 4)
    open_[0] = close[0] - 0.5
    high = np.round(np.maximum(open_, close) + 1.8, 4)
    low = np.round(np.minimum(open_, close) - 1.8, 4)
    volume = np.round(1_000_000.0 + 50_000.0 * (1.0 + np.sin(t / 3.0)), 2)

    return pd.DataFrame(
        {"Open": open_, "High": high, "Low": low, "Close": close, "Volume": volume},
        index=idx,
    )


def _parse_sse_stream(raw_text: str) -> list[dict[str, Any]]:
    """Parse SSE text into a list of {id, event, data} dicts with strict JSON parsing."""
    events: list[dict[str, Any]] = []
    cur: dict[str, Any] = {}
    for line in raw_text.splitlines():
        if not line:
            if cur:
                events.append(cur)
                cur = {}
            continue
        if line.startswith(":"):
            continue
        key, _, val = line.partition(":")
        val = val.lstrip(" ")
        if key == "data":
            cur["data"] = json.loads(val, parse_constant=lambda c: (_ for _ in ()).throw(ValueError(f"Invalid JSON constant {c}")))
        else:
            cur[key] = val
    if cur:
        events.append(cur)
    return events


@pytest.fixture()
def isolated_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Isolate portfolio/, research/, universes.json, and CACHE per test."""
    portfolio_dir = tmp_path / "portfolio"
    research_dir = tmp_path / "research"
    universes_path = tmp_path / "universes.json"
    universes_path.write_text(
        json.dumps(
            {
                "us_semis_ai": {
                    "description": "AI semiconductors core leaders",
                    "symbols": ["NVDA", "SPY"],
                },
                "commodities_precious": {
                    "description": "Precious metals",
                    "symbols": ["GC=F", "AU0"],
                },
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    monkeypatch.setattr(pm, "DEFAULT_PORTFOLIO_DIR", str(portfolio_dir))
    monkeypatch.setattr(ra, "DEFAULT_RESEARCH_DIR", str(research_dir))
    monkeypatch.setattr(so, "DEFAULT_UNIVERSES_PATH", str(universes_path))
    CACHE.clear()
    yield {
        "portfolio_dir": portfolio_dir,
        "research_dir": research_dir,
        "universes_path": universes_path,
        "tmp_path": tmp_path,
    }
    CACHE.clear()


@pytest.fixture()
def client(isolated_env) -> TestClient:
    app = create_app(task_manager=TaskManager(max_tasks=50), frontend_dist=str(isolated_env["tmp_path"] / "no_dist"))
    with TestClient(app) as tc:
        yield tc


# ---------------------------------------------------------------------------
# 1. Health & Symbol Search
# ---------------------------------------------------------------------------
def test_health_and_symbol_search(client: TestClient):
    res = client.get("/api/health")
    assert res.status_code == 200
    body = res.json()
    assert body["status"] == "ok"
    assert body["version"] == "1.0.0"
    assert body["portfolio_initialized"] is False
    assert body["archive_count"] == 0
    assert body["universe_count"] == 2
    assert isinstance(body["now_utc"], str) and "T" in body["now_utc"]

    # Search universe symbol
    sr = client.get("/api/symbols/search", params={"q": "nvda", "limit": 10})
    assert sr.status_code == 200
    items = sr.json()["items"]
    assert items
    assert items[0]["symbol"] == "NVDA"
    assert "universe" in items[0]["sources"]
    assert items[0]["pool"] == "us_semis_ai"

    # Search CJK commodity alias
    sr_cn = client.get("/api/symbols/search", params={"q": "黄金", "limit": 10})
    assert sr_cn.status_code == 200
    cn_items = sr_cn.json()["items"]
    assert any(it["symbol"] == "GC=F" and "alias" in it["sources"] for it in cn_items)

    # Empty query respects limit
    sr_all = client.get("/api/symbols/search", params={"limit": 5})
    assert sr_all.status_code == 200
    assert len(sr_all.json()["items"]) == 5


def test_macro_snapshot_cached(client: TestClient, monkeypatch: pytest.MonkeyPatch):
    calls = {"n": 0}

    def fake_macro():
        calls["n"] += 1
        return {
            "VIX": {"last": 16.42, "chg_1d_pct": -2.1},
            "US10Y_Yield": {"last": 4.28},
            "DXY_USD_Index": {"last": 103.85},
        }

    monkeypatch.setattr(market_snapshot, "fetch_macro_context", fake_macro)

    r1 = client.get("/api/macro/snapshot")
    assert r1.status_code == 200
    assert r1.headers.get("X-Cache") == "MISS"
    assert r1.json()["macro_context"]["VIX"]["last"] == 16.42
    assert calls["n"] == 1

    r2 = client.get("/api/macro/snapshot")
    assert r2.status_code == 200
    assert r2.headers.get("X-Cache") == "HIT"
    assert calls["n"] == 1


# ---------------------------------------------------------------------------
# 2. Portfolio Lifecycle & Sentinel Caching / Invalidation
# ---------------------------------------------------------------------------
def test_portfolio_full_lifecycle_and_sentinel(client: TestClient, monkeypatch: pytest.MonkeyPatch):
    # Uninitialized portfolio summary returns clean empty payload
    r0 = client.get("/api/portfolio/summary")
    assert r0.status_code == 200
    assert r0.json() == {"initialized": False, "summary": None, "positions": [], "journal": []}

    # Mock sentinel so we can verify caching & invalidation on mutations
    sentinel_calls = {"n": 0}

    def fake_sentinel(**kwargs):
        sentinel_calls["n"] += 1
        return {
            "checked_at_utc": "2026-11-24T03:00:00Z",
            "ref_date": "2026-11-24",
            "call_no": sentinel_calls["n"],
            "summary": {
                "holdings_total": 0,
                "red_count": 0,
                "yellow_count": 0,
                "green_count": 0,
                "watchlist_total": 0,
                "watchlist_fired_count": 0,
                "discovery_errors_count": 0,
                "state_sync_warnings_count": 0,
            },
            "discovery_errors": [],
            "state_sync_warnings": [],
            "holdings": [],
            "watchlist": [],
        }

    monkeypatch.setattr(daily_sentinel, "run_daily_sentinel", fake_sentinel)

    s1 = client.get("/api/sentinel/run")
    assert s1.status_code == 200
    assert s1.headers["X-Cache"] == "MISS"
    assert s1.json()["call_no"] == 1

    s2 = client.get("/api/sentinel/run")
    assert s2.status_code == 200
    assert s2.headers["X-Cache"] == "HIT"
    assert s2.json()["call_no"] == 1

    # refresh=true bypasses cache
    s3 = client.get("/api/sentinel/run", params={"refresh": "true"})
    assert s3.status_code == 200
    assert s3.headers["X-Cache"] == "MISS"
    assert s3.json()["call_no"] == 2

    # Init portfolio -> also invalidates sentinel cache
    r_init = client.post(
        "/api/portfolio/init",
        json={
            "equity": 100_000.0,
            "base_currency": "USD",
            "default_risk_per_trade_pct": 1.0,
            "max_total_open_risk_pct": 6.0,
            "max_macro_factor_risk_pct": 3.0,
            "overwrite": False,
        },
    )
    assert r_init.status_code == 200
    assert r_init.json()["status"] == "created"

    # Sentinel cache must have been invalidated by portfolio init
    s4 = client.get("/api/sentinel/run")
    assert s4.status_code == 200
    assert s4.headers["X-Cache"] == "MISS"
    assert s4.json()["call_no"] == 3

    # Health reflects initialized portfolio
    assert client.get("/api/health").json()["portfolio_initialized"] is True

    # Open sole leader position
    r_open = client.post(
        "/api/portfolio/positions/open",
        json={
            "symbol": "NVDA",
            "direction": "long",
            "sector_theme": "ai_compute",
            "entry_price": 120.0,
            "tactical_stop_30m": 115.0,
            "structural_stop_daily": 110.0,
            "target_price": 140.0,
            "quantity": 100,
            "leader_role": "sole_leader",
            "macro_factors": ["us_real_rate"],
            "exit_red_lines": ["30m break below 115"],
        },
    )
    assert r_open.status_code == 200
    pos_id = r_open.json()["position"]["position_id"]

    # Symbol search now tags NVDA with portfolio + holding status
    sr = client.get("/api/symbols/search", params={"q": "NVDA"})
    nvda_item = next(it for it in sr.json()["items"] if it["symbol"] == "NVDA")
    assert "portfolio" in nvda_item["sources"]
    assert nvda_item["status"] == "holding"

    # Trail stop tighter -> 200
    r_trail = client.post(
        f"/api/portfolio/positions/{pos_id}/trail-stop",
        json={"new_stop_30m": 120.0, "current_price": 126.0, "reason": "move_to_breakeven"},
    )
    assert r_trail.status_code == 200
    assert r_trail.json()["position"]["tactical_stop_30m"] == 120.0

    # Trail stop looser -> 422
    r_trail_bad = client.post(
        f"/api/portfolio/positions/{pos_id}/trail-stop",
        json={"new_stop_30m": 118.0, "reason": "illegal_loosen"},
    )
    assert r_trail_bad.status_code == 422

    # Trim half position -> 200
    r_trim = client.post(
        f"/api/portfolio/positions/{pos_id}/trim",
        json={"trim_price": 130.0, "trim_quantity": 40, "reason": "partial_tp"},
    )
    assert r_trim.status_code == 200
    assert r_trim.json()["position"]["quantity"] == 60

    # Atomic leader rotation within same sector_theme: NVDA -> AVGO
    r_rot = client.post(
        f"/api/portfolio/positions/{pos_id}/rotate",
        json={
            "exit_price": 132.0,
            "exit_reason": "leader_handoff_to_avgo",
            "new_position": {
                "symbol": "AVGO",
                "direction": "long",
                "sector_theme": "ai_compute",
                "entry_price": 180.0,
                "tactical_stop_30m": 172.0,
                "target_price": 210.0,
                "quantity": 100,
                "leader_role": "sole_leader",
                "macro_factors": ["us_real_rate"],
            },
        },
    )
    assert r_rot.status_code == 200
    assert r_rot.json()["status"] == "rotated"
    new_pos_id = r_rot.json()["opened_position"]["position_id"]
    assert r_rot.json()["opened_position"]["symbol"] == "AVGO"

    # Close rotated position
    r_close = client.post(
        f"/api/portfolio/positions/{new_pos_id}/close",
        json={"exit_price": 190.0, "exit_reason": "target_reached", "exit_category": "take_profit"},
    )
    assert r_close.status_code == 200
    assert r_close.json()["position"]["status"] == "closed"

    # Summary reflects realized equity growth and journal entries
    r_sum = client.get("/api/portfolio/summary")
    assert r_sum.status_code == 200
    sum_body = r_sum.json()
    assert sum_body["initialized"] is True
    assert sum_body["summary"]["open_positions_count"] == 0
    assert sum_body["summary"]["closed_positions_count"] == 2
    assert sum_body["summary"]["equity"] > 100_000.0
    assert len(sum_body["journal"]) >= 5


# ---------------------------------------------------------------------------
# 3. OHLCV Export & /api/market/dual
# ---------------------------------------------------------------------------
def test_serialize_ohlcv_bars_sorting_dedup_and_chanlun_alignment():
    # Unsorted + duplicate timestamp test (daily)
    df_daily = _make_wave_ohlcv(90, intraday=False)
    dup_row = df_daily.iloc[[5]].copy()
    dup_row["Close"] = 999.25
    dup_row["High"] = 1001.0
    messy = pd.concat([df_daily.iloc[10:], df_daily.iloc[:10], dup_row])

    bars = market_snapshot.serialize_ohlcv_bars(messy, intraday=False)
    times = [b["time"] for b in bars]
    assert times == sorted(times)
    assert len(times) == len(set(times))
    # Keep-last semantics for duplicate row
    dup_date = df_daily.index[5].strftime("%Y-%m-%d")
    matched = next(b for b in bars if b["time"] == dup_date)
    assert matched["close"] == 999.25
    assert set(matched.keys()) == {"time", "label", "open", "high", "low", "close", "volume", "dif", "dea", "hist"}

    # Exact MACD & label alignment with chanlun.py on clean daily & intraday frames
    for is_intra, tz in [(False, None), (True, None), (True, "America/New_York")]:
        df = _make_wave_ohlcv(110, intraday=is_intra, tz=tz)
        cl = chanlun.analyze_chanlun(df, timeframe_label="30分钟" if is_intra else "日线", full_export=True)
        serialized = market_snapshot.serialize_ohlcv_bars(df, intraday=is_intra)

        assert len(serialized) == len(df)
        bar_times = [b["time"] for b in serialized]
        assert bar_times == sorted(bar_times)
        assert len(set(bar_times)) == len(bar_times)
        if is_intra:
            assert all(isinstance(t, int) for t in bar_times)
        else:
            assert all(isinstance(t, str) and len(t) == 10 for t in bar_times)

        # Verify MACD values match chanlun.compute_macd
        dif, dea, hist = chanlun.compute_macd(df["Close"].astype(float))
        assert math.isclose(serialized[-1]["dif"], float(dif.iloc[-1]), rel_tol=1e-9, abs_tol=1e-9)
        assert math.isclose(serialized[-1]["dea"], float(dea.iloc[-1]), rel_tol=1e-9, abs_tol=1e-9)
        assert math.isclose(serialized[-1]["hist"], float(hist.iloc[-1]), rel_tol=1e-9, abs_tol=1e-9)

        # Verify 100% verbatim match of chanlun stroke & zhongshu dates against bar["label"]
        label_set = {b["label"] for b in serialized}
        assert cl["all_strokes"], "synthetic wave should produce strokes"
        for st in cl["all_strokes"]:
            assert st["start_date"] in label_set
            assert st["end_date"] in label_set
        for zs in cl["all_stroke_zhongshus"]:
            assert zs["start_date"] in label_set
            assert zs["end_date"] in label_set


def test_market_dual_endpoint_and_backward_compatibility(client: TestClient, monkeypatch: pytest.MonkeyPatch):
    daily_df = _make_wave_ohlcv(120, intraday=False, tz="America/New_York")
    intra_df = _make_wave_ohlcv(120, intraday=True, tz="America/New_York")

    class FakeTicker:
        def __init__(self, sym: str):
            self.sym = sym
            self.info = {"shortName": "SPDR S&P 500", "trailingPE": 22.5}

        def history(self, period: str = "3y", interval: str = "1d"):
            if interval in ("15m", "30m", "60m"):
                return intra_df.copy()
            return daily_df.copy()

    monkeypatch.setattr(market_snapshot.yf, "Ticker", FakeTicker)
    monkeypatch.setattr(market_snapshot, "fetch_macro_context", lambda: {"VIX": {"last": 15.2, "chg_1d_pct": -1.1}})

    # Backward compatibility: include_ohlcv=False (default) must NOT attach ohlcv keys
    snap_default = market_snapshot.get_snapshot("SPY", include_macro=False, include_fundamentals=False, include_intraday=True)
    assert "ohlcv_daily" not in snap_default
    assert "ohlcv_intraday" not in snap_default

    intra_default = market_snapshot.get_intraday_snapshot("SPY", interval="30m")
    assert "ohlcv_intraday" not in intra_default

    # /api/market/dual returns full OHLCV daily + intraday + dual alignment
    r1 = client.get("/api/market/dual", params={"symbol": "SPY", "period": "3y", "intraday_interval": "30m"})
    assert r1.status_code == 200
    assert r1.headers["X-Cache"] == "MISS"
    body = r1.json()
    assert body["symbol"] == "SPY"
    assert len(body["ohlcv_daily"]) == len(daily_df)
    assert len(body["ohlcv_intraday"]) == len(intra_df)
    assert "dual_alignment" in body["chanlun"]
    assert body["macro_context"]["VIX"]["last"] == 15.2

    # Second call hits cache
    r2 = client.get("/api/market/dual", params={"symbol": "SPY", "period": "3y", "intraday_interval": "30m"})
    assert r2.status_code == 200
    assert r2.headers["X-Cache"] == "HIT"

    # Upstream error maps to HTTP 502 with structured detail
    monkeypatch.setattr(market_snapshot, "get_snapshot", lambda *a, **kw: {"error": "Upstream data unavailable"})
    r_err = client.get("/api/market/dual", params={"symbol": "BADSYM", "refresh": "true"})
    assert r_err.status_code == 502
    detail = r_err.json()["detail"]
    assert detail["code"] == "snapshot_error"
    assert "Upstream data unavailable" in detail["message"]


# ---------------------------------------------------------------------------
# 4. /api/trade/plan (manual, portfolio auto-equity, B-structure split, live, 422)
# ---------------------------------------------------------------------------
def test_trade_plan_manual_b_structure_live_and_validation(client: TestClient, monkeypatch: pytest.MonkeyPatch):
    # Initialize portfolio so account_equity=None auto-reads portfolio equity & currency
    client.post(
        "/api/portfolio/init",
        json={"equity": 250_000.0, "base_currency": "USD", "default_risk_per_trade_pct": 1.0},
    )
    for seeded_sym in ("SPY", "CU0"):
        ra.init_archive(seeded_sym)
        ra.append_evidence(
            seeded_sym,
            [{"level": "L1", "kind": "fact", "metric": "verified_kpi", "value": "1"}],
        )

    # 1) Manual mode using frontend canonical field names + auto-read equity
    r1 = client.post(
        "/api/trade/plan",
        json={
            "symbol": "SPY",
            "direction": "long",
            "live": False,
            "entry_price": 500.0,
            "tactical_stop_30m": 490.0,
            "structural_stop_daily": 475.0,
            "target_price": 540.0,
            "holding_horizon_days": 90,
            "has_l1_l2_evidence": True,
            "pending_verifications_count": 0,
            "thesis_falsified": False,
            "is_pair_trade": False,
            "hedge_only": False,
            "alignment_status": "aligned_trigger",
        },
    )
    assert r1.status_code == 200
    p1 = r1.json()
    assert p1["position_plan"]["account_equity"] == 250_000.0
    assert p1["position_plan"]["account_currency"] == "USD"
    assert p1["position_plan"]["rr_ratio"] == 4.0
    assert p1["four_quadrant_gate"]["decision"] == "actionable_long"
    assert p1["live_warnings"] == []

    # 2) Commodity short under Backwardation (B-structure split) must NOT be vetoed
    r_b = client.post(
        "/api/trade/plan",
        json={
            "symbol": "CU0",
            "direction": "short",
            "live": False,
            "entry_price": 75000.0,
            "tactical_stop_30m": 76000.0,
            "target_price": 69000.0,
            "account_equity": 1_000_000.0,
            "account_currency": "CNY",
            "risk_budget_pct": 1.0,
            "horizon_days": 60,
            "curve_structure": "backwardation",
            "roll_yield_pct": 6.0,
            "has_l1_l2": True,
            "alignment_status": "aligned_trigger",
        },
    )
    assert r_b.status_code == 200
    pb = r_b.json()
    assert pb["expression_and_carry"]["vetoed"] is False
    assert pb["expression_and_carry"]["divergence_regime"] == "strong_reality_weak_expectation"
    assert pb["four_quadrant_gate"]["decision"] == "actionable_short"

    # 3) Live mode mocked (fresh closed daily & 30m bars)
    now_utc = datetime.now(timezone.utc)
    fresh_date = now_utc.strftime("%Y-%m-%d")
    fresh_30m = (now_utc - timedelta(minutes=30)).strftime("%Y-%m-%dT%H:%M+00:00")
    fake_live_ctx = {
        "snapshot": {
            "symbol": "SPY",
            "asset_class": "us_equity",
            "technicals": {
                "last_close": 500.0,
                "atr14": 8.0,
                "as_of_bar": fresh_date,
                "as_of_bar_utc": now_utc.isoformat(),
                "bar_status": "closed",
            },
            "intraday_technicals": {
                "last_close": 500.0,
                "atr14": 2.5,
                "as_of_bar": fresh_30m,
                "as_of_bar_utc": (now_utc - timedelta(minutes=30)).isoformat(),
                "bar_status": "closed",
            },
            "chanlun": {
                "dual_alignment": {
                    "alignment_status": "aligned_trigger",
                    "daily_bias": "long",
                    "intraday_action": "entry_long",
                    "intraday_tactical_stop": 492.0,
                    "daily_structural_stop": 478.0,
                }
            },
        },
        "research": {"fundamentals_and_supply": {}, "sources_status": {}},
    }
    monkeypatch.setattr(tp, "_fetch_live_context", lambda sym, **kw: fake_live_ctx)
    r_live = client.post(
        "/api/trade/plan",
        json={
            "symbol": "SPY",
            "direction": "long",
            "live": True,
            "target_price": 530.0,
            "has_l1_l2": True,
        },
    )
    assert r_live.status_code == 200
    pl = r_live.json()
    assert pl["position_plan"]["entry_price"] == 500.0
    assert pl["position_plan"]["tactical_stop_30m"] == 492.0
    assert pl["four_quadrant_gate"]["decision"] == "actionable_long"

    # 4) Missing entry/stop or invalid stop direction -> HTTP 422 with detail
    r_missing = client.post("/api/trade/plan", json={"symbol": "SPY", "direction": "long", "live": False})
    assert r_missing.status_code == 422
    assert "entry_price" in r_missing.json()["detail"]

    r_bad_stop = client.post(
        "/api/trade/plan",
        json={"symbol": "SPY", "direction": "long", "entry_price": 100.0, "tactical_stop_30m": 105.0},
    )
    assert r_bad_stop.status_code == 422
    assert r_bad_stop.json()["detail"]


# ---------------------------------------------------------------------------
# 5. Research Archive CRUD + Optimistic Lock 409 + /api/research/data
# ---------------------------------------------------------------------------
def test_research_archive_crud_lock_conflict_and_data(client: TestClient, monkeypatch: pytest.MonkeyPatch):
    assert client.get("/api/research/archives").json() == {"archives": []}
    assert client.get("/api/research/archive", params={"symbol": "NVDA"}).status_code == 404

    # Init archive
    r_init = client.post(
        "/api/research/archive/init",
        json={
            "symbol": "NVDA",
            "name": "NVIDIA Corp",
            "subtype": "growth_ai",
            "thesis_summary": "AI compute capex cycle leader",
            "primary_driver": "tech_cycle",
            "secondary_driver": "macro_liquidity",
        },
    )
    assert r_init.status_code == 200
    assert r_init.json()["status"] == "created"
    rev1 = r_init.json()["state"]["revision"]
    assert rev1 == 1

    # Append L1 evidence
    r_ev = client.post(
        "/api/research/archive/evidence",
        json={
            "symbol": "NVDA",
            "items": [
                {
                    "metric": "datacenter_revenue_yoy",
                    "value": 94.0,
                    "unit": "pct",
                    "period": "FY26Q3",
                    "level": "L1",
                    "source_url": "https://www.sec.gov/example",
                }
            ],
        },
    )
    assert r_ev.status_code == 200
    assert r_ev.json()["added"] == 1

    # Patch state with correct revision -> revision bumps to 2
    r_patch = client.patch(
        "/api/research/archive/state",
        json={
            "symbol": "NVDA",
            "expected_revision": 1,
            "patch": {"status": "holding", "direction_bias": "long"},
        },
    )
    assert r_patch.status_code == 200
    assert r_patch.json()["revision"] == 2
    assert r_patch.json()["status"] == "holding"

    # Stale expected_revision -> HTTP 409 conflict
    r_conflict = client.patch(
        "/api/research/archive/state",
        json={
            "symbol": "NVDA",
            "expected_revision": 1,
            "patch": {"direction_bias": "short"},
        },
    )
    assert r_conflict.status_code == 409

    # Append log entry
    r_log = client.post("/api/research/archive/log", json={"symbol": "NVDA", "entry": "Verified Q3 10-Q revenue."})
    assert r_log.status_code == 200
    assert "Verified Q3 10-Q revenue." in r_log.json()["appended"]

    # Load full archive
    r_get = client.get("/api/research/archive", params={"symbol": "NVDA"})
    assert r_get.status_code == 200
    loaded = r_get.json()
    assert loaded["state"]["revision"] == 2
    assert loaded["evidence_count"] == 1
    assert len(loaded["recent_evidence"]) == 1
    assert "Verified Q3 10-Q revenue." in loaded["thesis_md"]

    # /api/research/data sanitizes NaN/Inf and caches
    monkeypatch.setattr(
        research_data,
        "collect_research_data",
        lambda sym, include_macro=True: {
            "symbol": sym,
            "asset_class": "us_equity",
            "fundamentals_and_supply": {"pe_fwd": float("nan"), "growth": np.float64(0.42), "inf_val": float("inf")},
            "macro_dual_core": {"ts": pd.Timestamp("2026-11-24T00:00:00Z")},
            "sources_status": {"sec": "ok"},
        },
    )
    rd1 = client.get("/api/research/data", params={"symbol": "NVDA"})
    assert rd1.status_code == 200
    assert rd1.headers["X-Cache"] == "MISS"
    assert "NaN" not in rd1.text and "Infinity" not in rd1.text
    rd_body = rd1.json()
    assert rd_body["fundamentals_and_supply"]["pe_fwd"] is None
    assert rd_body["fundamentals_and_supply"]["inf_val"] is None
    assert rd_body["fundamentals_and_supply"]["growth"] == 0.42

    rd2 = client.get("/api/research/data", params={"symbol": "NVDA"})
    assert rd2.status_code == 200
    assert rd2.headers["X-Cache"] == "HIT"


# ---------------------------------------------------------------------------
# 6. Universes & Async Tasks + SSE Streaming
# ---------------------------------------------------------------------------
def test_universes_and_async_tasks_sse(client: TestClient, monkeypatch: pytest.MonkeyPatch):
    # GET / POST universes
    u1 = client.get("/api/universes")
    assert u1.status_code == 200
    assert "us_semis_ai" in u1.json()["universes"]

    u_save = client.post(
        "/api/universes",
        json={"name": "us_grid_leaders", "description": "Power grid bottleneck", "symbols": ["PWR", "ETN"]},
    )
    assert u_save.status_code == 200
    assert u_save.json()["name"] == "us_grid_leaders"
    assert "us_grid_leaders" in client.get("/api/universes").json()["universes"]

    # Mock get_snapshot for fast deterministic scan task
    def fake_scan_snap(sym: str, **kw):
        return {
            "symbol": sym,
            "asset_class": "us_equity",
            "technicals": {"last_close": 150.0, "ret_20d_pct": 5.5},
            "chanlun": {
                "signal_summary": {"tier": "A", "summary": "日线三买", "action_bias": "bullish"},
                "daily": {
                    "macro_structure": "上涨趋势",
                    "zhongshu_count": 1,
                    "latest_zhongshu": {"zd": 130.0, "zg": 145.0},
                    "latest_fractal": {"kind": "bottom", "date": "2026-11-20", "price": 146.0},
                    "momentum_assessment": {"area_ratio_vs_prev": float("nan"), "verdict": "一致"},
                },
            },
        }

    monkeypatch.setattr(market_snapshot, "get_snapshot", fake_scan_snap)

    # Start scan task using frontend's `sort_mode` field name -> 202
    r_start = client.post("/api/tasks/scan", json={"symbols": ["NVDA", "SPY"], "sort_mode": "tier", "period": "3y"})
    assert r_start.status_code == 202
    task_summary = r_start.json()
    task_id = task_summary["id"]
    assert task_summary["task_id"] == task_id
    assert task_summary["kind"] == "scan"

    # Stream SSE to completion
    with client.stream("GET", f"/api/tasks/{task_id}/events") as sse_resp:
        assert sse_resp.status_code == 200
        assert sse_resp.headers["content-type"].startswith("text/event-stream")
        raw_sse = "".join(sse_resp.iter_text())

    events = _parse_sse_stream(raw_sse)
    event_types = [e["event"] for e in events]
    assert "step" in event_types
    assert event_types.count("row") == 2
    assert "progress" in event_types
    assert event_types[-1] == "done"

    done_payload = events[-1]["data"]
    assert done_payload["result"]["count"] == 2
    assert done_payload["data"]["result"]["count"] == 2

    # Replay after completion + Last-Event-ID resumption
    r_replay = client.get(f"/api/tasks/{task_id}/events")
    assert r_replay.status_code == 200
    replay_events = _parse_sse_stream(r_replay.text)
    assert len(replay_events) == len(events)

    last_seq = int(events[-2]["id"])
    r_resume = client.get(f"/api/tasks/{task_id}/events", headers={"Last-Event-ID": str(last_seq)})
    resumed = _parse_sse_stream(r_resume.text)
    assert len(resumed) == 1 and resumed[0]["event"] == "done"

    # Task list & detail
    assert any(t["id"] == task_id for t in client.get("/api/tasks").json()["tasks"])
    detail = client.get(f"/api/tasks/{task_id}").json()
    assert detail["status"] == "completed"
    assert detail["result"]["count"] == 2
    assert client.get("/api/tasks/does_not_exist").status_code == 404

    # Research refresh task (with existing archive + trigger evaluation)
    client.post("/api/research/archive/init", json={"symbol": "SPY", "name": "SPDR S&P 500"})
    client.patch(
        "/api/research/archive/state",
        json={
            "symbol": "SPY",
            "expected_revision": 1,
            "patch": {
                "triggers": [
                    {
                        "id": "t_pe",
                        "kind": "fundamental",
                        "metric": "forward_pe",
                        "operator": ">=",
                        "threshold": 20.0,
                        "status": "armed",
                        "action": "alert",
                    }
                ]
            },
        },
    )
    monkeypatch.setattr(
        research_data,
        "collect_research_data",
        lambda sym, include_macro=True: {
            "symbol": sym,
            "asset_class": "us_equity",
            "fundamentals_and_supply": {"yfinance_estimates": {"forward_pe": 24.5}},
            "sources_status": {"yfinance": "ok"},
        },
    )
    monkeypatch.setattr(
        daily_sentinel,
        "extract_sentinel_observations",
        lambda bundle, snap: {"forward_pe": 24.5, "data_health_warnings": []},
    )
    r_rr = client.post("/api/tasks/research-refresh", json={"symbol": "SPY", "include_macro": False})
    assert r_rr.status_code == 202
    rr_id = r_rr.json()["id"]
    with client.stream("GET", f"/api/tasks/{rr_id}/events") as sse_rr:
        rr_events = _parse_sse_stream("".join(sse_rr.iter_text()))
    assert rr_events[-1]["event"] == "done"
    assert rr_events[-1]["data"]["result"]["archive_exists"] is True
    assert rr_events[-1]["data"]["result"]["fired_count"] == 1

    # Sentinel async task
    monkeypatch.setattr(
        daily_sentinel,
        "run_daily_sentinel",
        lambda **kw: {"summary": {"holdings_total": 0}, "holdings": [], "watchlist": []},
    )
    r_st = client.post("/api/tasks/sentinel", json={"include_watchlist": True, "skip_research": True, "event_window_days": 7})
    assert r_st.status_code == 202
    st_id = r_st.json()["id"]
    with client.stream("GET", f"/api/tasks/{st_id}/events") as sse_st:
        st_events = _parse_sse_stream("".join(sse_st.iter_text()))
    assert st_events[-1]["event"] == "done"
    assert st_events[-1]["data"]["result"]["summary"]["holdings_total"] == 0


# ---------------------------------------------------------------------------
# 7. Sanitizer, Cache & Static SPA Fallback
# ---------------------------------------------------------------------------
def test_sanitizer_and_ttl_cache():
    raw = {
        "nan_py": float("nan"),
        "inf_py": float("inf"),
        "ninf_py": float("-inf"),
        "nan_np": np.float64("nan"),
        "int_np": np.int64(7),
        "bool_np": np.bool_(True),
        "dec_ok": Decimal("12.34"),
        "dec_nan": Decimal("NaN"),
        "ts": pd.Timestamp("2026-11-24T03:00:00Z"),
        "nat": pd.NaT,
        "dt": datetime(2026, 11, 24, 3, 0, tzinfo=timezone.utc),
        "d": date(2026, 11, 24),
        "td": timedelta(seconds=90),
        "arr": np.array([1.0, float("nan"), 3.0]),
        123: "int_key",
    }
    clean = sanitize(raw)
    encoded = json.dumps(clean, allow_nan=False)
    decoded = json.loads(encoded)
    assert decoded["nan_py"] is None
    assert decoded["inf_py"] is None
    assert decoded["ninf_py"] is None
    assert decoded["nan_np"] is None
    assert decoded["int_np"] == 7
    assert decoded["bool_np"] is True
    assert decoded["dec_ok"] == 12.34
    assert decoded["dec_nan"] is None
    assert decoded["nat"] is None
    assert decoded["td"] == 90.0
    assert decoded["arr"] == [1.0, None, 3.0]
    assert decoded["123"] == "int_key"

    # TTLCache expiry + invalidate_prefix
    now = [100.0]
    cache = TTLCache(clock=lambda: now[0])
    v1, hit1 = cache.get_or_compute(("sentinel", False), 10.0, lambda: "first")
    assert (v1, hit1) == ("first", False)
    v2, hit2 = cache.get_or_compute(("sentinel", False), 10.0, lambda: "second")
    assert (v2, hit2) == ("first", True)
    cache.invalidate_prefix("sentinel")
    v3, hit3 = cache.get_or_compute(("sentinel", False), 10.0, lambda: "third")
    assert (v3, hit3) == ("third", False)
    now[0] += 11.0
    v4, hit4 = cache.get_or_compute(("sentinel", False), 10.0, lambda: "fourth")
    assert (v4, hit4) == ("fourth", False)


def test_spa_static_fallback_and_api_404(tmp_path: Path):
    dist = tmp_path / "dist"
    assets = dist / "assets"
    assets.mkdir(parents=True)
    (dist / "index.html").write_text("<html><body>T3 Desk SPA</body></html>", encoding="utf-8")
    (assets / "main.js").write_text("console.log('ok');", encoding="utf-8")

    app = create_app(task_manager=TaskManager(max_tasks=5), frontend_dist=str(dist))
    with TestClient(app) as tc:
        assert tc.get("/").status_code == 200
        assert "T3 Desk SPA" in tc.get("/war-room/SPY").text
        assert tc.get("/assets/main.js").status_code == 200
        # Unknown /api/* still returns JSON 404, never index.html
        api_404 = tc.get("/api/nonexistent/route")
        assert api_404.status_code == 404
        assert "Unknown API route" in api_404.json()["detail"]


def test_task_manager_ring_buffer_eviction(isolated_env, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(
        daily_sentinel,
        "run_daily_sentinel",
        lambda **kw: {"summary": {"holdings_total": 0}, "holdings": [], "watchlist": []},
    )
    tm = TaskManager(max_tasks=3)
    app = create_app(task_manager=tm, frontend_dist=str(isolated_env["tmp_path"] / "no_dist"))
    with TestClient(app) as tc:
        ids = []
        for _ in range(5):
            r = tc.post("/api/tasks/sentinel", json={})
            assert r.status_code == 202
            tid = r.json()["id"]
            ids.append(tid)
            # Wait for completion via SSE
            with tc.stream("GET", f"/api/tasks/{tid}/events") as resp:
                _ = "".join(resp.iter_text())
        listed = tc.get("/api/tasks").json()["tasks"]
        assert len(listed) == 3
        assert {t["id"] for t in listed} == set(ids[-3:])
        assert tc.get(f"/api/tasks/{ids[0]}").status_code == 404
        # Sanity: time import used if needed
        assert time.monotonic() > 0


def test_update_thesis_preserves_append_only_log(client: TestClient):
    r_init = client.post("/api/research/archive/init", json={"symbol": "PWR", "name": "Quanta Services"})
    assert r_init.status_code == 200
    client.post("/api/research/archive/log", json={"symbol": "PWR", "entry": "历史核验记录不可篡改"})

    # Attempt to overwrite body AND inject/strip audit log -> backend must preserve original log line
    r_put = client.put(
        "/api/research/archive/thesis",
        json={
            "symbol": "PWR",
            "body_md": "## 第三层研究更新\n- Backlog 创新高\n\n## 增量修订日志（只追加，不重写）\n- 试图伪造删除旧日志",
            "expected_revision": 1,
            "note": "补充 Q3 Backlog 硬合同核验",
        },
    )
    assert r_put.status_code == 200
    data = r_put.json()
    assert data["revision"] == 2
    assert "Backlog 创新高" in data["thesis_md"]
    assert "历史核验记录不可篡改" in data["thesis_md"]
    assert "试图伪造删除旧日志" not in data["thesis_md"]
    assert "补充 Q3 Backlog 硬合同核验" in data["thesis_md"]

    # Optimistic concurrency conflict -> 409/400
    r_conflict = client.put(
        "/api/research/archive/thesis",
        json={"symbol": "PWR", "body_md": "stale", "expected_revision": 1},
    )
    assert r_conflict.status_code in (400, 409)

    # Missing expected_revision is strictly rejected (422)
    r_missing_rev = client.put(
        "/api/research/archive/thesis",
        json={"symbol": "PWR", "body_md": "missing rev"},
    )
    assert r_missing_rev.status_code == 422

    # Fenced code blocks (including 4-backtick outer fence containing 3-backtick inner line + `## 修订日志`, AND unclosed EOF fence with dated bullet) must NOT truncate body
    fenced_body = (
        "## 论点\n````markdown\n```\n## 增量修订日志\n- 四反引号内示例\n````\n\n### 关键结论\n必须完整保留段尾\n```markdown\n- `2026-11-24`：未闭合代码块内示例行"
    )
    r_fence = client.put(
        "/api/research/archive/thesis",
        json={"symbol": "PWR", "body_md": fenced_body, "expected_revision": 2},
    )
    assert r_fence.status_code == 200
    assert "必须完整保留段尾" in r_fence.json()["thesis_md"]
    assert "未闭合代码块内示例行" in r_fence.json()["thesis_md"]
    assert "历史核验记录不可篡改" in r_fence.json()["thesis_md"]


def test_update_thesis_wal_consistency_and_headerless_log_rescue(
    isolated_env, monkeypatch: pytest.MonkeyPatch
):
    ra.init_archive("CU0", name="沪铜")
    folder, _, _, _ = ra.resolve_archive_dir("CU0")
    thesis_path = os.path.join(folder, "thesis.md")
    with open(thesis_path, "w", encoding="utf-8") as f:
        f.write("# 沪铜旧档\n初始正文\n\n- `2026-11-20`：旧版无标题日志行\n")

    real_write_json = ra._atomic_write_json

    # Case 1: Failure BEFORE WAL commit point -> untouched v1
    def fail_on_wal(path: str, obj: dict):
        if path.endswith(".thesis_wal.json"):
            raise OSError("simulated wal write error")
        return real_write_json(path, obj)

    monkeypatch.setattr(ra, "_atomic_write_json", fail_on_wal)
    with pytest.raises(OSError, match="simulated wal write error"):
        ra.update_thesis_body("CU0", "## 未达提交点", expected_revision=1)
    monkeypatch.setattr(ra, "_atomic_write_json", real_write_json)
    untouched = ra.load_archive("CU0")
    assert untouched["state"]["revision"] == 1
    assert "未达提交点" not in untouched["thesis_md"]

    # Case 2: Failure AFTER WAL commit (during state.json write) -> next load/write rolls forward atomically to v2 (never torn!)
    def fail_on_state_json(path: str, obj: dict):
        if path.endswith("state.json"):
            raise OSError("simulated state.json disk full")
        return real_write_json(path, obj)

    monkeypatch.setattr(ra, "_atomic_write_json", fail_on_state_json)
    with pytest.raises(OSError, match="simulated state.json disk full"):
        ra.update_thesis_body("CU0", "## WAL已提交正文", expected_revision=1, note="完成迁移")

    # Restore writer -> load_archive replays WAL cleanly so revision == 2 and stale expected_revision=1 is rejected!
    monkeypatch.setattr(ra, "_atomic_write_json", real_write_json)
    replayed = ra.load_archive("CU0")
    assert replayed["state"]["revision"] == 2
    assert "WAL已提交正文" in replayed["thesis_md"]
    assert "旧版无标题日志行" in replayed["thesis_md"]
    assert "完成迁移" in replayed["thesis_md"]
    with pytest.raises(ValueError, match="Revision conflict"):
        ra.update_thesis_body("CU0", "## 陈旧覆盖", expected_revision=1)


def test_scan_persistence_survives_eviction_and_merges_multi_instance(
    isolated_env, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(
        market_snapshot,
        "get_snapshot",
        lambda sym, **kw: {
            "symbol": sym,
            "asset_class": "us_equity",
            "technicals": {
                "last_close": 210.0,
                "return_5d_pct": -1.2,
                "return_20d_pct": -4.5,
                "return_60d_pct": -8.0,
                "atr14_pct": 2.1,
            },
            "chanlun": {
                "signal_summary": {
                    "tier": "买卖点信号",
                    "summary": "日线三卖确认",
                    "action_bias": "空头方向，严禁当一买盲目抄底",
                    "signal_meta": {"direction": "short", "signal_type": "third_sell", "confirmed": True},
                },
                "daily": {"macro_structure": "下跌趋势", "zhongshu_count": 1},
            },
        },
    )
    monkeypatch.setattr(
        daily_sentinel,
        "run_daily_sentinel",
        lambda **kw: {"summary": {"holdings_total": 0}, "holdings": [], "watchlist": []},
    )

    tm_a = TaskManager(max_tasks=2)
    tm_b = TaskManager(max_tasks=2)
    app_a = create_app(task_manager=tm_a, frontend_dist=str(isolated_env["tmp_path"] / "no_dist"))
    app_b = create_app(task_manager=tm_b, frontend_dist=str(isolated_env["tmp_path"] / "no_dist"))

    with TestClient(app_a) as tca, TestClient(app_b) as tcb:
        r_a = tca.post("/api/tasks/scan", json={"symbols": ["SPY"], "sort": "tier"})
        tid_a = r_a.json()["id"]
        with tca.stream("GET", f"/api/tasks/{tid_a}/events") as s:
            _ = "".join(s.iter_text())

        # Second instance runs another scan -> must merge with tid_a, NOT overwrite it
        r_b = tcb.post("/api/tasks/scan", json={"symbols": ["NVDA"], "sort": "tier"})
        tid_b = r_b.json()["id"]
        with tcb.stream("GET", f"/api/tasks/{tid_b}/events") as s:
            _ = "".join(s.iter_text())

        # Flood tm_b's small 2-slot ring buffer with 4 sentinel tasks -> persisted scans still survive!
        for _ in range(4):
            rs = tcb.post("/api/tasks/sentinel", json={})
            with tcb.stream("GET", f"/api/tasks/{rs.json()['id']}/events") as s:
                _ = "".join(s.iter_text())

        det_a = tcb.get(f"/api/tasks/{tid_a}").json()
        det_b = tcb.get(f"/api/tasks/{tid_b}").json()
        assert det_a["status"] == "completed"
        assert det_b["status"] == "completed"
        assert det_a["result"]["rows"][0]["signal_direction"] == "short"
        assert det_a["result"]["rows"][0]["signal_type"] == "third_sell"

        # Instance A (holding old cache) reads after Instance B writes 10 newer scans -> must see B's 10 newest, never hide them!
        newest_ids = []
        for idx in range(10):
            time.sleep(0.002)
            rb = tcb.post("/api/tasks/scan", json={"symbols": ["SPY"], "sort": "tier"})
            nid = rb.json()["id"]
            newest_ids.append(nid)
            with tcb.stream("GET", f"/api/tasks/{nid}/events") as s:
                _ = "".join(s.iter_text())
        # Force mtime bump in case coarse filesystem mtime granularity
        hist_path = tm_b._scan_history_path()
        st = os.stat(hist_path)
        os.utime(hist_path, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000))
        seen_on_a = [t["id"] for t in tca.get("/api/tasks").json()["tasks"] if t["kind"] == "scan"]
        assert seen_on_a == list(reversed(newest_ids))
        assert tid_a not in seen_on_a
        assert tca.get(f"/api/tasks/{tid_a}").status_code == 404


def test_trade_plan_unconditionally_blocks_spoofed_client_l1_l2_flag(client: TestClient):
    # Even if client sends has_l1_l2=True directly to API, server checks research/ archive on disk -> NOT actionable
    r_unarchived = client.post(
        "/api/trade/plan",
        json={
            "symbol": "PWR",
            "direction": "long",
            "live": False,
            "entry_price": 300.0,
            "tactical_stop_30m": 290.0,
            "target_price": 340.0,
            "account_equity": 100000.0,
            "account_currency": "USD",
            "has_l1_l2": True,
            "alignment_status": "aligned_trigger",
        },
    )
    assert r_unarchived.status_code == 200
    assert r_unarchived.json()["four_quadrant_gate"]["actionable"] is False


def test_market_dual_exposes_contract_spec(client: TestClient, monkeypatch: pytest.MonkeyPatch):
    def fake_snapshot(symbol, **kwargs):
        return {
            "symbol": "CU0",
            "asset_class": "cn_future",
            "ohlcv_daily": [],
            "ohlcv_intraday": [],
            "chanlun": {},
        }

    monkeypatch.setattr(market_snapshot, "get_snapshot", fake_snapshot)
    r = client.get("/api/market/dual", params={"symbol": "CU0", "include_macro": False})
    assert r.status_code == 200
    body = r.json()
    spec = body.get("contract_spec")
    assert isinstance(spec, dict)
    assert spec["known_spec"] is True
    assert spec["multiplier"] == 5
    assert spec["lot_step"] == 1
    assert spec["quote_currency"] == "CNY"


def test_scan_task_persists_across_task_manager_restarts(isolated_env, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(
        market_snapshot,
        "get_snapshot",
        lambda sym, **kw: {
            "symbol": sym,
            "asset_class": "us_equity",
            "technicals": {"last_close": 210.0, "ret_20d_pct": 3.2},
            "chanlun": {"signal_summary": {"tier": "A", "summary": "三买"}, "daily": {"macro_structure": "上涨"}},
        },
    )

    app1 = create_app(task_manager=TaskManager(max_tasks=10), frontend_dist=str(isolated_env["tmp_path"] / "no_dist"))
    with TestClient(app1) as tc1:
        r = tc1.post("/api/tasks/scan", json={"symbols": ["PWR"], "sort": "tier"})
        assert r.status_code == 202
        tid = r.json()["id"]
        with tc1.stream("GET", f"/api/tasks/{tid}/events") as sse:
            _ = "".join(sse.iter_text())

    # Brand new TaskManager instance simulating service restart -> persisted scan history restores cleanly
    app2 = create_app(task_manager=TaskManager(max_tasks=10), frontend_dist=str(isolated_env["tmp_path"] / "no_dist"))
    with TestClient(app2) as tc2:
        listed = tc2.get("/api/tasks").json()["tasks"]
        assert any(t["id"] == tid and t["status"] == "completed" for t in listed)
        det = tc2.get(f"/api/tasks/{tid}").json()
        assert det["result"]["count"] == 1
        assert det["result"]["rows"][0]["symbol"] == "PWR"


# ---------------------------------------------------------------------------
# 8. Codex Round-1 Audit Regression Tests
# ---------------------------------------------------------------------------
def test_sentinel_cache_epoch_prevents_stale_poisoning_on_concurrent_mutation(
    isolated_env, monkeypatch: pytest.MonkeyPatch
):
    app = create_app(task_manager=TaskManager(max_tasks=20), frontend_dist=str(isolated_env["tmp_path"] / "no_dist"))

    # --- Part A: Sync GET /api/sentinel/run racing with POST /api/portfolio/positions/open ---
    entered_sync = threading.Barrier(2)
    mutated_sync = threading.Barrier(2)
    call_counter = {"n": 0}
    counter_lock = threading.Lock()

    def controlled_sentinel(**kwargs):
        with counter_lock:
            call_counter["n"] += 1
            call_no = call_counter["n"]
        positions = pm.load_positions()
        snap = {
            "summary": {"holdings_total": len(positions), "call_no": call_no},
            "holdings": [{"symbol": p["symbol"]} for p in positions],
            "watchlist": [],
        }
        if call_no == 1:
            entered_sync.wait(timeout=5)
            mutated_sync.wait(timeout=5)
        elif call_no == 3:
            entered_async.wait(timeout=5)
            mutated_async.wait(timeout=5)
        return snap

    entered_async = threading.Barrier(2)
    mutated_async = threading.Barrier(2)
    monkeypatch.setattr(daily_sentinel, "run_daily_sentinel", controlled_sentinel)

    with TestClient(app) as tc:
        r_init = tc.post("/api/portfolio/init", json={"equity": 500_000.0, "base_currency": "USD"})
        assert r_init.status_code == 200

        holder: dict[str, Any] = {}

        def run_slow_sync_sentinel():
            with TestClient(app) as worker_tc:
                resp = worker_tc.get("/api/sentinel/run")
                holder["status"] = resp.status_code
                holder["cache"] = resp.headers.get("X-Cache")
                holder["body"] = resp.json()

        t = threading.Thread(target=run_slow_sync_sentinel)
        t.start()

        # Wait until the sentinel thread has read 0 positions and is mid-flight
        entered_sync.wait(timeout=5)

        # Mutate portfolio while sentinel is in flight -> bumps "sentinel" epoch
        r_open1 = tc.post(
            "/api/portfolio/positions/open",
            json={
                "symbol": "NVDA",
                "direction": "long",
                "sector_theme": "ai_semis",
                "entry_price": 120.0,
                "tactical_stop_30m": 115.0,
                "quantity": 100,
            },
        )
        assert r_open1.status_code == 200

        # Let the in-flight sentinel finish and attempt to write back to cache
        mutated_sync.wait(timeout=5)
        t.join(timeout=5)
        assert not t.is_alive()

        # The in-flight caller still receives its response, without poisoning the cache
        assert holder["status"] == 200
        assert holder["cache"] == "MISS"
        assert holder["body"]["summary"]["holdings_total"] == 0

        # Subsequent GET /api/sentinel/run MUST miss cache and compute fresh 1-position result
        r_fresh1 = tc.get("/api/sentinel/run")
        assert r_fresh1.status_code == 200
        assert r_fresh1.headers["X-Cache"] == "MISS"
        assert r_fresh1.json()["summary"]["holdings_total"] == 1

        # Next GET hits the fresh cached entry
        r_hit1 = tc.get("/api/sentinel/run")
        assert r_hit1.status_code == 200
        assert r_hit1.headers["X-Cache"] == "HIT"
        assert r_hit1.json()["summary"]["holdings_total"] == 1

        # --- Part B: Async POST /api/tasks/sentinel racing with portfolio write ---
        # Start async sentinel task (will be call_no == 3)
        r_task = tc.post("/api/tasks/sentinel", json={})
        assert r_task.status_code == 202
        task_id = r_task.json()["id"]

        # Wait until async task has captured epoch & read 1 position
        entered_async.wait(timeout=5)

        # Open a second position in another sector while async sentinel is in flight -> bumps epoch
        r_open2 = tc.post(
            "/api/portfolio/positions/open",
            json={
                "symbol": "PWR",
                "direction": "long",
                "sector_theme": "power_grid",
                "entry_price": 200.0,
                "tactical_stop_30m": 190.0,
                "quantity": 50,
            },
        )
        assert r_open2.status_code == 200

        # Release async sentinel so it finishes and attempts CACHE.set(..., expected_epoch=...)
        mutated_async.wait(timeout=5)
        with tc.stream("GET", f"/api/tasks/{task_id}/events") as sse_resp:
            events = _parse_sse_stream("".join(sse_resp.iter_text()))
        assert events[-1]["event"] == "done"
        assert events[-1]["data"]["result"]["summary"]["holdings_total"] == 1

        # Cache MUST NOT be poisoned with the 1-position async task result
        r_fresh2 = tc.get("/api/sentinel/run")
        assert r_fresh2.status_code == 200
        assert r_fresh2.headers["X-Cache"] == "MISS"
        assert r_fresh2.json()["summary"]["holdings_total"] == 2


def test_trade_plan_live_rejects_manual_stops_and_exposes_stop_provenance(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
):
    client.post(
        "/api/portfolio/init",
        json={"equity": 250_000.0, "base_currency": "USD", "default_risk_per_trade_pct": 1.0},
    )

    now_utc = datetime.now(timezone.utc)
    fake_live_ctx = {
        "snapshot": {
            "symbol": "SPY",
            "asset_class": "us_equity",
            "technicals": {
                "last_close": 500.0,
                "atr14": 8.0,
                "as_of_bar": now_utc.strftime("%Y-%m-%d"),
                "as_of_bar_utc": now_utc.isoformat(),
                "bar_status": "closed",
            },
            "intraday_technicals": {
                "last_close": 500.0,
                "atr14": 2.5,
                "as_of_bar": (now_utc - timedelta(minutes=30)).strftime("%Y-%m-%dT%H:%M+00:00"),
                "as_of_bar_utc": (now_utc - timedelta(minutes=30)).isoformat(),
                "bar_status": "closed",
            },
            "chanlun": {
                "dual_alignment": {
                    "alignment_status": "aligned_trigger",
                    "daily_bias": "long",
                    "intraday_action": "entry_long",
                    "intraday_tactical_stop": 492.0,
                    "daily_structural_stop": 478.0,
                }
            },
        },
        "research": {"fundamentals_and_supply": {}, "sources_status": {}},
    }
    monkeypatch.setattr(tp, "_fetch_live_context", lambda sym, **kw: fake_live_ctx)

    # live=True with manual tactical_stop_30m -> HTTP 422
    r_bad_30m = client.post(
        "/api/trade/plan",
        json={
            "symbol": "SPY",
            "direction": "long",
            "live": True,
            "tactical_stop_30m": 499.0,
            "target_price": 530.0,
            "has_l1_l2": True,
        },
    )
    assert r_bad_30m.status_code == 422
    assert "tactical_stop_30m" in r_bad_30m.json()["detail"]

    # live=True with manual structural_stop_daily -> HTTP 422
    r_bad_daily = client.post(
        "/api/trade/plan",
        json={
            "symbol": "SPY",
            "direction": "long",
            "live": True,
            "structural_stop_daily": 460.0,
            "target_price": 530.0,
            "has_l1_l2": True,
        },
    )
    assert r_bad_daily.status_code == 422
    assert "structural_stop_daily" in r_bad_daily.json()["detail"]

    # live=True without manual stops -> HTTP 200 with live_30m_chanlun provenance
    r_live_ok = client.post(
        "/api/trade/plan",
        json={
            "symbol": "SPY",
            "direction": "long",
            "live": True,
            "target_price": 530.0,
            "has_l1_l2": True,
        },
    )
    assert r_live_ok.status_code == 200
    body_live = r_live_ok.json()
    assert body_live["stop_source"] == "live_30m_chanlun"
    assert body_live["stop_timeframe"] == "30m"
    assert body_live["position_plan"]["stop_source"] == "live_30m_chanlun"
    assert body_live["position_plan"]["stop_timeframe"] == "30m"
    assert body_live["position_plan"]["tactical_stop_30m"] == 492.0
    assert body_live["equity_source"] == "portfolio_ledger"

    # live=False with manual stops -> HTTP 200 with manual_scenario provenance
    r_manual = client.post(
        "/api/trade/plan",
        json={
            "symbol": "SPY",
            "direction": "long",
            "live": False,
            "entry_price": 500.0,
            "tactical_stop_30m": 495.0,
            "target_price": 520.0,
            "has_l1_l2": True,
            "alignment_status": "aligned_trigger",
        },
    )
    assert r_manual.status_code == 200
    body_manual = r_manual.json()
    assert body_manual["stop_source"] == "manual_scenario"
    assert body_manual["stop_timeframe"] == "manual"
    assert body_manual["position_plan"]["stop_source"] == "manual_scenario"
    assert body_manual["position_plan"]["stop_timeframe"] == "manual"


def test_trade_plan_uninitialized_portfolio_requires_explicit_equity(client: TestClient):
    # Portfolio is uninitialized -> omitting account_equity MUST return HTTP 409 (no silent 1M fallback)
    r_409 = client.post(
        "/api/trade/plan",
        json={
            "symbol": "SPY",
            "direction": "long",
            "live": False,
            "entry_price": 500.0,
            "tactical_stop_30m": 490.0,
            "target_price": 530.0,
        },
    )
    assert r_409.status_code == 409
    assert "本地持仓账本尚未初始化" in r_409.json()["detail"]

    # Passing explicit account_equity works and marks equity_source="manual_simulation"
    r_sim = client.post(
        "/api/trade/plan",
        json={
            "symbol": "SPY",
            "direction": "long",
            "live": False,
            "entry_price": 500.0,
            "tactical_stop_30m": 490.0,
            "target_price": 530.0,
            "account_equity": 400_000.0,
            "account_currency": "USD",
        },
    )
    assert r_sim.status_code == 200
    sim_body = r_sim.json()
    assert sim_body["equity_source"] == "manual_simulation"
    assert sim_body["position_plan"]["account_equity"] == 400_000.0

    # Once portfolio is initialized, omitting account_equity uses ledger and marks equity_source="portfolio_ledger"
    client.post("/api/portfolio/init", json={"equity": 320_000.0, "base_currency": "USD"})
    r_ledger = client.post(
        "/api/trade/plan",
        json={
            "symbol": "SPY",
            "direction": "long",
            "live": False,
            "entry_price": 500.0,
            "tactical_stop_30m": 490.0,
            "target_price": 530.0,
        },
    )
    assert r_ledger.status_code == 200
    ledger_body = r_ledger.json()
    assert ledger_body["equity_source"] == "portfolio_ledger"
    assert ledger_body["position_plan"]["account_equity"] == 320_000.0


def test_research_archive_market_override_propagation(client: TestClient):
    r_init = client.post(
        "/api/research/archive/init",
        json={
            "symbol": "PWR",
            "market_override": "FX",
            "name": "PWR Custom FX Archive",
            "thesis_summary": "Testing explicit market_override propagation",
        },
    )
    assert r_init.status_code == 200
    assert r_init.json()["state"]["instrument"]["market"] == "FX"

    # Default market lookup for PWR resolves to US -> 404
    assert client.get("/api/research/archive", params={"symbol": "PWR"}).status_code == 404

    # Explicit market=FX lookup succeeds
    r_get = client.get("/api/research/archive", params={"symbol": "PWR", "market": "FX"})
    assert r_get.status_code == 200
    assert r_get.json()["market"] == "FX"

    # Evidence / state / log updates with market="FX" all target the FX archive
    r_ev = client.post(
        "/api/research/archive/evidence",
        json={
            "symbol": "PWR",
            "market": "FX",
            "items": [
                {
                    "metric": "rate_diff_10y",
                    "value": 1.25,
                    "unit": "pct",
                    "period": "2026-11",
                    "level": "L1",
                    "source_url": "https://fred.stlouisfed.org/example",
                }
            ],
        },
    )
    assert r_ev.status_code == 200
    assert r_ev.json()["added"] == 1

    r_state = client.patch(
        "/api/research/archive/state",
        json={
            "symbol": "PWR",
            "market": "FX",
            "expected_revision": 1,
            "patch": {"status": "holding", "direction_bias": "long"},
        },
    )
    assert r_state.status_code == 200
    assert r_state.json()["revision"] == 2
    assert r_state.json()["status"] == "holding"

    r_log = client.post(
        "/api/research/archive/log",
        json={"symbol": "PWR", "market": "FX", "entry": "Confirmed FX archive override."},
    )
    assert r_log.status_code == 200
    assert "Confirmed FX archive override." in r_log.json()["appended"]

    r_get2 = client.get("/api/research/archive", params={"symbol": "PWR", "market": "FX"})
    assert r_get2.status_code == 200
    loaded = r_get2.json()
    assert loaded["evidence_count"] == 1
    assert loaded["state"]["revision"] == 2
    assert "Confirmed FX archive override." in loaded["thesis_md"]


def test_fx_reference_daily_usd_cny_and_cross_rates(client: TestClient, monkeypatch: pytest.MonkeyPatch):
    calls: list[str] = []

    def fake_get_snapshot(symbol: str, **_kwargs: Any) -> dict[str, Any]:
        calls.append(symbol)
        quotes = {
            "USDCNY=X": ("2026-11-25", 7.20),
            "USDHKD=X": ("2026-11-24", 7.80),
            "EURUSD=X": ("2026-11-25", 1.05),
        }
        if symbol not in quotes:
            return {"symbol": symbol, "error": f"No data for {symbol}"}
        bar_date, close_val = quotes[symbol]
        return {
            "symbol": symbol,
            "data_source": "yfinance",
            "technicals": {"as_of_bar": bar_date, "last_close": close_val},
        }

    monkeypatch.setattr(market_snapshot, "get_snapshot", fake_get_snapshot)

    # 1. CNY -> USD returns both USD/CNY (7.20) and 1 CNY -> USD (0.138889)
    r1 = client.get("/api/fx/reference", params={"quote": "CNY", "account": "USD"})
    assert r1.status_code == 200
    assert r1.headers.get("X-Cache") == "MISS"
    b1 = r1.json()
    assert b1["available"] is True
    assert b1["usd_cny"] == 7.2
    assert b1["cny_usd"] == pytest.approx(1.0 / 7.2, abs=1e-6)
    assert b1["fx_to_account"] == pytest.approx(1.0 / 7.2, abs=1e-6)
    assert b1["account_to_quote"] == 7.2
    assert b1["as_of_date"] == "2026-11-25"

    # 2. Second request hits cache without re-fetching
    prev_calls = len(calls)
    r2 = client.get("/api/fx/reference", params={"quote": "cny", "account": "usd"})
    assert r2.status_code == 200
    assert r2.headers.get("X-Cache") == "HIT"
    assert len(calls) == prev_calls

    # 3. HKD -> USD also includes the daily USD/CNY benchmark
    r3 = client.get("/api/fx/reference", params={"quote": "HKD", "account": "USD"})
    assert r3.status_code == 200
    b3 = r3.json()
    assert b3["available"] is True
    assert b3["fx_to_account"] == pytest.approx(1.0 / 7.8, abs=1e-6)
    assert b3["usd_cny"] == 7.2
    assert b3["as_of_date"] == "2026-11-25"

    # 4. EUR -> CNY cross rate via USD legs (1 EUR = 1.05 * 7.20 = 7.56 CNY)
    r4 = client.get("/api/fx/reference", params={"quote": "EUR", "account": "CNY"})
    assert r4.status_code == 200
    b4 = r4.json()
    assert b4["available"] is True
    assert b4["fx_to_account"] == pytest.approx(7.56, abs=1e-6)

    # 5. Unsupported currency fails gracefully with HTTP 200 available=False (never crashes dialog)
    r5 = client.get("/api/fx/reference", params={"quote": "ZAR", "account": "USD"})
    assert r5.status_code == 200
    b5 = r5.json()
    assert b5["available"] is False
    assert b5["fx_to_account"] is None
    assert b5["usd_cny"] == 7.2


    # Conflicting market vs market_override fails loudly with 422
    assert client.get("/api/research/archive", params={"symbol": "PWR", "market": "FX", "market_override": "US"}).status_code == 422


def test_save_universe_concurrent_barrier_preserves_both_pools(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    uni_path = tmp_path / "universes.json"
    uni_path.write_text("{}\n", encoding="utf-8")

    # Slow down load_universes slightly so an unlocked read-modify-write would deterministically lose an update
    orig_load = so.load_universes

    def slow_load(path: str | None = None) -> dict:
        data = orig_load(path)
        time.sleep(0.03)
        return data

    monkeypatch.setattr(so, "load_universes", slow_load)

    barrier = threading.Barrier(2)
    errors: list[BaseException] = []

    def writer(pool_name: str, syms: list[str]):
        try:
            barrier.wait(timeout=5)
            so.save_universe(pool_name, syms, description=f"desc {pool_name}", universes_path=str(uni_path))
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    t_a = threading.Thread(target=writer, args=("pool_a", ["NVDA", "SPY"]))
    t_b = threading.Thread(target=writer, args=("pool_b", ["GC=F", "CL=F"]))
    t_a.start()
    t_b.start()
    t_a.join(timeout=5)
    t_b.join(timeout=5)

    assert not errors, f"Concurrent save_universe raised: {errors}"
    saved = orig_load(str(uni_path))
    assert "pool_a" in saved and saved["pool_a"]["symbols"] == ["NVDA", "SPY"]
    assert "pool_b" in saved and saved["pool_b"]["symbols"] == ["GC=F", "CL=F"]
    assert (tmp_path / ".universes.lock").exists()
    assert not list(tmp_path.glob(".universes_*.tmp"))


def test_same_currency_rejects_non_unit_fx_in_planner_and_ledger(client: TestClient):
    """Codex Round-2 HIGH #4: CNY->CNY with fx_to_account=0.138 MUST be rejected in both planner and open_position."""
    r_init = client.post(
        "/api/portfolio/init",
        json={
            "equity": 100000.0,
            "base_currency": "CNY",
            "default_risk_per_trade_pct": 1.0,
            "max_total_open_risk_pct": 6.0,
            "max_macro_factor_risk_pct": 3.0,
        },
    )
    assert r_init.status_code == 200

    # 1) Planner: CNY quote (600519) + CNY account + stale USD FX 0.138 -> HTTP 422
    r_bad_plan = client.post(
        "/api/trade/plan",
        json={
            "symbol": "600519",
            "direction": "long",
            "live": False,
            "entry_price": 1500.0,
            "tactical_stop_30m": 1495.0,
            "target_price": 1520.0,
            "fx_to_account": 0.138,
            "has_l1_l2": True,
            "alignment_status": "aligned_trigger",
        },
    )
    assert r_bad_plan.status_code == 422
    assert "Same-currency trade" in r_bad_plan.json()["detail"]

    # 2) Portfolio open: 1400 shares * 5 CNY = 7000 CNY > 1000 CNY sector cap; stale 0.138 MUST NOT sneak past cap
    r_bad_open = client.post(
        "/api/portfolio/positions/open",
        json={
            "symbol": "600519",
            "direction": "long",
            "sector_theme": "cn_consumer",
            "entry_price": 1500.0,
            "tactical_stop_30m": 1495.0,
            "quantity": 1400,
            "fx_to_account": 0.138,
        },
    )
    assert r_bad_open.status_code == 422
    assert "Same-currency trade" in r_bad_open.json()["detail"]

    # 3) Explicit 1.0 or omitted fx_to_account works cleanly for same-currency CNY->CNY
    r_ok_plan = client.post(
        "/api/trade/plan",
        json={
            "symbol": "600519",
            "direction": "long",
            "live": False,
            "entry_price": 100.0,
            "tactical_stop_30m": 95.0,
            "target_price": 120.0,
            "has_l1_l2": True,
            "alignment_status": "aligned_trigger",
        },
    )
    assert r_ok_plan.status_code == 200
    assert r_ok_plan.json()["position_plan"]["total_quantity"] == 200
    assert r_ok_plan.json()["position_plan"]["fx_to_account"] == 1.0


def test_pair_leg_verified_requires_distinct_opposite_symbol(client: TestClient):
    """Codex Round-2 HIGH #1: pair_leg_verified=True MUST require non-empty pair_leg_symbol != primary symbol."""
    ra.init_archive("SPY")
    ra.append_evidence("SPY", [{"level": "L1", "metric": "macro_anchor", "value": "ok"}])
    base_payload = {
        "symbol": "SPY",
        "direction": "short",
        "live": False,
        "entry_price": 100.0,
        "tactical_stop_30m": 105.0,
        "target_price": 85.0,
        "account_equity": 1000000.0,
        "account_currency": "USD",
        "has_l1_l2": True,
        "alignment_status": "aligned_trigger",
        "pair_trade": True,
        "pair_leg_verified": True,
    }
    # Missing pair_leg_symbol -> 422
    r_missing = client.post("/api/trade/plan", json=base_payload)
    assert r_missing.status_code == 422
    assert "pair_leg_symbol" in r_missing.json()["detail"]

    # Same symbol as primary -> 422
    r_same = client.post("/api/trade/plan", json={**base_payload, "pair_leg_symbol": "spy"})
    assert r_same.status_code == 422
    assert "不能与当前主标的" in r_same.json()["detail"]

    # Distinct opposite leg symbol -> allowed to proceed to four-quadrant gate
    r_ok = client.post("/api/trade/plan", json={**base_payload, "pair_leg_symbol": "ETN"})
    assert r_ok.status_code == 200
    assert r_ok.json()["four_quadrant_gate"]["decision"] == "pair_trade"
    assert r_ok.json()["four_quadrant_gate"]["actionable"] is True

    # Even when portfolio is uninitialized AND account_equity is omitted, invalid pair_leg_verified MUST fail with 422 before 409
    no_eq = {k: v for k, v in base_payload.items() if k not in ("account_equity", "account_currency")}
    assert client.post("/api/trade/plan", json={**no_eq, "pair_trade": False}).status_code == 422
    assert client.post("/api/trade/plan", json=no_eq).status_code == 422
    assert client.post("/api/trade/plan", json={**no_eq, "pair_leg_symbol": "SPY"}).status_code == 422


def test_ledger_currency_mismatch_rejects_stale_fx_submission(client: TestClient):
    """Codex Round-3 HIGH #3: USD-context FX submission against a CNY portfolio ledger MUST return 422."""
    r_init = client.post(
        "/api/portfolio/init",
        json={
            "equity": 1000000.0,
            "base_currency": "CNY",
            "default_risk_per_trade_pct": 1.0,
            "max_total_open_risk_pct": 6.0,
            "max_macro_factor_risk_pct": 3.0,
        },
    )
    assert r_init.status_code == 200

    r_mismatch = client.post(
        "/api/trade/plan",
        json={
            "symbol": "USDJPY=X",
            "direction": "long",
            "live": False,
            "entry_price": 150.0,
            "tactical_stop_30m": 149.0,
            "target_price": 153.5,
            "account_currency": "USD",
            "fx_to_account": 0.0067,
            "has_l1_l2": True,
            "alignment_status": "aligned_trigger",
        },
    )
    assert r_mismatch.status_code == 422
    assert "本位币 (CNY) 不一致" in r_mismatch.json()["detail"]



