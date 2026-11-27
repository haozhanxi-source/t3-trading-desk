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
    for seeded_sym, seeded_dir in (("SPY", "long"), ("CU0", "short")):
        ra.init_archive(seeded_sym, direction_bias=seeded_dir)
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


def test_compass_log_opportunity_l1_direction_call_and_abstention_idempotent(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
):
    from backend import macro_compass

    client.post(
        "/api/portfolio/init",
        json={"equity": 100_000.0, "base_currency": "USD", "default_risk_per_trade_pct": 1.0},
    )

    dir_run = {
        "run_id": "compass-test-dir-001",
        "status": "completed",
        "engine": "claude",
        "result": {
            "regime_title": "铜供需缺口多头主线",
            "regime_summary": "矿端干扰叠加库存低位。",
            "abstained": False,
            "focus_scopes": ["commodities"],
            "macro_snapshot_summary": {
                "anchors_items": [
                    {
                        "symbol": "HG=F",
                        "name": "COMEX铜",
                        "last": 4.50,
                        "chanlun_hint": {"latest_zd": 4.10, "latest_zg": 4.65},
                    }
                ]
            },
            "directions": [
                {
                    "id": "copper_deficit",
                    "rank": 1,
                    "title": "铜供给缺口多头",
                    "direction": "long",
                    "horizon": "1Q~2Q",
                    "conviction": "high",
                    "asset_scope": "commodities",
                    "reference_anchors": ["HG=F"],
                    "primary_driver": {"dimension": "商品供需平衡与库存周期", "summary": "矿端扰动"},
                    "secondary_driver": {"dimension": "技术与产业迭代", "summary": "电网需求"},
                    "unpriced_gap": "低库存与扩产滞后尚未充分定价",
                    "thesis": "供需缺口扩大",
                    "leaders": [],
                    "evidence": [],
                }
            ],
        },
    }
    abstain_run = {
        "run_id": "compass-test-abs-002",
        "status": "completed",
        "engine": "claude",
        "result": {
            "regime_title": "全局高波动混沌期：战略留白",
            "regime_summary": "多空无清晰预期差，按宪法第4条留白。",
            "abstained": True,
            "abstain_reason": "信用利差走阔且全市场无通过两维共振与护盾检验的清晰方向，宁缺毋滥。",
            "focus_scopes": [],
            "macro_snapshot_summary": {
                "anchors_items": [
                    {"symbol": "SPY", "name": "标普500", "last": 580.0, "chanlun_hint": {}}
                ]
            },
            "directions": [],
        },
    }

    runs = {dir_run["run_id"]: dir_run, abstain_run["run_id"]: abstain_run}
    monkeypatch.setattr(macro_compass, "get_compass_run", lambda rid: runs.get(rid))

    # 1. Log l1_direction_call -> uses snapshot anchor price (4.50) & Chanlun ZD (4.10)
    r1 = client.post(
        "/api/compass/history/compass-test-dir-001/log-opportunity",
        json={"kind": "l1_direction_call", "card_id": "copper_deficit"},
    )
    assert r1.status_code == 200
    b1 = r1.json()
    assert b1["idempotent_replay"] is False
    assert b1["stop_is_proxy"] is False
    assert b1["opportunity"]["kind"] == "l1_direction_call"
    assert b1["opportunity"]["symbol"] == "HG=F"
    assert b1["opportunity"]["market"] == "US_FUTURES"
    assert b1["opportunity"]["anchor_price"] == 4.5
    assert b1["opportunity"]["planned_stop_price"] == 4.1

    # 2. Repeat call with lowercase/alias symbol hits idempotency (no duplicate row)
    r2 = client.post(
        "/api/compass/history/compass-test-dir-001/log-opportunity",
        json={"kind": "l1_direction_call", "card_id": "copper_deficit", "symbol": "铜"},
    )
    assert r2.status_code == 200
    b2 = r2.json()
    assert b2["idempotent_replay"] is True
    assert b2["opportunity"]["opportunity_id"] == b1["opportunity"]["opportunity_id"]

    # 2b. Non-anchor symbol falls back to market_snapshot.get_snapshot (real signature + technicals.last_close)
    #     and subsequent repeat click after live price drift still replays idempotently (no HTTP 409)
    import market_snapshot

    live_prices = iter([120.5, 128.0])

    def _fake_get_snapshot(raw_symbol: str, *, period: str = "3y", include_macro: bool = True, include_fundamentals: bool = True):
        assert period == "3mo"
        assert include_macro is False
        assert include_fundamentals is False
        return {"symbol": raw_symbol, "technicals": {"last_close": next(live_prices)}}

    monkeypatch.setattr(market_snapshot, "get_snapshot", _fake_get_snapshot)
    r_nvda1 = client.post(
        "/api/compass/history/compass-test-dir-001/log-opportunity",
        json={"kind": "l1_direction_call", "card_id": "copper_deficit", "symbol": "CU0"},
    )
    assert r_nvda1.status_code == 200
    bn1 = r_nvda1.json()
    assert bn1["idempotent_replay"] is False
    assert bn1["opportunity"]["symbol"] == "CU0"
    assert bn1["opportunity"]["market"] == "CN_FUTURES"
    assert bn1["opportunity"]["anchor_price"] == 120.5

    r_nvda2 = client.post(
        "/api/compass/history/compass-test-dir-001/log-opportunity",
        json={"kind": "l1_direction_call", "card_id": "copper_deficit", "symbol": "CU0"},
    )
    assert r_nvda2.status_code == 200
    bn2 = r_nvda2.json()
    assert bn2["idempotent_replay"] is True
    assert bn2["opportunity"]["opportunity_id"] == bn1["opportunity"]["opportunity_id"]
    assert bn2["opportunity"]["anchor_price"] == 120.5

    # 3. Attempting l1_abstention on non-abstained run -> HTTP 422
    r_bad_abs = client.post(
        "/api/compass/history/compass-test-dir-001/log-opportunity",
        json={"kind": "l1_abstention"},
    )
    assert r_bad_abs.status_code == 422

    # 4. Log l1_abstention on abstained run -> uses SPY snapshot (580.0) and 8% proxy stop
    r3 = client.post(
        "/api/compass/history/compass-test-abs-002/log-opportunity",
        json={"kind": "l1_abstention"},
    )
    assert r3.status_code == 200
    b3 = r3.json()
    assert b3["stop_is_proxy"] is True
    assert b3["opportunity"]["kind"] == "l1_abstention"
    assert b3["opportunity"]["symbol"] == "SPY"
    assert b3["opportunity"]["anchor_price"] == 580.0
    assert b3["opportunity"]["planned_stop_price"] == pytest.approx(580.0 * 0.92, abs=1e-4)

    # 5. Attribution analytics reflects both L1 shadow ledger counts
    r_attr = client.get("/api/portfolio/attribution")
    assert r_attr.status_code == 200
    ss = r_attr.json()["shadow_summary"]
    assert ss["l1_direction_call_count"] == 2
    assert ss["l1_abstention_count"] == 1



def test_portfolio_void_mistaken_entry_preserves_equity_and_rejects_trimmed(client: TestClient):
    client.post(
        "/api/portfolio/init",
        json={"equity": 1_000_000.0, "base_currency": "USD", "default_risk_per_trade_pct": 3.0},
    )

    # 1. Open a mistaken position (e.g. NI0 typed at 150000 instead of 115210)
    r_open = client.post(
        "/api/portfolio/positions/open",
        json={
            "symbol": "NI0",
            "direction": "long",
            "sector_theme": "民族资源主义",
            "entry_price": 150_000.0,
            "tactical_stop_30m": 140_000.0,
            "quantity": 19,
            "fx_to_account": 0.150179,
        },
    )
    assert r_open.status_code == 200
    pid = r_open.json()["position"]["position_id"]

    # 2. Void it cleanly: equity stays 1,000,000, closed_positions_count stays 0, sector slot freed
    r_void = client.post(f"/api/portfolio/positions/{pid}/void", json={"reason": "误填入场价 150000，作废重录"})
    assert r_void.status_code == 200
    assert r_void.json()["status"] == "voided"

    summary_res = client.get("/api/portfolio/summary").json()
    assert summary_res["summary"]["equity"] == 1_000_000.0
    assert summary_res["summary"]["open_positions_count"] == 0
    assert summary_res["summary"]["closed_positions_count"] == 0
    assert any(j.get("action") == "void" and j.get("position_id") == pid for j in summary_res["journal"])

    # 3. Re-open NI0 with corrected entry/stop succeeds immediately in the same sector
    r_reopen = client.post(
        "/api/portfolio/positions/open",
        json={
            "symbol": "NI0",
            "direction": "long",
            "sector_theme": "民族资源主义",
            "entry_price": 115_210.0,
            "tactical_stop_30m": 114_000.0,
            "quantity": 19,
            "fx_to_account": 0.150179,
        },
    )
    assert r_reopen.status_code == 200
    pid2 = r_reopen.json()["position"]["position_id"]

    # 4. Once trimmed (partial PnL booked), voiding is forbidden (422)
    r_trim = client.post(
        f"/api/portfolio/positions/{pid2}/trim",
        json={"trim_price": 116_000.0, "trim_quantity": 5, "reason": "partial"},
    )
    assert r_trim.status_code == 200
    r_void_fail = client.post(f"/api/portfolio/positions/{pid2}/void", json={"reason": "try void after trim"})
    assert r_void_fail.status_code == 422
    assert "void_requires_untrimmed_initial_position" in r_void_fail.json()["detail"]



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
    ra.init_archive("SPY", direction_bias="short")
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


