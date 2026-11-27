"""Comprehensive unit tests for Track B-1 & Track C-1 asymmetric alpha portfolio engine.

Covers:
  1. Schema drift fixes + lifecycle & campaign_1r_amount (fixing 0.4R pilot R inflation).
  2. Zero-incremental-principal-risk breakeven pyramiding & profit cushion giveback guard.
  3. Controlled main-wave stop regime handover (tactical_30m <-> daily_structure) & breakeven floor.
  4. Structured post-trade attribution across close_position, trim_position, and rotate_leader.
  5. Shadow opportunity ledger (SQLite + opportunities.jsonl mirror) & skewness/attribution analytics.
  6. CLI subcommands and full Draft 2020-12 validation against portfolio/schema.json.
"""
from __future__ import annotations

import io
import json
import os
import sqlite3
import sys
import tempfile
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from typing import Any

import pytest

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS_DIR = os.path.join(ROOT_DIR, "scripts")
if SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, SCRIPTS_DIR)

import portfolio_manager as pm

# ---------------------------------------------------------------------------
# Strict Draft 2020-12 JSON Schema Validator (zero external dependency + optional jsonschema)
# ---------------------------------------------------------------------------

def _check_json_type(instance: Any, expected_type: str) -> bool:
    if expected_type == "null":
        return instance is None
    if expected_type == "boolean":
        return isinstance(instance, bool)
    if expected_type == "integer":
        return isinstance(instance, int) and not isinstance(instance, bool)
    if expected_type == "number":
        return isinstance(instance, (int, float)) and not isinstance(instance, bool)
    if expected_type == "string":
        return isinstance(instance, str)
    if expected_type == "array":
        return isinstance(instance, list)
    if expected_type == "object":
        return isinstance(instance, dict)
    raise AssertionError(f"Unsupported JSON schema type: {expected_type}")


def validate_against_schema(instance: Any, schema: dict[str, Any], root_schema: dict[str, Any], path: str = "$") -> None:
    """Validate an instance against a Draft 2020-12 JSON Schema subset used by portfolio/schema.json."""
    if "$ref" in schema:
        ref = schema["$ref"]
        assert ref.startswith("#/$defs/"), f"Unsupported $ref {ref} at {path}"
        def_name = ref.split("#/$defs/", 1)[1]
        target = root_schema["$defs"][def_name]
        validate_against_schema(instance, target, root_schema, path)
        return

    if "type" in schema:
        t = schema["type"]
        allowed_types = [t] if isinstance(t, str) else list(t)
        assert any(_check_json_type(instance, et) for et in allowed_types), (
            f"Type mismatch at {path}: got {type(instance).__name__} ({instance!r}), expected {allowed_types}"
        )

    if "const" in schema:
        assert instance == schema["const"], f"Const mismatch at {path}: {instance!r} != {schema['const']!r}"

    if "enum" in schema:
        assert instance in schema["enum"], f"Enum mismatch at {path}: {instance!r} not in {schema['enum']!r}"

    if isinstance(instance, str) and "minLength" in schema:
        assert len(instance) >= schema["minLength"], f"String too short at {path}: {instance!r}"

    if isinstance(instance, (int, float)) and not isinstance(instance, bool):
        if "minimum" in schema:
            assert instance >= schema["minimum"], f"Below minimum at {path}: {instance} < {schema['minimum']}"
        if "maximum" in schema:
            assert instance <= schema["maximum"], f"Above maximum at {path}: {instance} > {schema['maximum']}"
        if "exclusiveMinimum" in schema:
            assert instance > schema["exclusiveMinimum"], (
                f"Not > exclusiveMinimum at {path}: {instance} <= {schema['exclusiveMinimum']}"
            )
        if "exclusiveMaximum" in schema:
            assert instance < schema["exclusiveMaximum"], (
                f"Not < exclusiveMaximum at {path}: {instance} >= {schema['exclusiveMaximum']}"
            )

    if isinstance(instance, list):
        if "minItems" in schema:
            assert len(instance) >= schema["minItems"], f"Too few items at {path}: {len(instance)}"
        if "items" in schema:
            for idx, elem in enumerate(instance):
                validate_against_schema(elem, schema["items"], root_schema, f"{path}[{idx}]")

    if isinstance(instance, dict):
        for req_key in schema.get("required", []):
            assert req_key in instance, f"Missing required property {req_key!r} at {path}"
        props = schema.get("properties", {})
        if schema.get("additionalProperties") is False:
            extra = set(instance.keys()) - set(props.keys())
            assert not extra, f"Disallowed additionalProperties {sorted(extra)} at {path}"
        for k, v in instance.items():
            if k in props:
                validate_against_schema(v, props[k], root_schema, f"{path}.{k}")

        if "if" in schema:
            cond_matched = True
            try:
                validate_against_schema(instance, schema["if"], root_schema, f"{path}(if)")
            except AssertionError:
                cond_matched = False
            if cond_matched and "then" in schema:
                validate_against_schema(instance, schema["then"], root_schema, f"{path}(then)")
            elif not cond_matched and "else" in schema:
                validate_against_schema(instance, schema["else"], root_schema, f"{path}(else)")


# ---------------------------------------------------------------------------
# 1. Campaign 1R, Lifecycle Stages, Legacy Fallback & Effective Stop
# ---------------------------------------------------------------------------

def test_campaign_1r_fixes_pilot_r_inflation_and_preserves_legacy_r_multiple():
    with tempfile.TemporaryDirectory() as tmpdir:
        pm.init_portfolio(portfolio_dir=tmpdir, equity=1_000_000.0, base_currency="USD", default_risk_per_trade_pct=1.0)

        # Reject non-positive campaign_1r_amount
        with pytest.raises(pm.PortfolioError):
            pm.open_position(
                symbol="NVDA",
                direction="long",
                sector_theme="ai_compute",
                entry_price=100.0,
                tactical_stop_30m=90.0,
                quantity=400,
                campaign_1r_amount=0.0,
                portfolio_dir=tmpdir,
            )

        # Full campaign 1R = $10,000 (1% of $1M). T1 pilot = 400 shares @ $100, stop $90 -> initial_risk = $4,000 (0.4R).
        opened = pm.open_position(
            symbol="NVDA",
            direction="long",
            sector_theme="ai_compute",
            entry_price=100.0,
            tactical_stop_30m=90.0,
            quantity=400,
            structural_stop_daily=85.0,
            campaign_1r_amount=10_000.0,
            exit_red_lines=["30m break 90"],
            portfolio_dir=tmpdir,
        )
        pos = opened["position"]
        pid = pos["position_id"]
        assert pos["initial_risk_amount"] == 4_000.0
        assert pos["campaign_1r_amount"] == 10_000.0
        assert pos["lifecycle_stage"] == "pilot"
        assert pos["active_stop_regime"] == "tactical_30m"
        assert pm._effective_position_stop(pos) == 90.0

        # Trail stop to breakeven ($100) -> lifecycle_stage advances automatically from pilot -> breakeven_locked
        trailed = pm.trail_stop(pid, new_stop_30m=100.0, current_price=112.0, portfolio_dir=tmpdir)
        assert trailed["position"]["current_open_risk_amount"] == 0.0
        assert trailed["position"]["lifecycle_stage"] == "breakeven_locked"

        # Scale in T2 (+400 shares @ $115, stop -> $108 -> avg=$107.5, breakeven locked) -> stage becomes pyramided
        scaled = pm.scale_in_position(
            position_id=pid,
            add_quantity=400,
            add_price=115.0,
            new_stop_loss=108.0,
            require_breakeven_lock=True,
            portfolio_dir=tmpdir,
        )
        assert scaled["position"]["lifecycle_stage"] == "pyramided"
        assert scaled["position"]["avg_entry_price"] == 107.5

        # Exit all 800 shares @ $157.5 -> total PnL = (157.5 - 107.5) * 800 = $40,000.
        # Legacy r_multiple = 40,000 / 4,000 = 10.0R (preserved!).
        # True campaign_r_multiple = 40,000 / 10,000 = 4.0R!
        closed = pm.close_position(
            position_id=pid,
            exit_price=157.5,
            exit_reason="主升浪目标达成",
            exit_category="target",
            attribution_tag="clean_pyramid_win",
            lesson_note="0.4R试仓+保本推仓吃满4R",
            portfolio_dir=tmpdir,
        )
        assert closed["realized_pnl"] == 40_000.0
        assert closed["r_multiple"] == 10.0
        assert closed["campaign_r_multiple"] == 4.0
        assert closed["position"]["r_multiple"] == 10.0
        assert closed["position"]["campaign_r_multiple"] == 4.0
        assert closed["position"]["attribution_tag"] == "clean_pyramid_win"
        assert closed["position"]["lesson_note"] == "0.4R试仓+保本推仓吃满4R"

        # Default campaign_1r_amount (None) defaults to initial_risk_amount
        opened_default = pm.open_position(
            symbol="PWR",
            direction="long",
            sector_theme="power_grid",
            entry_price=200.0,
            tactical_stop_30m=190.0,
            quantity=500,  # initial_risk = $5,000
            portfolio_dir=tmpdir,
        )
        assert opened_default["position"]["campaign_1r_amount"] == 5_000.0


