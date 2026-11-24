"""End-to-end regression tests for SQLite persistence (`data/desk.db`) & OHLCV data-quality gates."""
from __future__ import annotations

import json
import os
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
from fastapi.testclient import TestClient

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS_DIR = os.path.join(ROOT_DIR, "scripts")
for p in (ROOT_DIR, SCRIPTS_DIR):
    if p not in sys.path:
        sys.path.insert(0, p)

import chanlun  # noqa: E402
import daily_sentinel  # noqa: E402
import data_store  # noqa: E402
import market_snapshot  # noqa: E402
import portfolio_manager as pm  # noqa: E402
import research_archive as ra  # noqa: E402
import scan_opportunities as so  # noqa: E402
import trade_planner as tp  # noqa: E402
from backend.app import create_app  # noqa: E402
from backend.cache import CACHE  # noqa: E402
from backend.tasks import TaskManager  # noqa: E402


def _make_wave_df(n: int = 120, start: str = "2026-05-01", tz: str | None = None, intraday: bool = False) -> pd.DataFrame:
    idx = (
        pd.date_range(start, periods=n, freq="30min", tz=tz or "America/New_York")
        if intraday
        else pd.bdate_range(start, periods=n, tz=tz)
    )
    t = np.arange(n, dtype=float)
    base = 100.0 + 0.2 * t + 9.0 * np.sin(t / 4.0) + 3.5 * np.cos(t / 8.5)
    close = np.round(base, 4)
    open_ = np.round(np.roll(close, 1), 4)
    open_[0] = close[0] - 0.4
    high = np.round(np.maximum(open_, close) + 1.5, 4)
    low = np.round(np.minimum(open_, close) - 1.5, 4)
    vol = np.full(n, 500_000.0)
    return pd.DataFrame({"Open": open_, "High": high, "Low": low, "Close": close, "Volume": vol}, index=idx)


@pytest.fixture()
def store_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    db_path = tmp_path / "data" / "desk.db"
    portfolio_dir = tmp_path / "portfolio"
    research_dir = tmp_path / "research"
    universes_path = tmp_path / "universes.json"
    universes_path.write_text(
        json.dumps({"us_test": {"description": "Test pool", "symbols": ["NVDA", "PWR"]}}),
        encoding="utf-8",
    )
    monkeypatch.setenv("TRADING_DESK_DB_PATH", str(db_path))
    monkeypatch.setattr(pm, "DEFAULT_PORTFOLIO_DIR", str(portfolio_dir))
    monkeypatch.setattr(ra, "DEFAULT_RESEARCH_DIR", str(research_dir))
    monkeypatch.setattr(so, "DEFAULT_UNIVERSES_PATH", str(universes_path))
    CACHE.clear()
    yield {
        "db_path": str(db_path),
        "portfolio_dir": portfolio_dir,
        "research_dir": research_dir,
        "tmp_path": tmp_path,
    }
    CACHE.clear()