# ---------------------------------------------------------------------------
# 9. Iteration 2A-2: Scale-in API, Archive Status Sync, Archive Init/State L1 Fields, Trade Plan Bias
# ---------------------------------------------------------------------------
def test_api_portfolio_scale_in_and_archive_status_sync(client: TestClient):
    # Initialize portfolio ($100k, 1% = $1,000 single-sector risk cap)
    assert client.post(
        "/api/portfolio/init",
        json={"equity": 100_000.0, "base_currency": "USD", "default_risk_per_trade_pct": 1.0},
    ).status_code == 200

    # Create research archives for NVDA and AVGO (both start as watchlist)
    assert client.post(
        "/api/research/archive/init",
        json={"symbol": "NVDA", "name": "NVIDIA", "direction_bias": "long", "layer1_primary": "技术迭代"},
    ).status_code == 200
    assert client.post(
        "/api/research/archive/init",
        json={"symbol": "AVGO", "name": "Broadcom", "direction_bias": "long", "layer1_primary": "技术迭代"},
    ).status_code == 200
    assert client.get("/api/research/archive", params={"symbol": "NVDA"}).json()["state"]["status"] == "watchlist"

    # 1) Open NVDA -> auto-syncs NVDA archive status to "holding"
    r_open = client.post(
        "/api/portfolio/positions/open",
        json={
            "symbol": "NVDA",
            "direction": "long",
            "sector_theme": "ai_compute",
            "entry_price": 100.0,
            "tactical_stop_30m": 95.0,
            "quantity": 100,  # $500 risk (0.5%)
            "macro_factors": ["us_real_rate"],
        },
    )
    assert r_open.status_code == 200
    open_body = r_open.json()
    pid = open_body["position"]["position_id"]
    assert open_body["archive_status_sync"]["synced"] is True
    assert open_body["archive_status_sync"]["current_status"] == "holding"
    assert client.get("/api/research/archive", params={"symbol": "NVDA"}).json()["state"]["status"] == "holding"

    # 2) Scale-in validation errors MUST return HTTP 400 with clear Chinese detail
    #    (a) Floating loss / breakeven averaging down
    r_loss = client.post(
        f"/api/portfolio/positions/{pid}/scale-in",
        json={"add_quantity": 50, "add_price": 98.0},
    )
    assert r_loss.status_code == 400
    assert "浮亏或平价摊平" in r_loss.json()["detail"]

    r_flat = client.post(
        f"/api/portfolio/positions/{pid}/scale-in",
        json={"add_quantity": 50, "add_price": 100.0},
    )
    assert r_flat.status_code == 400
    assert "浮亏或平价摊平" in r_flat.json()["detail"]

    #    (b) Stop widening
    r_widen = client.post(
        f"/api/portfolio/positions/{pid}/scale-in",
        json={"add_quantity": 50, "add_price": 110.0, "new_stop_loss": 92.0},
    )
    assert r_widen.status_code == 400
    assert "移动止损只紧不松" in r_widen.json()["detail"]

    #    (c) Over sector risk cap without tightening stop
    r_over_cap = client.post(
        f"/api/portfolio/positions/{pid}/scale-in",
        json={"add_quantity": 100, "add_price": 115.0},
    )
    assert r_over_cap.status_code == 400
    assert "保本/浮盈区释放风险额度" in r_over_cap.json()["detail"]

    #    (d) Unknown position_id -> HTTP 404
    r_404 = client.post(
        "/api/portfolio/positions/pos_nonexistent/scale-in",
        json={"add_quantity": 10, "add_price": 115.0},
    )
    assert r_404.status_code == 404

    # 3) Valid scale-in with stop tightened to profit/breakeven zone -> HTTP 200 + updated summary
    r_ok = client.post(
        f"/api/portfolio/positions/{pid}/scale-in",
        json={
            "add_quantity": 50,
            "add_price": 115.0,
            "new_stop_loss": 106.0,
            "note": "T2 浮盈加仓锁利",
        },
    )
    assert r_ok.status_code == 200
    body = r_ok.json()
    assert body["new_quantity"] == 150
    assert body["new_entry_price"] == pytest.approx(105.0, abs=1e-6)
    assert body["new_stop_loss"] == 106.0
    assert body["new_risk_amount"] == 0.0
    assert body["new_risk_pct"] == 0.0
    assert body["tranche_count"] == 2
    assert body["summary"]["open_positions_count"] == 1
    assert body["summary"]["total_open_risk_amount"] == 0.0

    # 4) Rotate NVDA -> AVGO auto-syncs NVDA to watchlist and AVGO to holding
    r_rot = client.post(
        f"/api/portfolio/positions/{pid}/rotate",
        json={
            "exit_price": 120.0,
            "exit_reason": "switch_to_avgo",
            "new_position": {
                "symbol": "AVGO",
                "direction": "long",
                "sector_theme": "ai_compute",
                "entry_price": 180.0,
                "tactical_stop_30m": 170.0,
                "quantity": 50,
            },
        },
    )
    assert r_rot.status_code == 200
    avgo_pid = r_rot.json()["opened_position"]["position_id"]
    assert client.get("/api/research/archive", params={"symbol": "NVDA"}).json()["state"]["status"] == "watchlist"
    assert client.get("/api/research/archive", params={"symbol": "AVGO"}).json()["state"]["status"] == "holding"

    # 5) Close AVGO -> AVGO returns to watchlist
    r_close = client.post(
        f"/api/portfolio/positions/{avgo_pid}/close",
        json={"exit_price": 190.0, "exit_reason": "take_profit"},
    )
    assert r_close.status_code == 200
    assert client.get("/api/research/archive", params={"symbol": "AVGO"}).json()["state"]["status"] == "watchlist"

    # 6) Open & void NVDA -> NVDA returns from holding to watchlist
    r_open2 = client.post(
        "/api/portfolio/positions/open",
        json={
            "symbol": "NVDA",
            "direction": "long",
            "sector_theme": "ai_compute",
            "entry_price": 120.0,
            "tactical_stop_30m": 115.0,
            "quantity": 50,
        },
    )
    assert r_open2.status_code == 200
    pid2 = r_open2.json()["position"]["position_id"]
    assert client.get("/api/research/archive", params={"symbol": "NVDA"}).json()["state"]["status"] == "holding"
    r_void = client.post(f"/api/portfolio/positions/{pid2}/void", json={"reason": "误录作废"})
    assert r_void.status_code == 200
    assert client.get("/api/research/archive", params={"symbol": "NVDA"}).json()["state"]["status"] == "watchlist"