def test_legacy_position_normalization_backfill():
    with tempfile.TemporaryDirectory() as tmpdir:
        pm.init_portfolio(portfolio_dir=tmpdir, equity=500_000.0)
        opened = pm.open_position(
            symbol="NVDA",
            direction="long",
            sector_theme="ai_compute",
            entry_price=100.0,
            tactical_stop_30m=95.0,
            quantity=100,
            portfolio_dir=tmpdir,
        )
        pid = opened["position"]["position_id"]

        # Strip new fields directly in SQLite to simulate a pre-migration legacy row
        db_file = os.path.join(tmpdir, "portfolio.db")
        conn = sqlite3.connect(db_file)
        row = conn.execute("SELECT payload_json FROM positions WHERE position_id = ?", (pid,)).fetchone()
        legacy_pos = json.loads(row[0])
        for k in ("campaign_1r_amount", "lifecycle_stage", "active_stop_regime", "campaign_r_multiple"):
            legacy_pos.pop(k, None)
        conn.execute(
            "UPDATE positions SET payload_json = ? WHERE position_id = ?",
            (json.dumps(legacy_pos), pid),
        )
        conn.commit()
        conn.close()

        loaded = pm.load_positions(portfolio_dir=tmpdir, status="open")
        assert len(loaded) == 1
        assert loaded[0]["campaign_1r_amount"] == 500.0
        assert loaded[0]["lifecycle_stage"] == "pilot"
        assert loaded[0]["active_stop_regime"] == "tactical_30m"


# ---------------------------------------------------------------------------
# 2. Zero-Principal-Risk Breakeven Pyramiding & Profit Cushion Giveback Guard
# ---------------------------------------------------------------------------

def test_breakeven_pyramiding_gate_profit_cushion_guard_and_margin_cap():
    with tempfile.TemporaryDirectory() as tmpdir:
        pm.init_portfolio(
            portfolio_dir=tmpdir,
            equity=100_000.0,
            base_currency="USD",
            default_risk_per_trade_pct=1.0,   # $1,000 single-sector principal cap
            max_total_open_risk_pct=3.0,      # $3,000 total principal cap
            max_macro_factor_risk_pct=1.5,    # $1,500 macro factor principal cap
        )

        # Another position occupies $1,000 of "usd_liquidity" macro factor
        pm.open_position(
            symbol="GC=F",
            direction="long",
            sector_theme="precious_metals",
            entry_price=2500.0,
            tactical_stop_30m=2490.0,
            quantity=1,  # $1,000 risk
            macro_factors=["usd_liquidity"],
            portfolio_dir=tmpdir,
        )

        # NVDA pilot: 100 shares @ $100, stop $95 -> $500 risk (macro factor total = $1,500 == cap!)
        nvda = pm.open_position(
            symbol="NVDA",
            direction="long",
            sector_theme="ai_compute",
            entry_price=100.0,
            tactical_stop_30m=95.0,
            quantity=100,
            macro_factors=["usd_liquidity"],
            portfolio_dir=tmpdir,
        )
        pid = nvda["position"]["position_id"]

        # 1) Invalid max_profit_cushion_ratio rejected
        with pytest.raises(pm.PortfolioError, match="invalid_max_profit_cushion_ratio"):
            pm.scale_in_position(
                pid, add_quantity=50, add_price=130.0, new_stop_loss=115.0,
                max_profit_cushion_ratio=1.2, portfolio_dir=tmpdir,
            )

        # 2) require_breakeven_lock=True rejects stop below new_avg (new_avg = (100*100 + 130*100)/200 = 115.0)
        with pytest.raises(pm.PortfolioError, match=r"breakeven_lock_required.*115\.0"):
            pm.scale_in_position(
                pid,
                add_quantity=100,
                add_price=130.0,
                new_stop_loss=112.0,  # < 115.0!
                require_breakeven_lock=True,
                portfolio_dir=tmpdir,
            )

        # 3) Profit Cushion Giveback Guard:
        #    At add_price=130, prior_unrealized_profit = (130 - 100) * 100 = $3,000.
        #    allowed_giveback_ceiling = max($1,000, $3,000 * 0.65) = $1,950.
        #    Suppose we add 300 shares @ $130 with new_stop_loss = $122.5 -> new_avg = (10000 + 39000)/400 = $122.5 (breakeven!).
        #    added_tranche_risk = (130 - 122.5) * 300 = $2,250 > $1,950 ceiling -> REJECTED!
        with pytest.raises(pm.PortfolioError, match="profit_cushion_giveback_exceeded"):
            pm.scale_in_position(
                pid,
                add_quantity=300,
                add_price=130.0,
                new_stop_loss=122.5,
                require_breakeven_lock=True,
                portfolio_dir=tmpdir,
            )

        # 4) Valid Druckenmiller Zero-Principal-Risk Pyramid:
        #    Add 100 shares @ $130 with new_stop_loss = $115.0 -> new_avg = $115.0 -> new_open_risk = $0.0!
        #    added_tranche_risk = (130 - 115) * 100 = $1,500 (> $1,000 sector cap, and macro factor was already at $1,500 cap!),
        #    yet <= $1,950 profit cushion ceiling and principal open risk drops from $500 -> $0!
        ok_pyr = pm.scale_in_position(
            pid,
            add_quantity=100,
            add_price=130.0,
            new_stop_loss=115.0,
            require_breakeven_lock=True,
            note="保本锁盈零本金风险推仓",
            portfolio_dir=tmpdir,
        )
        assert ok_pyr["status"] == "added"
        assert ok_pyr["breakeven_locked"] is True
        assert ok_pyr["new_risk_amount"] == 0.0
        assert ok_pyr["added_tranche_risk_amount"] == 1_500.0
        assert ok_pyr["position"]["lifecycle_stage"] == "pyramided"

        # 5) Margin ceiling STILL strictly enforced even when breakeven-locked and within profit cushion!
        #    Current NVDA = 200 shares @ $115 ($23k), GC=F margin = 2500*100*0.08 = $20k ($43k used of $100k).
        #    At $200, prior_unrealized_profit = $17,000 -> cushion ceiling = $11,050.
        #    Adding 400 shares @ $200 with stop $195 -> added_tranche_risk = $2,000 <= $11,050,
        #    BUT notional = 600 * $171.666667 = $103k + $20k GC=F = $123k > $100k equity!
        with pytest.raises(ValueError, match="portfolio_capital_limit_exceeded"):
            pm.scale_in_position(
                pid,
                add_quantity=400,
                add_price=200.0,
                new_stop_loss=195.0,
                require_breakeven_lock=True,
                portfolio_dir=tmpdir,
            )