def test_scan_to_warroom_reuses_sqlite_and_detects_split_drift(store_env, monkeypatch: pytest.MonkeyPatch):
    full_calls = {"daily": 0, "intra": 0}
    tail_calls = {"daily": 0}
    state = {"split_factor": 1.0}

    base_daily = _make_wave_df(140, start="2026-05-04", tz="America/New_York", intraday=False)
    base_intra = _make_wave_df(120, start="2026-11-18 09:30", tz="America/New_York", intraday=True)

    class FakeTicker:
        def __init__(self, sym: str):
            self.sym = sym
            self.info = {"shortName": sym}

        def history(self, period: str = "3y", interval: str = "1d", start: str | None = None):
            f = state["split_factor"]
            if interval != "1d":
                full_calls["intra"] += 1
                return base_intra * f
            if start is not None:
                tail_calls["daily"] += 1
                sub = base_daily[base_daily.index >= pd.Timestamp(start, tz="America/New_York")]
                return sub * f
            full_calls["daily"] += 1
            return base_daily * f

    monkeypatch.setattr(market_snapshot.yf, "Ticker", FakeTicker)

    app = create_app(task_manager=TaskManager(max_tasks=20), frontend_dist=str(store_env["tmp_path"] / "no_dist"))
    with TestClient(app) as client:
        # 1) Run scan on NVDA -> populates SQLite daily bars (1 upstream daily call)
        r_scan = client.post("/api/tasks/scan", json={"symbols": ["NVDA"], "sort": "tier"})
        assert r_scan.status_code == 202
        tid = r_scan.json()["id"]
        with client.stream("GET", f"/api/tasks/{tid}/events") as sse:
            _ = "".join(sse.iter_text())
        assert full_calls["daily"] == 1

        # 2) Jump to War Room (/api/market/dual) -> daily hits SQLite (0 extra daily calls!), only 30m fetched
        r_dual = client.get("/api/market/dual", params={"symbol": "NVDA", "intraday_interval": "30m", "include_macro": False})
        assert r_dual.status_code == 200
        body = r_dual.json()
        assert body["sync_meta"]["cache_status"] == "hit"
        assert full_calls["daily"] == 1
        assert full_calls["intra"] == 1

        # 3) Force refresh with NO split -> runs incremental tail sync without full 3y download
        CACHE.clear()
        r_inc = client.get(
            "/api/market/dual",
            params={"symbol": "NVDA", "intraday_interval": "30m", "include_macro": False, "refresh": True},
        )
        assert r_inc.status_code == 200
        assert tail_calls["daily"] == 1
        assert full_calls["daily"] == 1
        assert r_inc.json()["sync_meta"]["sync_mode"] == "incremental_tail"

        # 4) Simulate 2-for-1 stock split (0.5x historical adjusted prices) -> overlap drift triggers full rebuild!
        state["split_factor"] = 0.5
        CACHE.clear()
        r_split = client.get(
            "/api/market/dual",
            params={"symbol": "NVDA", "intraday_interval": "30m", "include_macro": False, "refresh": True},
        )
        assert r_split.status_code == 200
        assert tail_calls["daily"] == 2
        assert full_calls["daily"] == 2
        assert r_split.json()["sync_meta"]["sync_mode"] == "full_rebuild"
        assert "price_drift" in (r_split.json()["sync_meta"].get("rebuild_reason") or "")


def test_false_closed_bar_prevented_and_cross_boundary_invalidates(store_env):
    store = data_store.get_store(store_env["db_path"])
    key = data_store.SeriesKey("us_stock_or_etf", "NVDA", "30m", "yfinance_intraday", "auto_adjust")

    # Bar 10:00..10:30 EDT (14:00..14:30 UTC) fetched at 14:28 UTC -> unclosed!
    raw_df = _make_wave_df(40, start="2026-10-01 09:30", tz="America/New_York", intraday=True)
    last_bar_start_utc = raw_df.index[-1].tz_convert("UTC").to_pydatetime()
    last_bar_end_utc = last_bar_start_utc + pd.Timedelta(minutes=30)
    t_mid_bar = last_bar_end_utc - pd.Timedelta(minutes=2)   # 2 mins BEFORE bar closes
    t_still_open = last_bar_end_utc - pd.Timedelta(minutes=1)
    t_after_close = last_bar_end_utc + pd.Timedelta(minutes=1)  # 1 min AFTER bar closes

    fetch_count = {"n": 0}

    def fake_fetch():
        fetch_count["n"] += 1
        return raw_df.copy()

    # Initial sync mid-bar: explicitly verify sanitize_and_grade_ohlcv marks latest_bar_closed=False
    clean_mid, qual_mid = data_store.sanitize_and_grade_ohlcv(
        raw_df,
        asset_class="us_stock_or_etf",
        interval="30m",
        timestamp_anchor="start",
        fetched_at=t_mid_bar,
        now_utc=t_mid_bar,
    )
    assert qual_mid["latest_bar_closed"] is False
    ind_mid = market_snapshot.compute_indicators(clean_mid, interval="30m", timestamp_anchor="start")
    assert ind_mid["bar_is_closed"] is False

    # Store mid-bar snapshot at t_mid_bar
    store._replace_full_series(
        key,
        clean_mid,
        qual_mid,
        timestamp_anchor="start",
        covered_days=60,
        synced_at_iso=data_store.utc_iso(t_mid_bar),
    )

    # Reading 1 minute later (still before bar close) -> cache HIT and bar_is_closed stays False
    df_hit, _, meta_hit = store.get_or_sync_ohlcv(
        key,
        period="60d",
        timestamp_anchor="start",
        fetch_full=fake_fetch,
        now_utc=t_still_open,
    )
    assert meta_hit["cache_status"] == "hit"
    assert fetch_count["n"] == 0
    assert bool(df_hit["_is_closed"].iloc[-1]) is False

    # Reading 1 minute AFTER bar_end_utc (only 3 mins after sync, well inside 5m TTL!) -> MUST invalidate & re-sync!
    _, _, meta_crossed = store.get_or_sync_ohlcv(
        key,
        period="60d",
        timestamp_anchor="start",
        fetch_full=fake_fetch,
        now_utc=t_after_close,
    )
    assert meta_crossed["cache_status"] == "miss"
    assert fetch_count["n"] == 1