def test_api_archive_init_and_state_direction_and_l1_drivers(client: TestClient):
    # 1) Init archive with direction_bias, layer1_primary, layer1_secondary, thesis_summary, confidence
    r_init = client.post(
        "/api/research/archive/init",
        json={
            "symbol": "CU0",
            "name": "沪铜",
            "direction_bias": "short",
            "layer1_primary": "商品供需平衡预期",
            "layer1_secondary": "宏观双核",
            "thesis_summary": "强现实弱预期劈叉，远期冶炼投产过剩",
            "confidence": "high",
        },
    )
    assert r_init.status_code == 200
    init_state = r_init.json()["state"]
    assert init_state["direction_bias"] == "short"
    assert init_state["layer1_drivers"]["primary"] == "商品供需平衡预期"
    assert init_state["layer1_drivers"]["secondary"] == "宏观双核"
    assert init_state["thesis_summary"] == "强现实弱预期劈叉，远期冶炼投产过剩"
    assert init_state["confidence"] == "high"

    # Confirm persisted on disk via GET
    loaded = client.get("/api/research/archive", params={"symbol": "CU0"}).json()
    assert loaded["state"]["direction_bias"] == "short"
    assert loaded["state"]["layer1_drivers"]["primary"] == "商品供需平衡预期"
    rev = loaded["state"]["revision"]

    # 2) PATCH with flat top-level fields (direction_bias, thesis_summary, layer1_primary, layer1_secondary, status, confidence)
    r_flat = client.patch(
        "/api/research/archive/state",
        json={
            "symbol": "CU0",
            "expected_revision": rev,
            "direction_bias": "long",
            "thesis_summary": "矿端扰动超预期转缺口",
            "layer1_primary": "地缘与资源国政策",
            "layer1_secondary": "库存周期",
            "status": "actionable_long",
            "confidence": "medium",
        },
    )
    assert r_flat.status_code == 200
    st_flat = r_flat.json()
    assert st_flat["direction_bias"] == "long"
    assert st_flat["thesis_summary"] == "矿端扰动超预期转缺口"
    assert st_flat["layer1_drivers"]["primary"] == "地缘与资源国政策"
    assert st_flat["layer1_drivers"]["secondary"] == "库存周期"
    assert st_flat["status"] == "actionable_long"
    assert st_flat["confidence"] == "medium"

    # 3) PATCH with nested patch containing layer1_primary / layer1_secondary / layer1_drivers
    r_nested = client.patch(
        "/api/research/archive/state",
        json={
            "symbol": "CU0",
            "expected_revision": st_flat["revision"],
            "patch": {
                "direction_bias": "neutral",
                "layer1_primary": "宏观双核",
                "confidence": "low",
            },
        },
    )
    assert r_nested.status_code == 200
    st_nested = r_nested.json()
    assert st_nested["direction_bias"] == "neutral"
    assert st_nested["layer1_drivers"]["primary"] == "宏观双核"
    assert st_nested["layer1_drivers"]["secondary"] == "库存周期"
    assert st_nested["confidence"] == "low"

    # 4) Invalid direction_bias -> HTTP 422
    r_bad = client.patch(
        "/api/research/archive/state",
        json={"symbol": "CU0", "direction_bias": "bullish_invalid"},
    )
    assert r_bad.status_code == 422


def test_api_trade_plan_forwards_archive_direction_bias(client: TestClient, monkeypatch: pytest.MonkeyPatch):
    client.post(
        "/api/research/archive/init",
        json={
            "symbol": "CU0",
            "direction_bias": "short",
            "layer1_primary": "商品供需平衡预期",
            "thesis_summary": "远期过剩",
        },
    )
    ra.append_evidence("CU0", [{"level": "L1", "kind": "fact", "metric": "smelter_cap", "value": "100"}])

    captured: dict[str, Any] = {}
    orig_build = tp.build_trade_plan

    def spy_build(args: Any) -> dict[str, Any]:
        captured["research_direction"] = getattr(args, "research_direction", None)
        captured["research_direction_bias"] = getattr(args, "research_direction_bias", None)
        captured["direction_bias"] = getattr(args, "direction_bias", None)
        return orig_build(args)

    monkeypatch.setattr(tp, "build_trade_plan", spy_build)

    # 1) Archived symbol aligned direction: archive="short", plan="short" -> actionable_short
    r_arch = client.post(
        "/api/trade/plan",
        json={
            "symbol": "CU0",
            "direction": "short",
            "live": False,
            "entry_price": 75000.0,
            "tactical_stop_30m": 76000.0,
            "target_price": 70000.0,
            "account_equity": 1_000_000.0,
            "account_currency": "CNY",
            "alignment_status": "aligned_trigger",
        },
    )
    assert r_arch.status_code == 200
    assert captured["research_direction"] == "short"
    assert captured["research_direction_bias"] == "short"
    assert captured["direction_bias"] == "short"
    body_arch = r_arch.json()
    assert body_arch["research_direction_bias"] == "short"
    assert body_arch["archive_context"]["exists"] is True
    assert body_arch["archive_context"]["direction_bias"] == "short"
    assert body_arch["four_quadrant_gate"]["decision"] == "actionable_short"

    # 2) Archived symbol conflicting direction: archive="short", plan="long" -> rejected_direction_conflict
    r_conflict = client.post(
        "/api/trade/plan",
        json={
            "symbol": "CU0",
            "direction": "long",
            "live": False,
            "entry_price": 75000.0,
            "tactical_stop_30m": 74000.0,
            "target_price": 80000.0,
            "account_equity": 1_000_000.0,
            "account_currency": "CNY",
            "alignment_status": "aligned_trigger",
        },
    )
    assert r_conflict.status_code == 200
    assert r_conflict.json()["four_quadrant_gate"]["decision"] == "rejected_direction_conflict"
    assert r_conflict.json()["four_quadrant_gate"]["actionable"] is False

    # 3) Archived symbol neutral direction -> watchlist_research_pending
    client.post("/api/research/archive/init", json={"symbol": "GC=F", "direction_bias": "neutral"})
    ra.append_evidence("GC=F", [{"level": "L1", "kind": "fact", "metric": "real_yield", "value": "1.8"}])
    r_neutral = client.post(
        "/api/trade/plan",
        json={
            "symbol": "GC=F",
            "direction": "long",
            "live": False,
            "entry_price": 2500.0,
            "tactical_stop_30m": 2480.0,
            "target_price": 2600.0,
            "account_equity": 500_000.0,
            "account_currency": "USD",
            "alignment_status": "aligned_trigger",
        },
    )
    assert r_neutral.status_code == 200
    assert r_neutral.json()["four_quadrant_gate"]["decision"] == "watchlist_research_pending"
    assert r_neutral.json()["four_quadrant_gate"]["actionable"] is False

    # 4) Unarchived symbol: request-supplied direction_bias forwarded losslessly
    r_unarch = client.post(
        "/api/trade/plan",
        json={
            "symbol": "NVDA",
            "direction": "long",
            "live": False,
            "entry_price": 120.0,
            "tactical_stop_30m": 115.0,
            "target_price": 140.0,
            "account_equity": 100_000.0,
            "account_currency": "USD",
            "direction_bias": "long",
            "alignment_status": "aligned_trigger",
        },
    )
    assert r_unarch.status_code == 200
    assert captured["research_direction"] == "long"
    assert captured["research_direction_bias"] == "long"
    assert captured["direction_bias"] == "long"
    assert r_unarch.json()["research_direction_bias"] == "long"
    assert r_unarch.json()["archive_context"]["exists"] is False