def test_short_breakeven_lock_gate():
    with tempfile.TemporaryDirectory() as tmpdir:
        pm.init_portfolio(portfolio_dir=tmpdir, equity=1_000_000.0, base_currency="CNY")
        short_cu = pm.open_position(
            symbol="CU0",
            direction="short",
            sector_theme="base_metals",
            entry_price=80_000.0,
            tactical_stop_30m=81_000.0,
            quantity=2,  # risk = 1,000 * 5 * 2 = 10,000 CNY == 1% cap
            portfolio_dir=tmpdir,
        )
        pid = short_cu["position"]["position_id"]

        # Short add 2 lots @ 76,000 -> new_avg = 78,000. Passing stop 78,500 (> 78,000) violates breakeven lock!
        with pytest.raises(pm.PortfolioError, match=r"breakeven_lock_required.*78000\.0"):
            pm.scale_in_position(
                pid,
                add_quantity=2,
                add_price=76_000.0,
                new_stop_loss=78_500.0,
                require_breakeven_lock=True,
                portfolio_dir=tmpdir,
            )

        # Passing stop 78,000.0 locks breakeven cleanly (open risk = 0)
        ok_short = pm.scale_in_position(
            pid,
            add_quantity=2,
            add_price=76_000.0,
            new_stop_loss=78_000.0,
            require_breakeven_lock=True,
            portfolio_dir=tmpdir,
        )
        assert ok_short["new_risk_amount"] == 0.0
        assert ok_short["breakeven_locked"] is True


# ---------------------------------------------------------------------------
# 3. Controlled Main-Wave Stop Regime Handover (set_stop_regime)
# ---------------------------------------------------------------------------

def test_set_stop_regime_handover_breakeven_floor_and_return_to_tactical():
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "portfolio.db"
        pm.init_portfolio(portfolio_dir=tmpdir, equity=500_000.0, base_currency="USD")

        opened = pm.open_position(
            symbol="NVDA",
            direction="long",
            sector_theme="ai_compute",
            entry_price=100.0,
            tactical_stop_30m=95.0,
            quantity=200,
            portfolio_dir=tmpdir,
        )
        pid = opened["position"]["position_id"]

        # 1) Switching to daily_structure without any daily structural stop -> rejected
        with pytest.raises(pm.PortfolioError, match="missing_daily_structural_stop"):
            pm.set_stop_regime(pid, "daily_structure", db_path=db_path)

        # 2) Switching to daily_structure with daily stop below avg_entry_price (98 < 100) -> HARD REJECT!
        with pytest.raises(pm.PortfolioError, match="daily_stop_below_breakeven_floor"):
            pm.set_stop_regime(pid, "daily_structure", new_daily_stop=98.0, current_price=125.0, db_path=db_path)

        # 3) Trail 30m stop to 118 first, then hand over to daily_structure at daily ZD=108 (>= 100 breakeven!)
        pm.trail_stop(pid, new_stop_30m=118.0, current_price=125.0, portfolio_dir=tmpdir)
        handover = pm.set_stop_regime(
            position_id=pid,
            regime="daily_structure",
            new_daily_stop=108.0,
            current_price=125.0,
            reason="脱离成本区切入日线中枢ZD主升持有",
            idempotency_key="regime-nvda-main-1",
            db_path=db_path,
        )
        assert handover["status"] == "regime_updated"
        assert handover["old_stop_regime"] == "tactical_30m"
        assert handover["active_stop_regime"] == "daily_structure"
        assert handover["lifecycle_stage"] == "main_wave"
        assert handover["effective_stop"] == 108.0
        assert handover["current_open_risk_amount"] == 0.0
        # Locked profit uses effective daily stop 108: (108 - 100) * 200 = $1,600
        assert handover["locked_profit_amount"] == 1_600.0
        assert handover["summary"]["total_locked_profit_amount"] == 1_600.0
        assert handover["position"]["structural_stop_updated_at_utc"] is not None
        assert handover["position"]["regime_updated_at_utc"] is not None

        # Idempotent replay returns identical result
        replay = pm.set_stop_regime(
            position_id=pid,
            regime="daily_structure",
            new_daily_stop=108.0,
            current_price=125.0,
            reason="脱离成本区切入日线中枢ZD主升持有",
            idempotency_key="regime-nvda-main-1",
            db_path=db_path,
        )
        assert replay["idempotent_replay"] is True
        assert replay["effective_stop"] == 108.0

        # Widening daily stop (105 < 108) rejected
        with pytest.raises(pm.PortfolioError, match="stop_widening_forbidden"):
            pm.set_stop_regime(pid, "daily_structure", new_daily_stop=105.0, current_price=125.0, db_path=db_path)

        # Breached current_price (108 >= 107) rejected
        with pytest.raises(pm.PortfolioError, match="stop_breached_by_current_price"):
            pm.set_stop_regime(pid, "daily_structure", new_daily_stop=108.0, current_price=107.0, db_path=db_path)

        # 4) Switching back to tactical_30m:
        #    Explicit new_stop_30m looser than structural_stop_daily (106 < 108) MUST be rejected!
        with pytest.raises(pm.PortfolioError, match="stop_widening_forbidden"):
            pm.set_stop_regime(
                pid, "tactical_30m", new_stop_30m=106.0, current_price=130.0, db_path=db_path
            )

        #    Explicit new_stop_30m=114 (>= daily 108, even though < old 30m 118 after a normal 30m pullback!) succeeds!
        back_30m = pm.set_stop_regime(
            pid,
            "tactical_30m",
            new_stop_30m=114.0,
            current_price=130.0,
            reason="日线背驰迹象切回30m紧凑防守",
            db_path=db_path,
        )
        assert back_30m["active_stop_regime"] == "tactical_30m"
        assert back_30m["lifecycle_stage"] == "breakeven_locked"
        assert back_30m["effective_stop"] == 114.0
        assert back_30m["locked_profit_amount"] == 2_800.0  # (114 - 100) * 200

        #    Also test snap behavior when new_stop_30m is None and old_30m < structural_stop_daily:
        pm.set_stop_regime(pid, "daily_structure", new_daily_stop=120.0, current_price=135.0, db_path=db_path)
        snapped = pm.set_stop_regime(pid, "tactical_30m", current_price=135.0, db_path=db_path)
        assert snapped["position"]["tactical_stop_30m"] == 120.0
        assert snapped["effective_stop"] == 120.0

        journal = pm.load_journal(portfolio_dir=tmpdir)
        regime_entries = [j for j in journal if j["action"] == "set_stop_regime"]
        assert len(regime_entries) == 4


# ---------------------------------------------------------------------------
# 4. Structured Post-Trade Attribution, Shadow Opportunities & Skewness Analytics
# ---------------------------------------------------------------------------