def test_degraded_flat_bars_block_scan_alignment_and_live_planner():
    # Construct 90-bar series where last 55 of 60 bars have High == Low (mimicking ALI=F settlement-only bars)
    df = _make_wave_df(90, start="2026-06-01")
    for idx in range(35, 90):
        px = float(df["Close"].iloc[idx])
        df.iloc[idx, df.columns.get_loc("Open")] = px
        df.iloc[idx, df.columns.get_loc("High")] = px
        df.iloc[idx, df.columns.get_loc("Low")] = px

    clean_df, qual = data_store.sanitize_and_grade_ohlcv(
        df, asset_class="commodity", interval="1d", check_staleness=False
    )
    assert qual["quality_grade"] == "degraded"
    assert "HIGH_FLAT_BAR_RATIO" in qual["warnings"]

    cl = chanlun.analyze_chanlun(clean_df, timeframe_label="日线")
    cl["data_quality"] = qual
    sig = chanlun.evaluate_daily_signal_tier(cl)
    assert sig["tier"] == "数据质量降级"
    assert sig["signal_meta"]["confirmed"] is False

    intra_df = _make_wave_df(80, start="2026-11-10 09:30", tz="America/New_York", intraday=True)
    intra_cl = chanlun.analyze_chanlun(intra_df, timeframe_label="30分钟")
    dual = chanlun.assess_dual_timeframe_alignment(cl, intra_cl, desired_direction="long")
    assert dual["aligned"] is False
    assert dual["alignment_status"] == "data_quality_degraded"

    # Planner live health check must flag daily_data_quality_degraded
    now_ref = datetime(2026, 11, 24, 15, 0, tzinfo=timezone.utc)
    fake_snap = {
        "symbol": "ALI=F",
        "asset_class": "commodity",
        "data_quality": qual,
        "technicals": {"as_of_bar": "2026-11-24", "last_close": 2800.0},
        "intraday_technicals": {
            "as_of_bar": "2026-11-24 09:30",
            "as_of_bar_utc": "2026-11-24T14:30:00+00:00",
            "bar_is_closed": True,
            "last_close": 2800.0,
        },
        "chanlun": {"daily": cl, "intraday": intra_cl},
    }
    errs, _, _ = tp._check_live_upstream_health(
        fake_snap,
        {"sources_status": {"term_structure": {"status": "ok"}}},
        asset_class="commodity",
        now_utc=now_ref,
    )
    assert "daily_data_quality_degraded" in errs