def test_market_dual_does_not_cache_intraday_error_response(client: TestClient, monkeypatch: pytest.MonkeyPatch):
    """Covers healthy cache -> forced refresh timeout (no cache poison) -> normal request recovery + epoch-safe zero-TTL."""
    from backend.cache import TTLCache

    calls = {"n": 0}

    def fake_get_snapshot(symbol: str, **_kwargs: Any) -> dict[str, Any]:
        calls["n"] += 1
        base = {
            "symbol": symbol,
            "asset_class": "cn_future",
            "ohlcv_daily": [{"time": "2026-11-25", "label": "2026-11-25", "open": 88000, "high": 88400, "low": 82220, "close": 82620, "volume": 367219}],
            "ohlcv_intraday": [{"time": 1795590000, "label": "2026-11-25T15:00+08:00", "open": 82820, "high": 83000, "low": 82300, "close": 82620, "volume": 19643}],
            "chanlun": {"daily": {}, "intraday": {}},
        }
        if calls["n"] == 2:
            base["intraday_error"] = "Read timed out"
            base["intraday_stale_fallback"] = True
        return base

    monkeypatch.setattr(market_snapshot, "get_snapshot", fake_get_snapshot)

    # 1) Initial healthy request populates cache
    r0 = client.get("/api/market/dual", params={"symbol": "LC0", "intraday_interval": "30m"})
    assert r0.status_code == 200
    assert r0.headers.get("X-Cache") == "MISS"
    assert "intraday_error" not in r0.json()

    # 2) Forced refresh hits intraday timeout -> returns stale_fallback with intraday_error, MUST NOT cache for 60s
    r1 = client.get("/api/market/dual", params={"symbol": "LC0", "intraday_interval": "30m", "refresh": "true"})
    assert r1.status_code == 200
    assert r1.headers.get("X-Cache") == "MISS"
    assert r1.json()["intraday_error"] == "Read timed out"
    assert r1.json()["intraday_stale_fallback"] is True

    # 3) Next normal request recovers immediately (not poisoned by degraded response)
    r2 = client.get("/api/market/dual", params={"symbol": "LC0", "intraday_interval": "30m"})
    assert r2.status_code == 200
    assert r2.headers.get("X-Cache") == "MISS"
    assert "intraday_error" not in r2.json()
    assert calls["n"] == 3

    # 4) Healthy response IS cached on subsequent request
    r3 = client.get("/api/market/dual", params={"symbol": "LC0", "intraday_interval": "30m"})
    assert r3.status_code == 200
    assert r3.headers.get("X-Cache") == "HIT"
    assert calls["n"] == 3

    # 5) Zero-TTL invalidate respects epoch across clear() so slow degraded request cannot evict newer entry
    local_cache = TTLCache()
    entered = threading.Event()
    release = threading.Event()

    def slow_degraded():
        entered.set()
        release.wait(timeout=5)
        return {"intraday_error": "timeout"}

    t_slow = threading.Thread(
        target=lambda: local_cache.get_or_compute(
            ("dual", "LC0"),
            60.0,
            slow_degraded,
            refresh=True,
            ttl_for_value=lambda v: 0.0 if v.get("intraday_error") else 60.0,
        )
    )
    t_slow.start()
    assert entered.wait(timeout=5)
    local_cache.clear()
    local_cache.set(("dual", "LC0"), {"ok": True}, 60.0)
    release.set()
    t_slow.join(timeout=5)
    hit, val = local_cache.get(("dual", "LC0"))
    assert hit is True
    assert val == {"ok": True}


def test_p1_3_pair_trade_enforces_us_short_and_blocks_a_share_short_leg(client: TestClient):
    """P1-3: Pair trade MUST NOT bypass Option A US-short verdict or allow an A-share short opposing leg."""
    # Case 1: NVDA archive is direction_bias="pair" with L1 -> us_short_thesis_verified=False -> MUST NOT return pair_trade
    ra.init_archive("NVDA", direction_bias="pair")
    ra.append_evidence("NVDA", [{"level": "L1", "metric": "capex_check", "value": "ok"}])
    r_nvda = client.post(
        "/api/trade/plan",
        json={
            "symbol": "NVDA",
            "direction": "short",
            "live": False,
            "entry_price": 120.0,
            "tactical_stop_30m": 125.0,
            "target_price": 100.0,
            "account_equity": 1000000.0,
            "account_currency": "USD",
            "alignment_status": "aligned_trigger",
            "pair_trade": True,
            "pair_leg_verified": True,
            "pair_leg_symbol": "AMD",
        },
    )
    assert r_nvda.status_code == 200
    gate_nvda = r_nvda.json()["four_quadrant_gate"]
    assert gate_nvda["decision"] == "watchlist"
    assert gate_nvda["actionable"] is False

    # Case 2: Long PWR + opposing A-share short leg 600519 -> MUST unconditionally reject
    ra.init_archive("PWR", direction_bias="long")
    ra.append_evidence("PWR", [{"level": "L1", "metric": "grid_backlog", "value": "record"}])
    r_a_opp = client.post(
        "/api/trade/plan",
        json={
            "symbol": "PWR",
            "direction": "long",
            "live": False,
            "entry_price": 300.0,
            "tactical_stop_30m": 285.0,
            "target_price": 360.0,
            "account_equity": 1000000.0,
            "account_currency": "USD",
            "alignment_status": "aligned_trigger",
            "pair_trade": True,
            "pair_leg_verified": True,
            "pair_leg_symbol": "600519",
        },
    )
    assert r_a_opp.status_code == 200
    gate_a = r_a_opp.json()["four_quadrant_gate"]
    assert gate_a["decision"] == "rejected"
    assert gate_a["actionable"] is False
    assert "A 股受限严格仅做多" in gate_a["reason"]

    # Case 3: Long PWR + opposing US short leg AMD (no short verdict) -> watchlist; after AMD short + L1 -> pair_trade
    r_us_opp_blocked = client.post(
        "/api/trade/plan",
        json={
            "symbol": "PWR",
            "direction": "long",
            "live": False,
            "entry_price": 300.0,
            "tactical_stop_30m": 285.0,
            "target_price": 360.0,
            "account_equity": 1000000.0,
            "account_currency": "USD",
            "alignment_status": "aligned_trigger",
            "pair_trade": True,
            "pair_leg_verified": True,
            "pair_leg_symbol": "AMD",
        },
    )
    assert r_us_opp_blocked.status_code == 200
    assert r_us_opp_blocked.json()["four_quadrant_gate"]["decision"] == "watchlist"
    assert r_us_opp_blocked.json()["four_quadrant_gate"]["actionable"] is False

    ra.init_archive("AMD", direction_bias="short")
    ra.append_evidence("AMD", [{"level": "L1", "metric": "margin_compression", "value": "confirmed"}])
    r_us_opp_ok = client.post(
        "/api/trade/plan",
        json={
            "symbol": "PWR",
            "direction": "long",
            "live": False,
            "entry_price": 300.0,
            "tactical_stop_30m": 285.0,
            "target_price": 360.0,
            "account_equity": 1000000.0,
            "account_currency": "USD",
            "alignment_status": "aligned_trigger",
            "pair_trade": True,
            "pair_leg_verified": True,
            "pair_leg_symbol": "AMD",
        },
    )
    assert r_us_opp_ok.status_code == 200
    assert r_us_opp_ok.json()["four_quadrant_gate"]["decision"] == "pair_trade"
    assert r_us_opp_ok.json()["four_quadrant_gate"]["actionable"] is True


def test_p2_4_archive_init_single_write_and_overwrite_increments_revision(client: TestClient):
    """P2-4: /api/research/archive/init must stay at revision=1 on creation and increment revision on overwrite=True."""
    r1 = client.post(
        "/api/research/archive/init",
        json={
            "symbol": "ETN",
            "name": "Eaton",
            "direction_bias": "long",
            "layer1_primary": "维度3电力电网瓶颈",
            "layer1_secondary": "维度1美国制造业回流",
            "confidence": "HIGH",
            "thesis_summary": "电网变压器与数据中心配电核心龙头",
        },
    )
    assert r1.status_code == 200
    st1 = r1.json()["state"]
    assert st1["revision"] == 1
    assert st1["direction_bias"] == "long"
    assert st1["confidence"] == "high"
    assert st1["layer1_drivers"]["primary"] == "维度3电力电网瓶颈"
    assert st1["layer1_drivers"]["secondary"] == "维度1美国制造业回流"

    # Overwrite=True MUST increment revision to 2 (not reset to 1)
    r2 = client.post(
        "/api/research/archive/init",
        json={
            "symbol": "ETN",
            "name": "Eaton Rebuilt",
            "direction_bias": "short",
            "layer1_primary": "维度4产能过剩",
            "confidence": "medium",
            "thesis_summary": "重建档案",
            "overwrite": True,
        },
    )
    assert r2.status_code == 200
    st2 = r2.json()["state"]
    assert st2["revision"] == 2
    assert st2["direction_bias"] == "short"

    # Stale client holding expected_revision=1 MUST get HTTP 409 Conflict
    r_stale = client.patch(
        "/api/research/archive/state",
        json={"symbol": "ETN", "expected_revision": 1, "confidence": "low"},
    )
    assert r_stale.status_code == 409