def test_attribution_shadow_opportunities_skewness_and_schema_validation():
    schema_path = os.path.join(ROOT_DIR, "portfolio", "schema.json")
    with open(schema_path, "r", encoding="utf-8") as f:
        schema = json.load(f)

    with tempfile.TemporaryDirectory() as tmpdir:
        uninit_db = Path(tmpdir) / "uninitialized" / "portfolio.db"
        # 1) Uninitialized DB analytics & opportunities work cleanly without creating files
        uninit_stats = pm.compute_attribution_analytics(db_path=uninit_db)
        assert uninit_stats["initialized"] is False
        assert uninit_stats["closed_count"] == 0
        assert uninit_stats["payoff_ratio_r"] is None
        assert len(uninit_stats["taxonomy"]) == len(pm.ATTRIBUTION_TAGS)
        assert pm.load_opportunities(db_path=uninit_db) == []
        assert not uninit_db.exists()

        # Initialize real test ledger
        pdir = os.path.join(tmpdir, "live_sim")
        db_path = Path(pdir) / "portfolio.db"
        pm.init_portfolio(portfolio_dir=pdir, equity=1_000_000.0, base_currency="USD", default_risk_per_trade_pct=1.0)

        # Trade 1 (Big Multi-R Winner via Rotate): PWR pilot 0.5R ($5,000), campaign_1r=$10,000 -> +$35,000 (+3.5R)
        t1 = pm.open_position(
            symbol="PWR",
            direction="long",
            sector_theme="power_grid",
            entry_price=200.0,
            tactical_stop_30m=190.0,
            quantity=500,
            campaign_1r_amount=10_000.0,
            portfolio_dir=pdir,
        )
        pid1 = t1["position"]["position_id"]

        # Unknown attribution tag rejected on trim/close/rotate
        with pytest.raises(pm.PortfolioError, match="invalid_attribution_tag"):
            pm.trim_position(pid1, trim_price=230.0, trim_quantity=100, attribution_tag="bogus_tag", portfolio_dir=pdir)

        # Trim 100 shares @ 250 (+$5,000), then rotate remaining 400 shares @ 275 (+$30,000 -> total +$35,000 = +3.5R)
        trimmed = pm.trim_position(
            pid1,
            trim_price=250.0,
            trim_quantity=100,
            reason="阶段减仓锁盈",
            attribution_tag="disciplined_trailing_exit",
            lesson_note="30m盘整减仓20%",
            portfolio_dir=pdir,
        )
        assert len(trimmed["position"]["trims"]) == 1
        assert trimmed["position"]["trims"][0]["attribution_tag"] == "disciplined_trailing_exit"

        rot = pm.rotate_leader(
            old_position_id=pid1,
            exit_price=275.0,
            exit_reason="切换至GEV主升龙头",
            exit_category="leader_rotation",
            attribution_tag="full_main_wave_ride",
            lesson_note="吃满电网第一波主升后切换",
            new_position_kwargs={
                "symbol": "GEV",
                "direction": "long",
                "entry_price": 300.0,
                "tactical_stop_30m": 280.0,
                "quantity": 250,  # $5,000 risk
                "campaign_1r_amount": 10_000.0,
            },
            portfolio_dir=pdir,
        )
        assert rot["closed_position"]["realized_pnl"] == 35_000.0
        assert rot["closed_position"]["r_multiple"] == 7.0
        assert rot["closed_position"]["campaign_r_multiple"] == 3.5
        assert rot["closed_position"]["attribution_tag"] == "full_main_wave_ride"

        # Trade 2 (Small Winner): close GEV @ 320 -> +$5,000 (+0.5R on $10,000 campaign_1r)
        pid2 = rot["opened_position"]["position_id"]
        pm.close_position(
            pid2,
            exit_price=320.0,
            exit_reason="提前止盈卖飞",
            attribution_tag="premature_profit_taking",
            lesson_note="没等日线背驰过早下车",
            portfolio_dir=pdir,
        )

        # Trade 3 (Normal Pilot Friction Loss): NVDA 0.4R pilot ($4,000) on $10,000 campaign_1r -> -$4,000 (-0.4R)
        t3 = pm.open_position(
            symbol="NVDA",
            direction="long",
            sector_theme="ai_compute",
            entry_price=100.0,
            tactical_stop_30m=90.0,
            quantity=400,
            campaign_1r_amount=10_000.0,
            portfolio_dir=pdir,
        )["position"]["position_id"]
        pm.close_position(
            t3,
            exit_price=90.0,
            exit_reason="30m试仓止损",
            exit_category="stop_loss",
            attribution_tag="normal_pilot_cost",
            lesson_note="正常试仓摩擦成本",
            portfolio_dir=pdir,
        )

        # Trade 4 (Preventable Mistake Loss): CL=F short -> -$8,000 (-0.8R on $10,000 campaign_1r)
        t4 = pm.open_position(
            symbol="CL=F",
            direction="short",
            sector_theme="energy",
            entry_price=75.0,
            tactical_stop_30m=79.0,
            quantity=2,  # risk = 4 * 1000 * 2 = $8,000
            campaign_1r_amount=10_000.0,
            portfolio_dir=pdir,
        )["position"]["position_id"]
        pm.close_position(
            t4,
            exit_price=79.0,
            exit_reason="远离中枢追空被反抽止损",
            exit_category="stop_loss",
            attribution_tag="l4_chased_extended",
            lesson_note="违反不远离中枢追空铁律",
            portfolio_dir=pdir,
        )

        # Trade 5 (Untagged Small Winner): GC=F long -> +$10,000 (+1.0R)
        t5 = pm.open_position(
            symbol="GC=F",
            direction="long",
            sector_theme="precious_metals",
            entry_price=2600.0,
            tactical_stop_30m=2550.0,
            quantity=2,  # risk = $10,000
            campaign_1r_amount=10_000.0,
            portfolio_dir=pdir,
        )["position"]["position_id"]
        pm.close_position(
            t5,
            exit_price=2650.0,
            exit_reason="止盈",
            portfolio_dir=pdir,
        )

        # Leave 1 open main-wave position so positions.json has both open (main_wave) & closed records
        t6 = pm.open_position(
            symbol="HG=F",
            direction="long",
            sector_theme="base_metals",
            entry_price=4.20,
            tactical_stop_30m=4.05,
            structural_stop_daily=4.00,
            quantity=1,
            campaign_1r_amount=10_000.0,
            portfolio_dir=pdir,
        )["position"]["position_id"]
        pm.set_stop_regime(t6, "daily_structure", new_daily_stop=4.25, current_price=4.50, db_path=db_path)

        # Shadow Opportunities CRUD + validation
        with pytest.raises(pm.PortfolioError, match="invalid_planned_stop"):
            pm.log_opportunity(
                symbol="AVGO",
                market="US",
                sector_theme="ai_compute",
                direction="long",
                kind="missed_entry",
                anchor_price=150.0,
                planned_stop_price=155.0,  # wrong side for long!
                db_path=db_path,
            )

        opp1 = pm.log_opportunity(
            symbol="GEV",
            market="US",
            sector_theme="power_grid",
            direction="long",
            kind="premature_exit",
            anchor_price=320.0,
            planned_stop_price=300.0,  # 1R dist = 20.0
            subsequent_extreme_price=420.0,  # fav_move = 100 -> +31.25%, +5.0R!
            root_cause_tag="premature_profit_taking",
            lesson_note="主升未背驰提前止盈错失5R",
            linked_position_id=pid2,
            db_path=db_path,
        )
        assert opp1["unit_1r_distance"] == 20.0
        assert opp1["missed_move_pct"] == 31.25
        assert opp1["missed_r_multiple"] == 5.0

        opp2 = pm.log_opportunity(
            symbol="CU0",
            market="CN_FUTURES",
            sector_theme="copper",
            direction="short",
            kind="missed_entry",
            anchor_price=96_000.0,
            planned_stop_price=98_000.0,  # 1R dist = 2,000
            db_path=db_path,
        )
        assert opp2["missed_r_multiple"] is None

        # Update opp2 once price drops to 88,000 -> fav_move = 8,000 -> +4.0R, mark reviewed
        opp2_upd = pm.update_opportunity(
            opp2["opportunity_id"],
            subsequent_extreme_price=88_000.0,
            status="reviewed",
            root_cause_tag="hesitated_30m_2s",
            lesson_note="大B结构劈叉不敢空错失4R",
            db_path=db_path,
        )
        assert opp2_upd["missed_r_multiple"] == 4.0
        assert opp2_upd["missed_move_pct"] == pytest.approx(8.3333, abs=1e-4)
        assert opp2_upd["status"] == "reviewed"

        # Verify load_opportunities newest-first & filters
        all_opps = pm.load_opportunities(db_path=db_path)
        assert [o["symbol"] for o in all_opps] == ["CU0", "GEV"]
        tracking_opps = pm.load_opportunities(status="tracking", db_path=db_path)
        assert len(tracking_opps) == 1 and tracking_opps[0]["symbol"] == "GEV"

        # Verify opportunities.jsonl mirror ordered seq ASC (oldest first)
        opp_mirror_lines = [
            json.loads(line)
            for line in Path(pdir, "opportunities.jsonl").read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        assert [o["symbol"] for o in opp_mirror_lines] == ["GEV", "CU0"]

        # Verify compute_attribution_analytics exact numbers!
        # Closed trades:
        #   1) PWR:  +$35,000, +3.5R (full_main_wave_ride, positive)
        #   2) GEV:  +$5,000,  +0.5R (premature_profit_taking, mistake — positive PnL so not mistake loss R)
        #   3) NVDA: -$4,000,  -0.4R (normal_pilot_cost, pilot_friction)
        #   4) CL=F: -$8,000,  -0.8R (l4_chased_extended, mistake)
        #   5) GC=F: +$10,000, +1.0R (untagged)
        stats = pm.compute_attribution_analytics(db_path=db_path)
        assert stats["initialized"] is True
        assert stats["closed_count"] == 5
        assert stats["win_count"] == 3
        assert stats["loss_count"] == 2
        assert stats["flat_count"] == 0
        assert stats["win_rate_pct"] == 60.0
        assert stats["gross_profit"] == 50_000.0
        assert stats["gross_loss"] == 12_000.0
        assert stats["total_realized_pnl"] == 38_000.0
        assert stats["profit_factor"] == pytest.approx(50_000.0 / 12_000.0, abs=1e-4)

        # R metrics: wins = [3.5, 0.5, 1.0] -> avg_win_r = 1.6667; losses = [-0.4, -0.8] -> avg_loss_r = -0.6
        assert stats["total_realized_r"] == pytest.approx(3.8, abs=1e-4)
        assert stats["avg_win_r"] == pytest.approx(1.6667, abs=1e-4)
        assert stats["avg_loss_r"] == pytest.approx(-0.6, abs=1e-4)
        assert stats["avg_loss_r_abs"] == pytest.approx(0.6, abs=1e-4)
        assert stats["payoff_ratio_r"] == pytest.approx(1.6667 / 0.6, abs=1e-3)
        assert stats["expectancy_r"] == pytest.approx(3.8 / 5.0, abs=1e-4)
        assert stats["max_win_r"] == 3.5
        assert stats["max_loss_r"] == -0.8

        # Skewness: win_count=3 -> ceil(3*0.2)=1 top winner ($35,000 / $50,000 = 70.0%!)
        skew = stats["skewness"]
        assert skew["top_20pct_winner_count"] == 1
        assert skew["top_20pct_profit_share_pct"] == 70.0
        assert skew["multi_r_winners_count"] == 1
        assert skew["pilot_friction_loss_r"] == -0.4
        assert skew["preventable_mistake_loss_r"] == -0.8
        assert skew["untagged_closed_count"] == 1

        # by_tag sorted by abs(total_r) desc -> full_main_wave_ride (3.5R) first!
        assert stats["by_tag"][0]["tag"] == "full_main_wave_ride"
        assert stats["by_tag"][0]["total_r"] == 3.5

        # Shadow summary
        sh = stats["shadow_summary"]
        assert sh["total_count"] == 2
        assert sh["tracking_count"] == 1
        assert sh["missed_entry_count"] == 1
        assert sh["premature_exit_count"] == 1
        assert sh["total_missed_r"] == 9.0
        assert sh["max_missed_r"] == 5.0
        assert [o["symbol"] for o in sh["top_missed"]] == ["GEV", "CU0"]

        # Validate ALL exported JSON mirrors against portfolio/schema.json!
        acc_json = json.loads(Path(pdir, "account.json").read_text(encoding="utf-8"))
        pos_json = json.loads(Path(pdir, "positions.json").read_text(encoding="utf-8"))
        journal_lines = [
            json.loads(line)
            for line in Path(pdir, "journal.jsonl").read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        validate_against_schema(acc_json, schema["$defs"]["account"], schema, "account")
        validate_against_schema(pos_json, schema["$defs"]["positions_document"], schema, "positions_document")
        for idx, j_rec in enumerate(journal_lines):
            validate_against_schema(j_rec, schema["$defs"]["journal_entry"], schema, f"journal[{idx}]")
        for idx, o_rec in enumerate(opp_mirror_lines):
            validate_against_schema(o_rec, schema["$defs"]["opportunity_entry"], schema, f"opportunity[{idx}]")


# ---------------------------------------------------------------------------
# 5. CLI End-to-End Subcommands Test
# ---------------------------------------------------------------------------

def test_asymmetric_cli_subcommands_end_to_end():
    with tempfile.TemporaryDirectory() as tmpdir:
        def run_cli(argv: list[str]) -> tuple[int, dict[str, Any]]:
            out_buf, err_buf = io.StringIO(), io.StringIO()
            with redirect_stdout(out_buf), redirect_stderr(err_buf):
                rc = pm.main(["--portfolio-dir", tmpdir, *argv])
            raw = out_buf.getvalue().strip() or err_buf.getvalue().strip()
            return rc, json.loads(raw)

        rc, _ = run_cli(["init", "--equity", "200000"])
        assert rc == 0

        rc, opened = run_cli([
            "open", "--symbol", "NVDA", "--direction", "long", "--sector", "ai_compute",
            "--entry", "100", "--stop", "95", "--daily-stop", "90", "--quantity", "100",
            "--campaign-1r-amount", "2000",
        ])
        assert rc == 0
        pid = opened["position"]["position_id"]
        assert opened["position"]["campaign_1r_amount"] == 2000.0

        # CLI scale-in with --require-breakeven-lock
        rc_fail, err_fail = run_cli([
            "scale-in", "--id", pid, "--quantity", "100", "--price", "120",
            "--new-stop", "105", "--require-breakeven-lock",
        ])
        assert rc_fail == 2
        assert "breakeven_lock_required" in err_fail["error"]

        rc_ok, scaled = run_cli([
            "scale-in", "--id", pid, "--quantity", "100", "--price", "120",
            "--new-stop", "110", "--require-breakeven-lock",
        ])
        assert rc_ok == 0
        assert scaled["new_risk_amount"] == 0.0

        # CLI stop-regime handover to daily_structure
        rc_reg, reg_res = run_cli([
            "stop-regime", "--id", pid, "--regime", "daily_structure",
            "--new-daily-stop", "112", "--current-price", "130", "--reason", "CLI主升切换",
        ])
        assert rc_reg == 0
        assert reg_res["active_stop_regime"] == "daily_structure"
        assert reg_res["lifecycle_stage"] == "main_wave"

        # CLI close with attribution tag
        rc_cls, cls_res = run_cli([
            "close", "--id", pid, "--exit-price", "150", "--exit-reason", "主升兑现",
            "--attribution-tag", "full_main_wave_ride", "--lesson-note", "日线ZD防守吃满主升",
        ])
        assert rc_cls == 0
        assert cls_res["campaign_r_multiple"] == 4.0  # (150 - 110) * 200 = $8,000 / $2,000 = 4.0R

        # CLI opportunity-log, opportunity-update, opportunity-list, attribution
        rc_ol, opp = run_cli([
            "opportunity-log", "--symbol", "PWR", "--market", "US", "--sector", "power_grid",
            "--direction", "long", "--kind", "shaken_out", "--anchor-price", "200", "--planned-stop", "190",
            "--root-cause-tag", "shaken_out_30m_noise",
        ])
        assert rc_ol == 0
        oid = opp["opportunity_id"]

        rc_ou, opp_u = run_cli([
            "opportunity-update", "--id", oid, "--extreme-price", "260", "--status", "reviewed",
        ])
        assert rc_ou == 0
        assert opp_u["missed_r_multiple"] == 6.0

        rc_list, opp_list = run_cli(["opportunity-list", "--status", "reviewed"])
        assert rc_list == 0
        assert len(opp_list["opportunities"]) == 1

        rc_attr, attr_res = run_cli(["attribution"])
        assert rc_attr == 0
        assert attr_res["closed_count"] == 1
        assert attr_res["total_realized_r"] == 4.0
        assert attr_res["shadow_summary"]["max_missed_r"] == 6.0


# ---------------------------------------------------------------------------
# 6. Codex Review Regression Tests (HIGH #5/#6, MEDIUM #1/#3, LOW #1)
# ---------------------------------------------------------------------------

def test_codex_high5_tactical_stop_active_since_refreshes_on_regime_return_without_touching_daily(
    monkeypatch: pytest.MonkeyPatch,
):
    """HIGH #5: Switching back to unchanged tactical_30m stop refreshes tactical_stop_active_since_utc
    while keeping structural_stop_updated_at_utc strictly tied to structural_stop_daily edits.
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "portfolio.db"
        pm.init_portfolio(portfolio_dir=tmpdir, equity=500_000.0, base_currency="USD")

        monkeypatch.setattr(pm, "utc_now_str", lambda: "2026-06-01 00:00:00 UTC")
        opened = pm.open_position(
            symbol="NVDA",
            direction="long",
            sector_theme="ai_compute",
            entry_price=100.0,
            tactical_stop_30m=95.0,
            structural_stop_daily=90.0,
            quantity=200,
            portfolio_dir=tmpdir,
        )
        pid = opened["position"]["position_id"]
        assert opened["position"]["tactical_stop_active_since_utc"] == "2026-06-01 00:00:00 UTC"
        assert opened["position"]["structural_stop_updated_at_utc"] == "2026-06-01 00:00:00 UTC"

        # T1: trail 30m stop to 115 -> refreshes tactical_stop_active_since_utc, leaves daily timestamp intact
        monkeypatch.setattr(pm, "utc_now_str", lambda: "2026-06-02 00:00:00 UTC")
        trailed = pm.trail_stop(pid, new_stop_30m=115.0, current_price=125.0, portfolio_dir=tmpdir)
        assert trailed["position"]["stop_updated_at_utc"] == "2026-06-02 00:00:00 UTC"
        assert trailed["position"]["tactical_stop_active_since_utc"] == "2026-06-02 00:00:00 UTC"
        assert trailed["position"]["structural_stop_updated_at_utc"] == "2026-06-01 00:00:00 UTC"

        # T2: hand over to daily_structure at 108 -> updates structural_stop_updated_at_utc, leaves tactical active_since
        monkeypatch.setattr(pm, "utc_now_str", lambda: "2026-06-03 00:00:00 UTC")
        to_daily = pm.set_stop_regime(
            pid, "daily_structure", new_daily_stop=108.0, current_price=125.0, db_path=db_path
        )
        assert to_daily["position"]["structural_stop_updated_at_utc"] == "2026-06-03 00:00:00 UTC"
        assert to_daily["position"]["tactical_stop_active_since_utc"] == "2026-06-02 00:00:00 UTC"
        assert to_daily["position"]["regime_updated_at_utc"] == "2026-06-03 00:00:00 UTC"

        # T3: switch BACK to tactical_30m with OMITTED new_stop_30m (remains 115.0 unchanged!)
        # tactical_stop_active_since_utc MUST refresh to T3 while structural_stop_updated_at_utc stays T2!
        monkeypatch.setattr(pm, "utc_now_str", lambda: "2026-06-10 00:00:00 UTC")
        back_omitted = pm.set_stop_regime(pid, "tactical_30m", current_price=130.0, db_path=db_path)
        assert back_omitted["position"]["tactical_stop_30m"] == 115.0
        assert back_omitted["position"]["tactical_stop_active_since_utc"] == "2026-06-10 00:00:00 UTC"
        assert back_omitted["position"]["structural_stop_updated_at_utc"] == "2026-06-03 00:00:00 UTC"
        assert back_omitted["position"]["regime_updated_at_utc"] == "2026-06-10 00:00:00 UTC"

        # T4 -> T5: return to daily_structure (unchanged daily stop) then back with EXPLICIT unchanged new_stop_30m=115.0
        monkeypatch.setattr(pm, "utc_now_str", lambda: "2026-06-11 00:00:00 UTC")
        to_daily2 = pm.set_stop_regime(pid, "daily_structure", current_price=130.0, db_path=db_path)
        assert to_daily2["position"]["structural_stop_updated_at_utc"] == "2026-06-03 00:00:00 UTC"

        monkeypatch.setattr(pm, "utc_now_str", lambda: "2026-06-12 00:00:00 UTC")
        back_explicit_same = pm.set_stop_regime(
            pid, "tactical_30m", new_stop_30m=115.0, current_price=130.0, db_path=db_path
        )
        assert back_explicit_same["position"]["tactical_stop_active_since_utc"] == "2026-06-12 00:00:00 UTC"
        assert back_explicit_same["position"]["structural_stop_updated_at_utc"] == "2026-06-03 00:00:00 UTC"

        # T6: add_position_tranche tightening 30m stop also refreshes tactical_stop_active_since_utc
        monkeypatch.setattr(pm, "utc_now_str", lambda: "2026-06-13 00:00:00 UTC")
        scaled = pm.scale_in_position(
            pid, add_quantity=50, add_price=130.0, new_stop_loss=118.0, portfolio_dir=tmpdir
        )
        assert scaled["position"]["tactical_stop_active_since_utc"] == "2026-06-13 00:00:00 UTC"
        assert scaled["position"]["structural_stop_updated_at_utc"] == "2026-06-03 00:00:00 UTC"


def test_codex_high6_conservative_scale_in_margin_basis_and_max_leverage():
    """HIGH #6: Scale-in margin uses max(cost_notional, mark_notional) vs booked equity and enforces max_leverage."""
    metrics = pm.compute_conservative_notional_and_margin(
        quantity=45, avg_entry=126.666667, mark_price=140.0, multiplier=1.0, fx_rate=1.0, margin_rate=1.0
    )
    assert metrics["cost_notional"] == 5700.0
    assert metrics["mark_notional"] == 6300.0
    assert metrics["conservative_notional"] == 6300.0
    assert metrics["conservative_margin"] == 6300.0

    # 1) Cash equity: equity=5900, 15@100 + 30@140 -> cost=5700 <= 5900, BUT mark=6300 > 5900 -> REJECTED!
    with tempfile.TemporaryDirectory() as tmpdir:
        pm.init_portfolio(
            portfolio_dir=tmpdir,
            equity=5_900.0,
            base_currency="USD",
            default_risk_per_trade_pct=5.0,
            max_total_open_risk_pct=10.0,
            max_macro_factor_risk_pct=10.0,
        )
        pid = pm.open_position(
            symbol="NVDA",
            direction="long",
            sector_theme="ai_compute",
            entry_price=100.0,
            tactical_stop_30m=90.0,
            quantity=15,  # $150 risk, $1,500 cost
            portfolio_dir=tmpdir,
        )["position"]["position_id"]

        with pytest.raises(ValueError, match="portfolio_capital_limit_exceeded"):
            pm.scale_in_position(
                pid,
                add_quantity=30,
                add_price=140.0,
                new_stop_loss=130.0,  # breakeven locked (avg=126.67), cushion=600*0.65=390 >= 300 risk
                require_breakeven_lock=True,
                portfolio_dir=tmpdir,
            )

    # 2) Margin futures: margin passes, but explicit max_leverage rejects when conservative_notional > equity * max_leverage
    with tempfile.TemporaryDirectory() as tmpdir:
        pm.init_portfolio(
            portfolio_dir=tmpdir,
            equity=100_000.0,
            base_currency="USD",
            default_risk_per_trade_pct=5.0,
            max_total_open_risk_pct=10.0,
            max_macro_factor_risk_pct=10.0,
        )
        # CL=F: multiplier=1000, margin_rate=0.10
        pid_cl = pm.open_position(
            symbol="CL=F",
            direction="long",
            sector_theme="energy",
            entry_price=70.0,
            tactical_stop_30m=68.0,
            quantity=2,  # notional = 140k, margin = 14k, risk = 4k
            portfolio_dir=tmpdir,
        )["position"]["position_id"]

        # Invalid max_leverage <= 0 rejected
        with pytest.raises(pm.PortfolioError, match="max_leverage"):
            pm.scale_in_position(
                pid_cl,
                add_quantity=1,
                add_price=80.0,
                new_stop_loss=75.0,
                max_leverage=0.0,
                portfolio_dir=tmpdir,
            )

        # Adding 1 lot @ 80 -> qty=3, avg=73.333333, mark_notional = 3 * 80 * 1000 = 240,000 (2.4x equity!),
        # margin = 24,000 <= 100,000, BUT max_leverage=2.0 -> cap=200,000 -> REJECTED!
        with pytest.raises(pm.PortfolioError, match="leverage_limit_exceeded"):
            pm.scale_in_position(
                pid_cl,
                add_quantity=1,
                add_price=80.0,
                new_stop_loss=75.0,
                require_breakeven_lock=True,
                max_leverage=2.0,
                portfolio_dir=tmpdir,
            )

        # With max_leverage=2.5 -> cap=250,000 >= 240,000 -> ALLOWED, records margin_basis
        ok_cl = pm.scale_in_position(
            pid_cl,
            add_quantity=1,
            add_price=80.0,
            new_stop_loss=75.0,
            require_breakeven_lock=True,
            max_leverage=2.5,
            portfolio_dir=tmpdir,
        )
        assert ok_cl["margin_basis"] == "max(cost,mark_to_add)_vs_booked_equity"
        assert ok_cl["conservative_notional_account"] == 240_000.0
        assert ok_cl["conservative_margin_account"] == 24_000.0


def test_codex_medium1_campaign_breakeven_covers_prior_realized_loss_and_hard_cushion_ceiling():
    """MEDIUM #1: Campaign breakeven covers negative partial_realized_pnl in scale-in and set_stop_regime,
    and require_breakeven_lock=True enforces max_profit_cushion_ratio as a strict hard ceiling.
    """
    # Part A: Prior realized loss (-$100 from losing trim) must be covered by breakeven stop
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "portfolio.db"
        pm.init_portfolio(
            portfolio_dir=tmpdir,
            equity=100_000.0,
            base_currency="USD",
            default_risk_per_trade_pct=1.0,
        )
        pid = pm.open_position(
            symbol="NVDA",
            direction="long",
            sector_theme="ai_compute",
            entry_price=100.0,
            tactical_stop_30m=95.0,
            quantity=200,  # $1,000 initial risk
            portfolio_dir=tmpdir,
        )["position"]["position_id"]

        # Realize -$100 loss by trimming 100 shares @ $99 (remaining: 100 shares @ $100)
        pm.trim_position(pid, trim_price=99.0, trim_quantity=100, reason="减仓降险", portfolio_dir=tmpdir)
        summary_before = pm.summarize_portfolio(portfolio_dir=tmpdir)
        pos_sum = summary_before["open_positions"][0]
        assert pos_sum["remaining_open_risk_amount"] == 500.0
        assert pos_sum["campaign_open_risk_amount"] == 600.0
        assert pos_sum["campaign_net_pnl_at_stop"] == -600.0
        assert summary_before["total_remaining_open_risk_amount"] == 500.0
        assert summary_before["total_campaign_open_risk_amount"] == 600.0

        # 1) set_stop_regime("daily_structure") at avg_entry=100.0 leaves campaign -$100 underwater -> REJECTED!
        with pytest.raises(pm.PortfolioError, match=r"daily_stop_below_breakeven_floor.*101\.0"):
            pm.set_stop_regime(
                pid, "daily_structure", new_daily_stop=100.0, current_price=120.0, db_path=db_path
            )

        # 2) scale_in_position with require_breakeven_lock=True at new_avg=110.0 leaves campaign -$100 underwater -> REJECTED!
        #    new_qty = 200, new_avg = (100*100 + 100*120)/200 = 110.0; needs 110.0 + 100/200 = 110.5!
        with pytest.raises(pm.PortfolioError, match=r"breakeven_lock_required.*110\.5"):
            pm.scale_in_position(
                pid,
                add_quantity=100,
                add_price=120.0,
                new_stop_loss=110.0,
                require_breakeven_lock=True,
                portfolio_dir=tmpdir,
            )

        # 3) Stopping at 110.5 covers the -$100 prior realized loss -> campaign_open_risk == 0.0 -> ALLOWED!
        #    prior_unrealized = (120-100)*100 = 2000, partial_realized = -100 -> net_profit_cushion = 1900,
        #    allowed_giveback_ceiling = 1900 * 0.65 = 1235.0; added_tranche_risk = (120-110.5)*100 = 950.0 <= 1235.0
        ok_scale = pm.scale_in_position(
            pid,
            add_quantity=100,
            add_price=120.0,
            new_stop_loss=110.5,
            require_breakeven_lock=True,
            portfolio_dir=tmpdir,
        )
        assert ok_scale["breakeven_locked"] is True
        assert ok_scale["remaining_open_risk_amount"] == 0.0
        assert ok_scale["campaign_open_risk_amount"] == 0.0
        assert ok_scale["campaign_net_pnl_at_stop"] == 0.0
        assert ok_scale["net_profit_cushion_amount"] == 1900.0
        assert ok_scale["allowed_giveback_ceiling"] == 1235.0
        assert ok_scale["giveback_cap_mode"] == "strict_profit_cushion_ceiling"

        # 4) Now set_stop_regime("daily_structure") at 110.5 covers the -$100 realized loss -> ALLOWED!
        ok_daily = pm.set_stop_regime(
            pid, "daily_structure", new_daily_stop=110.5, current_price=125.0, db_path=db_path
        )
        assert ok_daily["active_stop_regime"] == "daily_structure"
        assert ok_daily["campaign_open_risk_amount"] == 0.0
        assert ok_daily["campaign_net_pnl_at_stop"] == 0.0

    # Part B: Strict hard profit cushion ceiling NOT overridden by single_sector_cap
    with tempfile.TemporaryDirectory() as tmpdir:
        pm.init_portfolio(
            portfolio_dir=tmpdir,
            equity=100_000.0,
            base_currency="USD",
            default_risk_per_trade_pct=5.0,  # single_sector_cap = $5,000 >> profit cushion!
            max_total_open_risk_pct=10.0,
            max_macro_factor_risk_pct=10.0,
        )
        pid2 = pm.open_position(
            symbol="NVDA",
            direction="long",
            sector_theme="ai_compute",
            entry_price=100.0,
            tactical_stop_30m=95.0,
            quantity=100,
            portfolio_dir=tmpdir,
        )["position"]["position_id"]

        # At add_price=110: net_profit_cushion = $1,000 -> hard ceiling = $1,000 * 0.65 = $650.
        # Adding 300 shares @ 110 with stop 107.5 (= new_avg): added_tranche_risk = $750 (> $650, even though << $5,000 sector cap!)
        with pytest.raises(pm.PortfolioError, match="profit_cushion_giveback_exceeded"):
            pm.scale_in_position(
                pid2,
                add_quantity=300,
                add_price=110.0,
                new_stop_loss=107.5,
                require_breakeven_lock=True,
                portfolio_dir=tmpdir,
            )

        # Adding 100 shares @ 110 with stop 105.0 (= new_avg): added_tranche_risk = $500 <= $650 -> ALLOWED!
        ok_hard = pm.scale_in_position(
            pid2,
            add_quantity=100,
            add_price=110.0,
            new_stop_loss=105.0,
            require_breakeven_lock=True,
            portfolio_dir=tmpdir,
        )
        assert ok_hard["allowed_giveback_ceiling"] == 650.0
        assert ok_hard["giveback_cap_mode"] == "strict_profit_cushion_ceiling"


def test_codex_medium3_update_opportunity_clears_extreme_price_and_root_cause_tag():
    """MEDIUM #3: update_opportunity supports explicit None to clear extreme price/metrics & root_cause_tag."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "portfolio.db"
        pm.init_portfolio(portfolio_dir=tmpdir, equity=200_000.0, base_currency="USD")

        opp = pm.log_opportunity(
            symbol="PWR",
            market="US",
            sector_theme="power_grid",
            direction="long",
            kind="shaken_out",
            anchor_price=200.0,
            planned_stop_price=190.0,
            subsequent_extreme_price=260.0,
            root_cause_tag="shaken_out_30m_noise",
            lesson_note="30m noise shakeout",
            db_path=db_path,
        )
        oid = opp["opportunity_id"]
        assert opp["missed_r_multiple"] == 6.0
        assert pm.compute_attribution_analytics(db_path=db_path)["shadow_summary"]["total_missed_r"] == 6.0

        # Omitted fields stay untouched when updating status only
        kept = pm.update_opportunity(oid, status="reviewed", db_path=db_path)
        assert kept["status"] == "reviewed"
        assert kept["subsequent_extreme_price"] == 260.0
        assert kept["missed_r_multiple"] == 6.0
        assert kept["root_cause_tag"] == "shaken_out_30m_noise"
        assert kept["lesson_note"] == "30m noise shakeout"

        # Explicitly passing None clears extreme price, missed_move_pct, missed_r_multiple, and root_cause_tag
        cleared = pm.update_opportunity(
            oid,
            subsequent_extreme_price=None,
            root_cause_tag=None,
            db_path=db_path,
        )
        assert cleared["subsequent_extreme_price"] is None
        assert cleared["missed_move_pct"] is None
        assert cleared["missed_r_multiple"] is None
        assert cleared["root_cause_tag"] is None
        assert cleared["lesson_note"] == "30m noise shakeout"

        # Removed from shadow_summary.total_missed_r
        sh = pm.compute_attribution_analytics(db_path=db_path)["shadow_summary"]
        assert sh["total_missed_r"] == 0.0
        assert sh["max_missed_r"] == 0.0
        assert sh["top_missed"] == []

        # CLI --clear-extreme and --clear-root-cause also work end-to-end
        pm.update_opportunity(oid, subsequent_extreme_price=240.0, root_cause_tag="hesitated", db_path=db_path)
        out_buf, err_buf = io.StringIO(), io.StringIO()
        with redirect_stdout(out_buf), redirect_stderr(err_buf):
            rc = pm.main([
                "--portfolio-dir", tmpdir, "opportunity-update",
                "--id", oid, "--clear-extreme", "--clear-root-cause",
            ])
        assert rc == 0
        cli_cleared = json.loads(out_buf.getvalue())
        assert cli_cleared["subsequent_extreme_price"] is None
        assert cli_cleared["missed_r_multiple"] is None
        assert cli_cleared["root_cause_tag"] is None


def test_codex_low1_subcent_campaign_1r_amount_rejected_on_open_and_rotate():
    """LOW #1: Explicit sub-cent campaign_1r_amount (e.g. 0.001) is rejected rather than silently rounded to 0.00."""
    with tempfile.TemporaryDirectory() as tmpdir:
        pm.init_portfolio(portfolio_dir=tmpdir, equity=100_000.0, base_currency="USD")

        with pytest.raises(pm.PortfolioError, match=r"campaign_1r_amount must be >= 0\.01"):
            pm.open_position(
                symbol="NVDA",
                direction="long",
                sector_theme="ai_compute",
                entry_price=100.0,
                tactical_stop_30m=95.0,
                quantity=100,
                campaign_1r_amount=0.001,
                portfolio_dir=tmpdir,
            )

        # Valid 0.01 succeeds
        opened = pm.open_position(
            symbol="NVDA",
            direction="long",
            sector_theme="ai_compute",
            entry_price=100.0,
            tactical_stop_30m=95.0,
            quantity=100,
            campaign_1r_amount=0.01,
            portfolio_dir=tmpdir,
        )
        pid = opened["position"]["position_id"]
        assert opened["position"]["campaign_1r_amount"] == 0.01

        # Sub-cent campaign_1r_amount on rotate_leader also rejected
        with pytest.raises(pm.PortfolioError, match=r"campaign_1r_amount must be >= 0\.01"):
            pm.rotate_leader(
                old_position_id=pid,
                exit_price=105.0,
                exit_reason="switch_leader",
                new_position_kwargs={
                    "symbol": "AVGO",
                    "direction": "long",
                    "entry_price": 150.0,
                    "tactical_stop_30m": 145.0,
                    "quantity": 50,
                    "campaign_1r_amount": 0.004,
                },
                portfolio_dir=tmpdir,
            )


def test_round2_unrounded_profit_cushion_ceiling_blocks_subcent_overshoot() -> None:
    """Round-2 LOW #1: $0.01 profit cushion * 0.65 = $0.0065 must reject $0.009 (90%) giveback."""
    with tempfile.TemporaryDirectory() as tmpdir:
        pm.init_portfolio(equity=100_000.0, portfolio_dir=tmpdir)
        opened = pm.open_position(
            symbol="NVDA",
            direction="long",
            sector_theme="ai_compute",
            entry_price=100.0,
            tactical_stop_30m=99.0,
            quantity=1,
            portfolio_dir=tmpdir,
        )
        pid = opened["position"]["position_id"]
        # Prior unrealized profit at add_price=100.01 is 1 * 0.01 = $0.01.
        # 65% cap = $0.0065. Adding 9 shares at 100.01 -> new_avg = 100.009 -> eff_stop = 100.009
        # -> added_tranche_risk_raw = (100.01 - 100.009) * 9 = $0.009 (90% of cushion) -> MUST REJECT!
        with pytest.raises(pm.PortfolioError, match="profit_cushion_giveback_exceeded"):
            pm.scale_in_position(
                position_id=pid,
                add_quantity=9,
                add_price=100.01,
                new_stop_loss=100.009,
                require_breakeven_lock=True,
                max_profit_cushion_ratio=0.65,
                portfolio_dir=tmpdir,
            )


def test_round2_clf_quantized_campaign_breakeven_matches_frontend_formula() -> None:
    """Round-2 MEDIUM #3: CL=F 20@90, -$0.01 realized, +13@103.000001 -> rejects 95.121213, accepts 95.121214."""
    with tempfile.TemporaryDirectory() as tmpdir:
        pm.init_portfolio(equity=5_000_000.0, default_risk_per_trade_pct=5.0, portfolio_dir=tmpdir)
        opened = pm.open_position(
            symbol="CL=F",
            direction="long",
            sector_theme="energy",
            entry_price=90.0,
            tactical_stop_30m=89.0,
            structural_stop_daily=85.0,
            quantity=21,
            portfolio_dir=tmpdir,
        )
        pid = opened["position"]["position_id"]
        # Trim 1 contract at 89.99999 -> partial_realized_pnl = -0.01 (1000 bbl * -0.00001)
        pm.trim_position(
            position_id=pid,
            trim_price=89.99999,
            trim_quantity=1,
            portfolio_dir=tmpdir,
        )
        pm.set_stop_regime(
            position_id=pid,
            regime="daily_structure",
            new_daily_stop=90.000001,
            current_price=103.0,
            portfolio_dir=tmpdir,
        )
        # 95.121213 is rejected; 95.121214 (matching frontend Math.round(rawNewAvg*1e6)/1e6 + offset ceil) succeeds!
        with pytest.raises(pm.PortfolioError, match="breakeven_lock_required"):
            pm.scale_in_position(
                position_id=pid,
                add_quantity=13,
                add_price=103.000001,
                new_daily_stop=95.121213,
                require_breakeven_lock=True,
                portfolio_dir=tmpdir,
            )
        res = pm.scale_in_position(
            position_id=pid,
            add_quantity=13,
            add_price=103.000001,
            new_daily_stop=95.121214,
            require_breakeven_lock=True,
            portfolio_dir=tmpdir,
        )
        assert res["position"]["structural_stop_daily"] == 95.121214
        assert res["position"]["campaign_open_risk_amount"] == 0.0


def test_round4_open_rejects_6dec_collapsed_stop_and_short_clf_near_midpoint_be() -> None:
    """Round-4: open_position rejects 6-dec collapsed entry/stop; CL=F short 20@110 + 20@83.000023 accepts 96.500010."""
    with tempfile.TemporaryDirectory() as tmpdir:
        pm.init_portfolio(equity=5_000_000.0, default_risk_per_trade_pct=5.0, portfolio_dir=tmpdir)

        # 1) Collapsed entry/stop at 6-decimal persistence precision is rejected
        with pytest.raises(ValueError, match="6-decimal precision"):
            pm.open_position(
                symbol="NVDA",
                direction="long",
                sector_theme="ai_compute",
                entry_price=0.5,
                tactical_stop_30m=0.4999996,
                quantity=100,
                portfolio_dir=tmpdir,
            )

        # 2) CL=F short 20@110, -$0.01 partial realized, +20@83.000023 -> rejects 96.500011, accepts 96.500010
        opened = pm.open_position(
            symbol="CL=F",
            direction="short",
            sector_theme="energy",
            entry_price=110.0,
            tactical_stop_30m=111.0,
            structural_stop_daily=115.0,
            quantity=21,
            portfolio_dir=tmpdir,
        )
        pid = opened["position"]["position_id"]
        pm.trim_position(
            position_id=pid,
            trim_price=110.00001,
            trim_quantity=1,
            portfolio_dir=tmpdir,
        )
        pm.set_stop_regime(
            position_id=pid,
            regime="daily_structure",
            new_daily_stop=109.999999,
            current_price=83.0,
            portfolio_dir=tmpdir,
        )
        with pytest.raises(pm.PortfolioError, match="breakeven_lock_required"):
            pm.scale_in_position(
                position_id=pid,
                add_quantity=20,
                add_price=83.000023,
                new_daily_stop=96.500011,
                require_breakeven_lock=True,
                portfolio_dir=tmpdir,
            )
        ok = pm.scale_in_position(
            position_id=pid,
            add_quantity=20,
            add_price=83.000023,
            new_daily_stop=96.500010,
            require_breakeven_lock=True,
            portfolio_dir=tmpdir,
        )
        assert ok["position"]["structural_stop_daily"] == 96.50001
        assert ok["position"]["campaign_open_risk_amount"] == 0.0