def test_repaired_settlement_wick_does_not_trigger_false_red_exit():
    # Raw pit range is [266.0, 275.0], settlement close is 264.2 (inverted below raw_low=266.0).
    # Stop is at 265.0 -> raw traded low (266.0) NEVER touched 265.0, only settlement close=264.2 was repaired!
    idx = pd.bdate_range("2026-10-01", periods=30)
    df = pd.DataFrame(
        {
            "Open": [270.0] * 30,
            "High": [275.0] * 30,
            "Low": [266.0] * 30,
            "Close": [271.0] * 29 + [264.2],
            "Volume": [1500.0] * 30,
        },
        index=idx,
    )
    # Wait: if latest Close itself is 264.2 <= 265.0, last_price breaches stop; test historical bar idx=-2 where price recovered!
    df.iloc[-2, df.columns.get_loc("Close")] = 264.2  # off-market settlement dip on yesterday's bar
    df.iloc[-1, df.columns.get_loc("Close")] = 272.0  # today's close is safe at 272.0

    clean_df, qual = data_store.sanitize_and_grade_ohlcv(
        df, asset_class="commodity", interval="1d", check_staleness=False
    )
    assert qual["repaired_ohlc_bars_total"] == 1
    assert float(clean_df["Low"].iloc[-2]) == 264.2
    assert float(clean_df["_raw_low"].iloc[-2]) == 266.0

    tech = market_snapshot.compute_indicators(clean_df, interval="1d")
    intra_df = _make_wave_df(40, start="2026-11-10 09:30", tz="America/New_York", intraday=True) + 180.0
    intra_clean, _ = data_store.sanitize_and_grade_ohlcv(
        intra_df, asset_class="commodity", interval="30m", check_staleness=False
    )
    intra_tech = market_snapshot.compute_indicators(intra_clean, interval="30m")

    snap = {
        "symbol": "KC=F",
        "asset_class": "commodity",
        "technicals": tech,
        "intraday_technicals": intra_tech,
        "chanlun": {
            "daily": chanlun.analyze_chanlun(clean_df, "日线"),
            "intraday": chanlun.analyze_chanlun(intra_clean, "30分钟"),
        },
    }
    pos = {
        "position_id": "pos_kc",
        "symbol": "KC=F",
        "direction": "long",
        "entry_price": 270.0,
        "structural_stop_daily": 265.0,
        "tactical_stop_30m": 265.0,
        "opened_at_utc": "2026-10-15T14:00:00+00:00",
    }
    res = daily_sentinel.evaluate_holding_health(
        symbol="KC=F",
        direction="long",
        position=pos,
        snapshot=snap,
        ref_date=clean_df.index[-1].date(),
    )
    red_codes = [a.get("code") for a in res.get("red_alerts", [])]
    assert "structural_stop_daily_intraday_breached" not in red_codes
    assert "tactical_stop_30m_intraday_breached" not in red_codes


def test_cross_market_symbol_dedup_and_unreliable_momentum_label():
    # US:CU0 (US stock) and CU0 (CN copper future) must NOT deduplicate each other
    resolved = so.resolve_targets(symbols=["US:CU0", "CU0"])
    assert len(resolved) == 2

    # Unreliable momentum must NOT print area=0.0x,peak=0.0x or peak=Nonex
    lbl_unrel = so.format_momentum_label({
        "verdict": "lower_high_rebound",
        "momentum_reliable": False,
        "area_ratio_vs_prev": 0.0,
        "peak_ratio_vs_prev": 0.0,
        "curr_pct_change": 4.12,
        "prev_pct_change": 2.45,
    })
    assert "area=0.0x" not in lbl_unrel
    assert "短笔/零面积" in lbl_unrel

    lbl_no_peak = so.format_momentum_label({
        "verdict": "divergence_candidate_down",
        "momentum_reliable": True,
        "area_ratio_vs_prev": 0.42,
        "peak_ratio_vs_prev": None,
    })
    assert "Nonex" not in lbl_no_peak
    assert "area=0.42x" in lbl_no_peak

    # 1wk period compatibility in market_snapshot.period_to_start_date
    assert market_snapshot.period_to_start_date("1wk", today="2026-11-24") == "20261117"