# ---------------------------------------------------------------------------
# 10. Asymmetric Alpha Engine API Integration Tests
# ---------------------------------------------------------------------------
def test_asymmetric_alpha_archive_expectation_gap_patch_and_trade_plan(client: TestClient):
    """Verify PATCH /api/research/archive/state expectation_gap semantics and /api/trade/plan propagation."""
    r_init = client.post(
        "/api/research/archive/init",
        json={
            "symbol": "PWR",
            "name": "Quanta Services",
            "direction_bias": "long",
            "layer1_primary": "电力与电网设备瓶颈",
            "layer1_secondary": "AI算力资本开支周期",
        },
    )
    assert r_init.status_code == 200
    assert r_init.json()["state"]["expectation_gap"] == {
        "priced_in_consensus": "",
        "our_variant_view": "",
        "crowding_assessment": "unknown",
        "leading_kpis": [],
    }

    # 1) Full expectation_gap dict patch
    full_eg = {
        "priced_in_consensus": "市场定价 FY27 营收增速回落至 11%",
        "our_variant_view": "特高压变电站与数据中心并网积压推动 FY27 增速维持 18%+",
        "crowding_assessment": "underpriced",
        "leading_kpis": [
            {
                "name": "12-Month Remaining Performance Obligations",
                "current_baseline": "$35.2B",
                "expected_trajectory": "QoQ 增速转正超过 6%",
            }
        ],
    }
    r_full = client.patch(
        "/api/research/archive/state",
        json={"symbol": "PWR", "expected_revision": 1, "expectation_gap": full_eg},
    )
    assert r_full.status_code == 200
    st2 = r_full.json()
    assert st2["revision"] == 2
    assert st2["expectation_gap"]["priced_in_consensus"] == full_eg["priced_in_consensus"]
    assert st2["expectation_gap"]["our_variant_view"] == full_eg["our_variant_view"]
    assert st2["expectation_gap"]["crowding_assessment"] == "underpriced"
    assert len(st2["expectation_gap"]["leading_kpis"]) == 1

    # 2) Omitting expectation_gap when patching another field MUST preserve existing expectation_gap
    r_omit = client.patch(
        "/api/research/archive/state",
        json={"symbol": "PWR", "expected_revision": 2, "thesis_summary": "电网主干网改造超级周期"},
    )
    assert r_omit.status_code == 200
    st3 = r_omit.json()
    assert st3["thesis_summary"] == "电网主干网改造超级周期"
    assert st3["expectation_gap"]["crowding_assessment"] == "underpriced"
    assert st3["expectation_gap"]["priced_in_consensus"] == full_eg["priced_in_consensus"]
    assert len(st3["expectation_gap"]["leading_kpis"]) == 1

    # 3) Partial merge updates only crowding_assessment while keeping other subfields intact
    r_partial = client.patch(
        "/api/research/archive/state",
        json={
            "symbol": "PWR",
            "expected_revision": 3,
            "expectation_gap": {"crowding_assessment": "crowded"},
        },
    )
    assert r_partial.status_code == 200
    st4 = r_partial.json()
    assert st4["expectation_gap"]["crowding_assessment"] == "crowded"
    assert st4["expectation_gap"]["priced_in_consensus"] == full_eg["priced_in_consensus"]
    assert st4["expectation_gap"]["our_variant_view"] == full_eg["our_variant_view"]
    assert len(st4["expectation_gap"]["leading_kpis"]) == 1

    # Verify /api/trade/plan exposes archive expectation_gap_summary and offensive_campaign unstripped
    ra.append_evidence("PWR", [{"level": "L1", "metric": "backlog", "value": "35.2B"}])
    r_plan = client.post(
        "/api/trade/plan",
        json={
            "symbol": "PWR",
            "direction": "long",
            "live": False,
            "entry_price": 300.0,
            "tactical_stop_30m": 288.0,
            "structural_stop_daily": 270.0,
            "target_price": 360.0,
            "account_equity": 200_000.0,
            "account_currency": "USD",
            "alignment_status": "aligned_trigger",
        },
    )
    assert r_plan.status_code == 200
    plan_body = r_plan.json()
    assert plan_body["expectation_gap_summary"]["crowding_assessment"] == "crowded"
    assert plan_body["expectation_gap_summary"]["leading_kpis_count"] == 1
    assert plan_body["archive_context"]["expectation_gap"]["crowding_assessment"] == "crowded"
    assert isinstance(plan_body["position_plan"].get("offensive_campaign"), dict)
    assert plan_body["position_plan"]["offensive_campaign"]["campaign_1r_amount"] > 0

    # 4) Invalid crowding_assessment -> clean 4xx validation error (never 500)
    r_bad = client.patch(
        "/api/research/archive/state",
        json={
            "symbol": "PWR",
            "expectation_gap": {"crowding_assessment": "mega_overheated"},
        },
    )
    assert r_bad.status_code in (400, 422)
    assert "crowding_assessment" in r_bad.json()["detail"]

    # Conflicting top-level expectation_gap vs patch.expectation_gap -> 422
    r_conflict_eg = client.patch(
        "/api/research/archive/state",
        json={
            "symbol": "PWR",
            "expectation_gap": None,
            "patch": {"expectation_gap": {"crowding_assessment": "fair"}},
        },
    )
    assert r_conflict_eg.status_code == 422

    # 5) Explicit null resets expectation_gap to clean defaults
    r_reset = client.patch(
        "/api/research/archive/state",
        json={"symbol": "PWR", "expected_revision": 4, "expectation_gap": None},
    )
    assert r_reset.status_code == 200
    st5 = r_reset.json()
    assert st5["revision"] == 5
    assert st5["expectation_gap"] == {
        "priced_in_consensus": "",
        "our_variant_view": "",
        "crowding_assessment": "unknown",
        "leading_kpis": [],
    }