def test_sentinel_fingerprint_invalidates_on_stop_change_and_task_cancel_works(
    store_env, monkeypatch: pytest.MonkeyPatch
):
    sent_calls = {"n": 0}

    def fake_sentinel(**kw):
        sent_calls["n"] += 1
        return {
            "checked_at_utc": "2026-11-24T12:00:00Z",
            "ref_date": "2026-11-24",
            "summary": {
                "holdings_total": 1,
                "red_exit_count": 0,
                "yellow_loosening_count": 0,
                "green_hold_count": 1,
                "red_count": 0,
                "yellow_count": 0,
                "green_count": 1,
                "watchlist_total": 0,
                "watchlist_fired_count": 0,
                "discovery_errors_count": 0,
                "state_sync_warnings_count": 0,
            },
            "discovery_errors": [],
            "state_sync_warnings": [],
            "holdings": [],
            "watchlist": [],
            "call_seq": sent_calls["n"],
        }

    monkeypatch.setattr(daily_sentinel, "run_daily_sentinel", fake_sentinel)

    app1 = create_app(task_manager=TaskManager(max_tasks=20), frontend_dist=str(store_env["tmp_path"] / "no_dist"))
    with TestClient(app1) as c1:
        c1.post("/api/portfolio/init", json={"equity": 500_000.0, "base_currency": "USD"})
        r_open = c1.post(
            "/api/portfolio/positions/open",
            json={
                "symbol": "NVDA",
                "direction": "long",
                "sector_theme": "ai_semis",
                "entry_price": 150.0,
                "tactical_stop_30m": 142.0,
                "quantity": 100,
            },
        )
        assert r_open.status_code == 200
        pos_id = r_open.json()["position"]["position_id"]

        s1 = c1.get("/api/sentinel/run")
        assert s1.status_code == 200
        assert s1.headers.get("X-Cache") == "MISS"
        assert sent_calls["n"] == 1

    # Simulate service restart (clear in-memory CACHE + new create_app): unchanged portfolio hits SQLite sentinel!
    CACHE.clear()
    app2 = create_app(task_manager=TaskManager(max_tasks=20), frontend_dist=str(store_env["tmp_path"] / "no_dist"))
    with TestClient(app2) as c2:
        s2 = c2.get("/api/sentinel/run")
        assert s2.status_code == 200
        assert s2.headers.get("X-Cache") == "HIT"
        assert sent_calls["n"] == 1

        # Now mutate trailing stop (even if CACHE were cleared on another process!) -> fingerprint changes -> MISS!
        CACHE.clear()
        pm.trail_stop(position_id=pos_id, new_stop_30m=146.0, current_price=158.0)
        s3 = c2.get("/api/sentinel/run")
        assert s3.status_code == 200
        assert s3.headers.get("X-Cache") == "MISS"
        assert sent_calls["n"] == 2

        # Verify task cancellation transitions cleanly to 'cancelled' and cannot be overwritten
        gate = threading.Event()
        entered = threading.Event()

        def slow_snap(sym, **kw):
            entered.set()
            gate.wait(timeout=2.0)
            return {"symbol": sym, "technicals": {"last_close": 100.0}, "chanlun": {"daily": {}, "signal_summary": {}}}

        monkeypatch.setattr(market_snapshot, "get_snapshot", slow_snap)
        r_task = c2.post("/api/tasks/scan", json={"symbols": ["NVDA", "PWR"]})
        tid = r_task.json()["id"]
        entered.wait(timeout=2.0)
        r_cancel = c2.post(f"/api/tasks/{tid}/cancel")
        assert r_cancel.status_code == 200
        assert r_cancel.json()["status"] == "cancelled"
        gate.set()
        time.sleep(0.05)
        assert c2.get(f"/api/tasks/{tid}").json()["status"] == "cancelled"

        # Warehouse & scan history endpoints work
        wh = c2.get("/api/data/warehouse")
        assert wh.status_code == 200
        assert wh.json()["sentinel_runs_count"] >= 2