def test_asymmetric_alpha_campaign_scale_in_stop_regime_and_attribution_lifecycle(client: TestClient):
    """End-to-end API flow: open(campaign_1r) -> scale-in(breakeven lock) -> stop-regime(daily) -> trim -> close -> rotate."""
    assert (
        client.post(
            "/api/portfolio/init",
            json={
                "equity": 100_000.0,
                "base_currency": "USD",
                "default_risk_per_trade_pct": 1.0,
                "max_total_open_risk_pct": 6.0,
                "max_macro_factor_risk_pct": 3.0,
            },
        ).status_code
        == 200
    )

    # 1) Open pilot position with explicit campaign_1r_amount=1000 (0.5R pilot risk = $500)
    r_open = client.post(
        "/api/portfolio/positions/open",
        json={
            "symbol": "NVDA",
            "direction": "long",
            "sector_theme": "ai_compute",
            "entry_price": 100.0,
            "tactical_stop_30m": 95.0,
            "structural_stop_daily": 92.0,
            "target_price": 145.0,
            "quantity": 100,
            "campaign_1r_amount": 1000.0,
            "macro_factors": ["us_real_rate"],
        },
    )
    assert r_open.status_code == 200
    pos = r_open.json()["position"]
    pid = pos["position_id"]
    assert pos["campaign_1r_amount"] == 1000.0
    assert pos["lifecycle_stage"] == "pilot"
    assert pos["active_stop_regime"] == "tactical_30m"

    # 2) Scale-in with require_breakeven_lock=True rejected when stop is below post-add avg entry -> HTTP 400
    r_scale_reject = client.post(
        f"/api/portfolio/positions/{pid}/scale-in",
        json={
            "add_quantity": 100,
            "add_price": 112.0,
            "new_stop_loss": 102.0,  # avg entry would be 106.0 > 102.0
            "require_breakeven_lock": True,
            "max_profit_cushion_ratio": 0.65,
        },
    )
    assert r_scale_reject.status_code == 400
    assert "breakeven" in r_scale_reject.json()["detail"].lower() or "保本" in r_scale_reject.json()["detail"]

    # 3) Scale-in with require_breakeven_lock=True accepted when locked at breakeven (106.0) -> HTTP 200
    r_scale_ok = client.post(
        f"/api/portfolio/positions/{pid}/scale-in",
        json={
            "add_quantity": 100,
            "add_price": 112.0,
            "new_stop_loss": 106.0,
            "new_daily_stop": 100.0,
            "require_breakeven_lock": True,
            "max_profit_cushion_ratio": 0.65,
            "note": "T2 保本推仓",
        },
    )
    assert r_scale_ok.status_code == 200
    scale_body = r_scale_ok.json()
    assert scale_body["new_quantity"] == 200
    assert scale_body["new_entry_price"] == pytest.approx(106.0, abs=1e-6)
    assert scale_body["new_risk_amount"] == 0.0
    assert scale_body["position"]["lifecycle_stage"] == "pyramided"
    assert scale_body["summary"]["open_positions_count"] == 1

    # 4) Handover stop-regime to daily_structure rejected when daily stop < avg_entry (106.0) -> HTTP 400
    r_regime_reject = client.post(
        f"/api/portfolio/positions/{pid}/stop-regime",
        json={
            "regime": "daily_structure",
            "new_daily_stop": 103.0,
            "current_price": 122.0,
            "reason": "attempt_below_breakeven",
        },
    )
    assert r_regime_reject.status_code == 400
    assert "daily_stop_below_breakeven_floor" in r_regime_reject.json()["detail"]

    # Unknown position_id on stop-regime -> HTTP 404
    r_regime_404 = client.post(
        "/api/portfolio/positions/pos_missing/stop-regime",
        json={"regime": "daily_structure", "new_daily_stop": 110.0},
    )
    assert r_regime_404.status_code == 404

    # 5) Handover stop-regime to daily_structure accepted when daily stop >= avg_entry (106.0) -> HTTP 200
    r_regime_ok = client.post(
        f"/api/portfolio/positions/{pid}/stop-regime",
        json={
            "regime": "daily_structure",
            "new_daily_stop": 108.0,
            "current_price": 125.0,
            "reason": "handover_to_daily_main_wave",
        },
    )
    assert r_regime_ok.status_code == 200
    reg_body = r_regime_ok.json()
    assert reg_body["position"]["active_stop_regime"] == "daily_structure"
    assert reg_body["position"]["structural_stop_daily"] == 108.0
    assert reg_body["position"]["lifecycle_stage"] == "main_wave"
    assert reg_body["summary"]["open_positions_count"] == 1

    # 6) Trim with attribution_tag & lesson_note (bogus tag rejected cleanly)
    r_trim_bad = client.post(
        f"/api/portfolio/positions/{pid}/trim",
        json={
            "trim_price": 130.0,
            "trim_quantity": 50,
            "attribution_tag": "nonexistent_tag",
        },
    )
    assert r_trim_bad.status_code in (400, 422)

    r_trim_ok = client.post(
        f"/api/portfolio/positions/{pid}/trim",
        json={
            "trim_price": 130.0,
            "trim_quantity": 50,
            "reason": "lock_partial_gain",
            "attribution_tag": "disciplined_trailing_exit",
            "lesson_note": "加速段减仓 25% 锁利",
        },
    )
    assert r_trim_ok.status_code == 200
    assert r_trim_ok.json()["position"]["trims"][0]["attribution_tag"] == "disciplined_trailing_exit"

    # 7) Close with attribution_tag & lesson_note -> verify campaign_r_multiple & attribution_tag
    # Partial PnL: 50 * (130 - 106) = 1200; Remaining PnL: 150 * (136 - 106) = 4500; Total = 5700 -> 5.7R on 1R=1000
    r_close = client.post(
        f"/api/portfolio/positions/{pid}/close",
        json={
            "exit_price": 136.0,
            "exit_reason": "daily_top_divergence",
            "exit_category": "take_profit",
            "attribution_tag": "full_main_wave_ride",
            "lesson_note": "保本推仓后切换日线主升完整吃满",
        },
    )
    assert r_close.status_code == 200
    closed_pos = r_close.json()["position"]
    assert closed_pos["status"] == "closed"
    assert closed_pos["attribution_tag"] == "full_main_wave_ride"
    assert closed_pos["lesson_note"] == "保本推仓后切换日线主升完整吃满"
    assert closed_pos["campaign_1r_amount"] == 1000.0
    assert closed_pos["campaign_r_multiple"] == pytest.approx(5.7, abs=1e-4)

    # 8) Rotate with attribution_tag, lesson_note, and new_position.campaign_1r_amount
    r_open2 = client.post(
        "/api/portfolio/positions/open",
        json={
            "symbol": "PWR",
            "direction": "long",
            "sector_theme": "power_grid",
            "entry_price": 200.0,
            "tactical_stop_30m": 195.0,
            "quantity": 100,
            "campaign_1r_amount": 1000.0,
        },
    )
    assert r_open2.status_code == 200
    pwr_pid = r_open2.json()["position"]["position_id"]

    r_rot = client.post(
        f"/api/portfolio/positions/{pwr_pid}/rotate",
        json={
            "exit_price": 215.0,
            "exit_reason": "switch_to_etn_leader",
            "attribution_tag": "disciplined_trailing_exit",
            "lesson_note": "龙头换仓至 ETN",
            "new_position": {
                "symbol": "ETN",
                "direction": "long",
                "sector_theme": "power_grid",
                "entry_price": 300.0,
                "tactical_stop_30m": 295.0,
                "quantity": 100,
                "campaign_1r_amount": 1200.0,
            },
        },
    )
    assert r_rot.status_code == 200
    rot_body = r_rot.json()
    assert rot_body["closed_position"]["attribution_tag"] == "disciplined_trailing_exit"
    assert rot_body["closed_position"]["lesson_note"] == "龙头换仓至 ETN"
    assert rot_body["opened_position"]["campaign_1r_amount"] == 1200.0

    # Summary preserves all new fields unstripped
    sum_body = client.get("/api/portfolio/summary").json()
    assert sum_body["initialized"] is True
    open_etn = next(p for p in sum_body["positions"] if p["symbol"] == "ETN")
    assert open_etn["campaign_1r_amount"] == 1200.0
    assert open_etn["lifecycle_stage"] == "pilot"
    assert open_etn["active_stop_regime"] == "tactical_30m"
    closed_nvda = next(p for p in sum_body["positions"] if p["symbol"] == "NVDA")
    assert closed_nvda["campaign_r_multiple"] == pytest.approx(5.7, abs=1e-4)
    assert closed_nvda["attribution_tag"] == "full_main_wave_ride"


def test_asymmetric_alpha_shadow_opportunities_and_attribution_analytics(client: TestClient):
    """Verify shadow opportunity CRUD endpoints and post-trade skewness/attribution analytics."""
    # 1) Uninitialized portfolio returns clean zeroed analytics and empty opportunity list
    r_attr0 = client.get("/api/portfolio/attribution")
    assert r_attr0.status_code == 200
    assert r_attr0.json()["closed_count"] == 0
    assert r_attr0.json()["shadow_summary"]["total_count"] == 0

    r_opps0 = client.get("/api/portfolio/opportunities")
    assert r_opps0.status_code == 200
    assert r_opps0.json() == {"opportunities": []}

    # 2) Seed portfolio with 1 multi-R winner, 1 pilot friction loss, and 1 preventable mistake loss
    assert (
        client.post(
            "/api/portfolio/init",
            json={"equity": 100_000.0, "base_currency": "USD", "default_risk_per_trade_pct": 1.0},
        ).status_code
        == 200
    )

    trades = [
        ("NVDA", "ai_compute", 100.0, 95.0, 100, 1000.0, 140.0, "full_main_wave_ride", "主升吃满 4R"),
        ("PWR", "power_grid", 200.0, 195.0, 100, 1000.0, 195.0, "normal_pilot_cost", "正常试仓止损 -0.5R"),
        ("AMD", "semis_alt", 150.0, 145.0, 100, 1000.0, 140.0, "l4_chased_extended", "追高远离中枢 -1.0R"),
    ]
    for sym, sec, entry, stop, qty, camp_1r, exit_p, tag, note in trades:
        ro = client.post(
            "/api/portfolio/positions/open",
            json={
                "symbol": sym,
                "direction": "long",
                "sector_theme": sec,
                "entry_price": entry,
                "tactical_stop_30m": stop,
                "quantity": qty,
                "campaign_1r_amount": camp_1r,
            },
        )
        assert ro.status_code == 200
        pid = ro.json()["position"]["position_id"]
        rc = client.post(
            f"/api/portfolio/positions/{pid}/close",
            json={
                "exit_price": exit_p,
                "exit_reason": "test_close",
                "attribution_tag": tag,
                "lesson_note": note,
            },
        )
        assert rc.status_code == 200

    # 3) Log shadow opportunities (and validate error mapping)
    r_opp_bad = client.post(
        "/api/portfolio/opportunities",
        json={
            "symbol": "GC=F",
            "market": "US_FUTURES",
            "sector_theme": "precious_metals",
            "direction": "long",
            "kind": "missed_entry",
            "anchor_price": 2600.0,
            "planned_stop_price": 2650.0,  # invalid: stop above entry for long
        },
    )
    assert r_opp_bad.status_code == 400
    assert "invalid_planned_stop" in r_opp_bad.json()["detail"]

    r_opp1 = client.post(
        "/api/portfolio/opportunities",
        json={
            "symbol": "GC=F",
            "market": "US_FUTURES",
            "sector_theme": "precious_metals",
            "direction": "long",
            "kind": "missed_entry",
            "anchor_price": 2600.0,
            "planned_stop_price": 2550.0,
            "subsequent_extreme_price": 2800.0,
            "root_cause_tag": "hesitated_at_trigger",
            "lesson_note": "30m二买确认但犹豫未上车",
            "status": "tracking",
        },
    )
    assert r_opp1.status_code == 200
    opp1_body = r_opp1.json()
    opp1_id = opp1_body["opportunity_id"]
    assert opp1_body["missed_r_multiple"] == pytest.approx(4.0, abs=1e-4)
    assert opp1_body["attribution"]["shadow_summary"]["total_count"] == 1

    r_opp2 = client.post(
        "/api/portfolio/opportunities",
        json={
            "symbol": "CU0",
            "market": "CN_FUTURES",
            "sector_theme": "base_metals",
            "direction": "short",
            "kind": "shaken_out",
            "anchor_price": 76000.0,
            "planned_stop_price": 77000.0,
            "subsequent_extreme_price": 73000.0,
            "root_cause_tag": "30m_noise_shakeout",
            "lesson_note": "未切日线防线被30m洗出",
            "status": "reviewed",
        },
    )
    assert r_opp2.status_code == 200
    assert r_opp2.json()["missed_r_multiple"] == pytest.approx(3.0, abs=1e-4)

    # 4) List & filter opportunities (+ invalid query params -> HTTP 400)
    assert len(client.get("/api/portfolio/opportunities").json()["opportunities"]) == 2
    tracking_only = client.get("/api/portfolio/opportunities", params={"status": "tracking"}).json()["opportunities"]
    assert len(tracking_only) == 1 and tracking_only[0]["opportunity_id"] == opp1_id
    shaken_only = client.get("/api/portfolio/opportunities", params={"kind": "shaken_out"}).json()["opportunities"]
    assert len(shaken_only) == 1 and shaken_only[0]["symbol"] == "CU0"

    assert client.get("/api/portfolio/opportunities", params={"status": "bogus"}).status_code == 400
    assert client.get("/api/portfolio/opportunities", params={"kind": "bogus"}).status_code == 400

    # 5) PATCH opportunity selectively (omitted fields preserved; explicit null clears root_cause_tag; missing id -> 404)
    r_upd1 = client.patch(
        f"/api/portfolio/opportunities/{opp1_id}",
        json={"subsequent_extreme_price": 2900.0},
    )
    assert r_upd1.status_code == 200
    upd1 = r_upd1.json()
    assert upd1["missed_r_multiple"] == pytest.approx(6.0, abs=1e-4)
    assert upd1["status"] == "tracking"
    assert upd1["root_cause_tag"] == "hesitated_at_trigger"
    assert upd1["lesson_note"] == "30m二买确认但犹豫未上车"

    r_upd2 = client.patch(
        f"/api/portfolio/opportunities/{opp1_id}",
        json={"status": "reviewed", "root_cause_tag": None},
    )
    assert r_upd2.status_code == 200
    upd2 = r_upd2.json()
    assert upd2["status"] == "reviewed"
    assert upd2["root_cause_tag"] is None
    assert upd2["subsequent_extreme_price"] == 2900.0
    assert upd2["missed_r_multiple"] == pytest.approx(6.0, abs=1e-4)
    assert upd2["attribution"]["shadow_summary"]["total_missed_r"] == pytest.approx(9.0, abs=1e-4)

    assert (
        client.patch(
            "/api/portfolio/opportunities/opp_nonexistent",
            json={"status": "reviewed"},
        ).status_code
        == 404
    )

    # 6) Verify GET /api/portfolio/attribution skewness / payoff / leakage / shadow metrics
    r_attr = client.get("/api/portfolio/attribution")
    assert r_attr.status_code == 200
    attr = r_attr.json()
    assert attr["closed_count"] == 3
    assert attr["avg_win_r"] == pytest.approx(4.0, abs=1e-4)
    assert attr["avg_loss_r"] == pytest.approx(-0.75, abs=1e-4)
    assert attr["payoff_ratio_r"] == pytest.approx(4.0 / 0.75, abs=1e-3)
    assert attr["expectancy_r"] == pytest.approx((4.0 - 0.5 - 1.0) / 3.0, abs=1e-4)
    assert attr["skewness"]["multi_r_winners_count"] == 1
    assert attr["skewness"]["top_20pct_profit_share_pct"] == pytest.approx(100.0, abs=1e-4)
    assert attr["skewness"]["pilot_friction_loss_r"] == pytest.approx(-0.5, abs=1e-4)
    assert attr["skewness"]["preventable_mistake_loss_r"] == pytest.approx(-1.0, abs=1e-4)
    assert attr["shadow_summary"]["total_count"] == 2
    assert attr["shadow_summary"]["total_missed_r"] == pytest.approx(9.0, abs=1e-4)
    assert len(attr["by_tag"]) == 3


def test_opportunity_patch_explicit_null_idempotency_conflict_and_attribution_resilience(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
):
    """Regression suite for Codex API findings #3 and #4 on shadow opportunity endpoints."""
    assert (
        client.post(
            "/api/portfolio/init",
            json={"equity": 100_000.0, "base_currency": "USD", "default_risk_per_trade_pct": 1.0},
        ).status_code
        == 200
    )

    base_payload = {
        "symbol": "GC=F",
        "market": "US_FUTURES",
        "sector_theme": "precious_metals",
        "direction": "long",
        "kind": "missed_entry",
        "anchor_price": 2600.0,
        "planned_stop_price": 2550.0,
        "subsequent_extreme_price": 2800.0,
        "root_cause_tag": "hesitated_at_trigger",
        "lesson_note": "初始记录",
        "status": "tracking",
        "idempotency_key": "opp-create-key-1",
    }

    # 1) POST with idempotency_key -> 200; identical replay -> 200 with no duplicate row
    r1 = client.post("/api/portfolio/opportunities", json=base_payload)
    assert r1.status_code == 200
    opp_id = r1.json()["opportunity_id"]
    assert r1.json()["missed_r_multiple"] == pytest.approx(4.0, abs=1e-4)

    r1_replay = client.post("/api/portfolio/opportunities", json=base_payload)
    assert r1_replay.status_code == 200
    assert r1_replay.json()["opportunity_id"] == opp_id
    assert r1_replay.json().get("idempotent_replay") is True
    assert len(client.get("/api/portfolio/opportunities").json()["opportunities"]) == 1

    # 2) Reused idempotency_key with different payload -> HTTP 409 Conflict
    r1_conflict = client.post(
        "/api/portfolio/opportunities",
        json={**base_payload, "subsequent_extreme_price": 2950.0},
    )
    assert r1_conflict.status_code == 409
    assert "idempotency_key_conflict" in r1_conflict.json()["detail"].lower()
    assert len(client.get("/api/portfolio/opportunities").json()["opportunities"]) == 1

    # 3) PATCH omitting subsequent_extreme_price preserves existing extreme price & metrics
    r_preserve = client.patch(
        f"/api/portfolio/opportunities/{opp_id}",
        json={"lesson_note": "仅补充复盘笔记"},
    )
    assert r_preserve.status_code == 200
    assert r_preserve.json()["subsequent_extreme_price"] == 2800.0
    assert r_preserve.json()["missed_move_pct"] == pytest.approx(7.6923, abs=1e-3)
    assert r_preserve.json()["missed_r_multiple"] == pytest.approx(4.0, abs=1e-4)
    assert r_preserve.json()["lesson_note"] == "仅补充复盘笔记"

    # 4) PATCH with explicit null status -> 422 without mutating
    r_null_status = client.patch(
        f"/api/portfolio/opportunities/{opp_id}",
        json={"status": None},
    )
    assert r_null_status.status_code == 422

    # 5) PATCH with explicit null for subsequent_extreme_price and root_cause_tag clears all derived fields
    r_clear = client.patch(
        f"/api/portfolio/opportunities/{opp_id}",
        json={
            "subsequent_extreme_price": None,
            "root_cause_tag": None,
            "idempotency_key": "opp-patch-key-1",
        },
    )
    assert r_clear.status_code == 200
    cleared = r_clear.json()
    assert cleared["subsequent_extreme_price"] is None
    assert cleared["missed_move_pct"] is None
    assert cleared["missed_r_multiple"] is None
    assert cleared["root_cause_tag"] is None
    assert cleared["lesson_note"] == "仅补充复盘笔记"
    assert cleared["status"] == "tracking"

    # Verify PATCH idempotency replay (200) and conflict (409)
    r_patch_replay = client.patch(
        f"/api/portfolio/opportunities/{opp_id}",
        json={
            "subsequent_extreme_price": None,
            "root_cause_tag": None,
            "idempotency_key": "opp-patch-key-1",
        },
    )
    assert r_patch_replay.status_code == 200
    assert r_patch_replay.json().get("idempotent_replay") is True

    r_patch_conflict = client.patch(
        f"/api/portfolio/opportunities/{opp_id}",
        json={
            "status": "reviewed",
            "idempotency_key": "opp-patch-key-1",
        },
    )
    assert r_patch_conflict.status_code == 409
    assert "idempotency_key_conflict" in r_patch_conflict.json()["detail"].lower()

    # 6) Post-commit attribution analytics failure must NOT turn committed POST/PATCH into 500
    def boom_attribution(*args, **kwargs):
        raise RuntimeError("transient analytics read failure")

    monkeypatch.setattr(pm, "compute_attribution_analytics", boom_attribution)

    r_post_resilient = client.post(
        "/api/portfolio/opportunities",
        json={
            **base_payload,
            "symbol": "SI=F",
            "anchor_price": 30.0,
            "planned_stop_price": 28.0,
            "subsequent_extreme_price": 36.0,
            "idempotency_key": "opp-create-resilient-1",
        },
    )
    assert r_post_resilient.status_code == 200
    post_body = r_post_resilient.json()
    assert post_body["opportunity_id"]
    assert post_body["attribution"] is None
    assert "transient analytics read failure" in post_body["attribution_warning"]

    r_patch_resilient = client.patch(
        f"/api/portfolio/opportunities/{post_body['opportunity_id']}",
        json={"status": "reviewed"},
    )
    assert r_patch_resilient.status_code == 200
    patch_body = r_patch_resilient.json()
    assert patch_body["status"] == "reviewed"
    assert patch_body["attribution"] is None
    assert "transient analytics read failure" in patch_body["attribution_warning"]

    # Confirm the committed opportunity actually persisted in SQLite
    all_opps = client.get("/api/portfolio/opportunities").json()["opportunities"]
    assert any(o["opportunity_id"] == post_body["opportunity_id"] and o["status"] == "reviewed" for o in all_opps)