def test_codex_r2_sqlite_hit_preserves_quarantined_corrupt_bar_degraded_grade(store_env):
    """Codex R2 P1-1: SQLite cache hit & tail sync MUST retain quarantined corrupt-bar degraded status."""
    store = data_store.get_store(store_env["db_path"])
    key = data_store.SeriesKey("commodity", "KC=F", "1d", "yfinance", "raw_quote")

    raw_df = _make_wave_df(80, start="2026-08-03", tz="America/New_York", intraday=False)
    # Inject 3 corrupt negative-low rows inside the recent 60-bar window
    for bad_idx in (-5, -12, -20):
        raw_df.iloc[bad_idx, raw_df.columns.get_loc("Low")] = -1.0

    ref_now = raw_df.index[-1].tz_convert("UTC").to_pydatetime() + pd.Timedelta(hours=22)

    df_miss, qual_miss, meta_miss = store.get_or_sync_ohlcv(
        key,
        period="3y",
        timestamp_anchor="start",
        refresh=True,
        fetch_full=lambda: raw_df,
        now_utc=ref_now,
    )
    assert meta_miss["cache_status"] == "miss"
    assert qual_miss["quality_grade"] == "degraded"
    assert "CORRUPT_BARS_DROPPED" in qual_miss["warnings"]
    assert qual_miss["dropped_corrupt_bars"] == 3

    # Second call hits SQLite -> MUST still grade as 'degraded' with dropped_corrupt_bars == 3!
    _, qual_hit, meta_hit = store.get_or_sync_ohlcv(
        key,
        period="3y",
        timestamp_anchor="start",
        refresh=False,
        fetch_full=lambda: pd.DataFrame(),
        now_utc=ref_now,
    )
    assert meta_hit["cache_status"] == "hit"
    assert qual_hit["quality_grade"] == "degraded"
    assert "CORRUPT_BARS_DROPPED" in qual_hit["warnings"]
    assert qual_hit["dropped_corrupt_bars"] == 3


def test_codex_r2_short_period_refresh_never_truncates_stored_3y_history(store_env, monkeypatch: pytest.MonkeyPatch):
    """Codex R2 P1-2: Refreshing period='1mo' on CN futures must NEVER wipe 3y bars stored in SQLite."""
    full_3y = _make_wave_df(750, start="2023-12-01", intraday=False)

    def fake_cn_daily(symbol: str, period: str = "3y"):
        days = data_store.period_to_days(period)
        cutoff = full_3y.index[-1] - pd.Timedelta(days=days)
        sub = full_3y[full_3y.index >= cutoff].copy()
        sub.attrs["provider_source"] = "akshare_sina_main"
        return sub

    monkeypatch.setattr(market_snapshot, "fetch_cn_future_daily", fake_cn_daily)

    s_3y_first = market_snapshot.get_snapshot("CU0", period="3y", include_macro=False, include_fundamentals=False)
    bars_3y = s_3y_first["technicals"]["bars"]
    assert bars_3y >= 700

    # Force refresh with short period='1mo' -> returns ~22 bars for the 1mo view without wiping 3y SQLite history
    s_1mo = market_snapshot.get_snapshot(
        "CU0", period="1mo", include_macro=False, include_fundamentals=False, refresh=True
    )
    assert s_1mo["technicals"]["bars"] <= 35

    # Next 3y request MUST still return the full >=700 bars from SQLite!
    s_3y_after = market_snapshot.get_snapshot("CU0", period="3y", include_macro=False, include_fundamentals=False)
    assert s_3y_after["technicals"]["bars"] == bars_3y