def test_scale_in_max_leverage_and_conservative_margin_cap(client: TestClient):
    """Regression suite for Codex HIGH #6: expose max_leverage and enforce max(cost, mark_to_add) cap."""
    assert (
        client.post(
            "/api/portfolio/init",
            json={
                "equity": 100_000.0,
                "base_currency": "USD",
                "default_risk_per_trade_pct": 2.0,
                "max_total_open_risk_pct": 10.0,
            },
        ).status_code
        == 200
    )

    # Case A: Margin-enabled equity position testing max_leverage against conservative max(cost, mark_to_add)
    r_open_a = client.post(
        "/api/portfolio/positions/open",
        json={
            "symbol": "NVDA",
            "direction": "long",
            "sector_theme": "ai_compute",
            "entry_price": 100.0,
            "tactical_stop_30m": 95.0,
            "quantity": 200,
            "allow_equity_margin": True,
            "margin_rate": 0.25,
        },
    )
    assert r_open_a.status_code == 200
    pid_a = r_open_a.json()["position"]["position_id"]

    # Adding 400 @ 200 gives qty=600, avg=166.67 => cost_notional=100,000 (1.0x), mark_notional=120,000 (1.2x).
    # max_leverage=1.1 must reject (400) because conservative max(cost, mark_to_add) = 120,000 > 110,000.
    r_lev_rej = client.post(
        f"/api/portfolio/positions/{pid_a}/scale-in",
        json={
            "add_quantity": 400,
            "add_price": 200.0,
            "new_stop_loss": 180.0,
            "max_leverage": 1.1,
            "note": "over-leveraged add",
        },
    )
    assert r_lev_rej.status_code == 400
    assert "leverage_limit_exceeded" in r_lev_rej.json()["detail"]
    assert "max(cost,mark_to_add)_vs_booked_equity" in r_lev_rej.json()["detail"]

    # Raising max_leverage to 1.25 (cap 125,000 >= 120,000) allows the exact same add.
    r_lev_ok = client.post(
        f"/api/portfolio/positions/{pid_a}/scale-in",
        json={
            "add_quantity": 400,
            "add_price": 200.0,
            "new_stop_loss": 180.0,
            "max_leverage": 1.25,
            "note": "within leverage cap",
        },
    )
    assert r_lev_ok.status_code == 200
    assert r_lev_ok.json()["position"]["quantity"] == 600.0

    # Close Case A and verify Case B: non-margin equity conservative margin cap max(cost, mark_to_add) <= equity
    assert (
        client.post(
            f"/api/portfolio/positions/{pid_a}/close",
            json={"exit_price": 200.0, "exit_reason": "reset_for_case_b", "update_account_equity": False},
        ).status_code
        == 200
    )

    r_open_b = client.post(
        "/api/portfolio/positions/open",
        json={
            "symbol": "PWR",
            "direction": "long",
            "sector_theme": "power_grid",
            "entry_price": 100.0,
            "tactical_stop_30m": 97.5,
            "quantity": 400,
            "allow_equity_margin": False,
        },
    )
    assert r_open_b.status_code == 200
    pid_b = r_open_b.json()["position"]["position_id"]

    # Adding 300 @ 150 gives qty=700, cost_notional=85,000 (<= 100k), but mark_notional=105,000 (> 100k).
    # Conservative max(cost, mark_to_add) margin cap must reject with HTTP 400.
    r_cap_rej = client.post(
        f"/api/portfolio/positions/{pid_b}/scale-in",
        json={
            "add_quantity": 300,
            "add_price": 150.0,
            "new_stop_loss": 135.0,
            "note": "exceeds mark-to-add capital cap",
        },
    )
    assert r_cap_rej.status_code == 400
    assert "portfolio_capital_limit_exceeded" in r_cap_rej.json()["detail"]
    assert "max(cost,mark_to_add)_vs_booked_equity" in r_cap_rej.json()["detail"]


def test_research_archive_state_rejects_misspelled_expectation_gap_key(client: TestClient):
    """PATCH /api/research/archive/state rejects unknown expectation_gap keys without mutating state."""
    r_init = client.post(
        "/api/research/archive/init",
        json={
            "symbol": "PWR",
            "name": "Quanta Services",
            "thesis_summary": "Grid capex multi-year bottleneck",
            "layer1_primary": "技术与产业迭代",
            "layer1_secondary": "产业政策与监管",
            "direction_bias": "long",
        },
    )
    assert r_init.status_code == 200

    before = client.get("/api/research/archive", params={"symbol": "PWR"}).json()["state"]
    rev_before = before["revision"]
    eg_before = dict(before["expectation_gap"])

    # Top-level expectation_gap with typo 'crowding_assesment' -> 422, state unchanged
    r_bad_top = client.patch(
        "/api/research/archive/state",
        json={
            "symbol": "PWR",
            "expectation_gap": {
                "priced_in_consensus": "Grid growth already priced in",
                "crowding_assesment": "low",
            },
        },
    )
    assert r_bad_top.status_code in (400, 422)
    assert "crowding_assesment" in r_bad_top.json()["detail"]

    # Nested patch.expectation_gap with typo 'crowding_assesment' -> 422, state unchanged
    r_bad_patch = client.patch(
        "/api/research/archive/state",
        json={
            "symbol": "PWR",
            "patch": {
                "expectation_gap": {
                    "crowding_assesment": "high",
                }
            },
        },
    )
    assert r_bad_patch.status_code in (400, 422)
    assert "crowding_assesment" in r_bad_patch.json()["detail"]

    after = client.get("/api/research/archive", params={"symbol": "PWR"}).json()["state"]
    assert after["revision"] == rev_before
    assert after["expectation_gap"] == eg_before