def test_codex_r2_sentinel_backfill_race_cannot_poison_memory_cache(store_env, monkeypatch: pytest.MonkeyPatch):
    """Codex R2 P1-3: Mid-backfill stop mutation must never poison in-memory sentinel cache with stale report."""
    calls = {"n": 0}

    def fake_sentinel(**kw):
        calls["n"] += 1
        return {
            "checked_at_utc": "2026-11-24T12:00:00Z",
            "ref_date": "2026-11-24",
            "summary": {
                "holdings_total": 1,
                "red_exit_count": 0,
                "yellow_loosening_count": 0,
                "green_hold_count": 1,
                "red_count": 0,
                "yellow_count": 0,
                "green_count": 1,
                "watchlist_total": 0,
                "watchlist_fired_count": 0,
                "discovery_errors_count": 0,
                "state_sync_warnings_count": 0,
            },
            "discovery_errors": [],
            "state_sync_warnings": [],
            "holdings": [],
            "watchlist": [],
            "seq": calls["n"],
        }

    monkeypatch.setattr(daily_sentinel, "run_daily_sentinel", fake_sentinel)
    app = create_app(task_manager=TaskManager(max_tasks=20), frontend_dist=str(store_env["tmp_path"] / "no_dist"))

    with TestClient(app) as client:
        client.post("/api/portfolio/init", json={"equity": 500_000.0, "base_currency": "USD"})
        r_open = client.post(
            "/api/portfolio/positions/open",
            json={
                "symbol": "NVDA",
                "direction": "long",
                "sector_theme": "ai_semis",
                "entry_price": 150.0,
                "tactical_stop_30m": 140.0,
                "quantity": 100,
            },
        )
        pos_id = r_open.json()["position"]["position_id"]

        # Seed initial SQLite sentinel run
        assert client.get("/api/sentinel/run").json()["seq"] == 1
        CACHE.clear()

        # Intercept get_fresh_sentinel_run to simulate a concurrent trail_stop happening mid-read!
        store = data_store.get_store(store_env["db_path"])
        orig_get_fresh = store.get_fresh_sentinel_run
        mutated_once = {"done": False}

        def racing_get_fresh(**kw):
            res = orig_get_fresh(**kw)
            if not mutated_once["done"]:
                mutated_once["done"] = True
                client.post(
                    f"/api/portfolio/positions/{pos_id}/trail-stop",
                    json={"new_stop_30m": 145.0, "current_price": 155.0},
                )
            return res

        monkeypatch.setattr(store, "get_fresh_sentinel_run", racing_get_fresh)

        # Request during race must NOT return the stale seq=1 report, and next request must also see seq=2!
        r_race = client.get("/api/sentinel/run")
        assert r_race.json()["seq"] == 2
        r_after = client.get("/api/sentinel/run")
        assert r_after.json()["seq"] == 2


def test_consolidation_universe_futures_specs_and_refresh_passthrough(store_env, monkeypatch: pytest.MonkeyPatch):
    """Cross-PR alignment: all CN futures in universes.json have known specs & research_data forwards refresh."""
    import research_data

    for sym in ("PB0", "C0", "FU0", "PG0", "SF0", "SM0"):
        spec = tp.get_contract_spec(sym, "cn_future")
        assert spec["known_spec"] is True, f"Missing CN future spec for {sym}"
        assert spec["multiplier"] > 0

    seen_kwargs: dict = {}

    def fake_get_snapshot(raw_symbol: str, **kw):
        seen_kwargs.update(kw)
        return {
            "symbol": "NVDA",
            "asset_class": "us_stock_or_etf",
            "technicals": {"last_close": np.float64(180.5), "nan_probe": float("nan")},
        }

    monkeypatch.setattr(research_data, "get_snapshot", fake_get_snapshot)
    monkeypatch.setattr(research_data, "fetch_sec_edgar", lambda *a, **kw: {"status": "ok"})
    monkeypatch.setattr(research_data, "fetch_yfinance_consensus", lambda *a, **kw: {"status": "ok"})

    bundle = research_data.collect_research_data(
        "NVDA", include_macro=False, refresh=True, db_path=store_env["db_path"]
    )
    assert seen_kwargs.get("refresh") is True
    assert seen_kwargs.get("db_path") == store_env["db_path"]
    assert bundle["symbol"] == "NVDA"

    cached = data_store.get_store(store_env["db_path"]).get_fresh_research_snapshot(
        asset_class="us_stock_or_etf", symbol="NVDA", include_macro=False, max_age_seconds=300
    )
    assert cached is not None
    assert cached["snapshot"]["technicals"]["nan_probe"] is None


