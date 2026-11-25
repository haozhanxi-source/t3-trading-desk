"""Offline unit tests for portfolio浮盈加仓 (scale_in_position) and research archive status auto-sync."""
from __future__ import annotations

import io
import json
import os
import sys
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

import pytest

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS_DIR = os.path.join(ROOT_DIR, "scripts")
if SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, SCRIPTS_DIR)

import portfolio_manager as pm
import research_archive as ra


# ---------------------------------------------------------------------------
# 1. Scale-in (浮盈加仓) core rules & invariants
# ---------------------------------------------------------------------------
def test_scale_in_long_success_weighted_avg_risk_and_mirrors(tmp_path: Path):
    pdir = str(tmp_path / "portfolio")
    pm.init_portfolio(
        portfolio_dir=pdir,
        equity=100_000.0,
        base_currency="USD",
        default_risk_per_trade_pct=1.0,
        max_total_open_risk_pct=6.0,
        max_macro_factor_risk_pct=3.0,
    )
    opened = pm.open_position(
        symbol="NVDA",
        direction="long",
        sector_theme="ai_compute",
        entry_price=100.0,
        tactical_stop_30m=95.0,
        quantity=100,  # initial risk = (100 - 95) * 100 = $500 (0.5%)
        macro_factors=["us_real_rate"],
        exit_red_lines=["30m break below 95"],
        portfolio_dir=pdir,
    )
    pid = opened["position"]["position_id"]

    # Scale in T2 on floating profit (110 > 100), tightening stop from 95 -> 102
    # New qty = 150, weighted avg = (100*100 + 110*50)/150 = 103.333333
    # Combined open risk = (103.333333 - 102) * 150 = $200.0 (0.2%)
    # Added tranche standalone risk = (110 - 102) * 50 = $400.0 <= $1,000 cap
    scaled = pm.scale_in_position(
        position_id=pid,
        add_quantity=50,
        add_price=110.0,
        new_stop_loss=102.0,
        note="T2 30m三买确认浮盈加仓",
        portfolio_dir=pdir,
    )
    assert scaled["status"] == "added"
    assert scaled["old_quantity"] == 100
    assert scaled["add_quantity"] == 50
    assert scaled["new_quantity"] == 150
    assert scaled["old_entry_price"] == 100.0
    assert scaled["add_price"] == 110.0
    assert scaled["new_entry_price"] == pytest.approx(103.333333, abs=1e-6)
    assert scaled["old_stop_loss"] == 95.0
    assert scaled["new_stop_loss"] == 102.0
    assert scaled["old_risk_amount"] == 500.0
    assert scaled["new_risk_amount"] == 200.0
    assert scaled["old_risk_pct"] == 0.5
    assert scaled["new_risk_pct"] == 0.2
    assert scaled["tranche_count"] == 2
    assert scaled["note"] == "T2 30m三买确认浮盈加仓"
    assert scaled["mirror_sync_ok"] is True

    # Verify positions.json mirror & journal.jsonl mirror
    pos_mirror = json.loads((tmp_path / "portfolio" / "positions.json").read_text(encoding="utf-8"))
    assert len(pos_mirror["positions"]) == 1
    p = pos_mirror["positions"][0]
    assert p["quantity"] == 150
    assert p["avg_entry_price"] == pytest.approx(103.333333, abs=1e-6)
    assert p["tactical_stop_30m"] == 102.0
    assert p["current_open_risk_amount"] == 200.0
    assert len(p["tranches"]) == 2
    assert p["tranches"][1]["reason"] == "T2 30m三买确认浮盈加仓"

    journal_lines = [
        json.loads(line)
        for line in (tmp_path / "portfolio" / "journal.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert len(journal_lines) == 2
    j = journal_lines[1]
    assert j["action"] == "add_on_profit"
    assert j["event"] == "scale_in"
    assert j["old_quantity"] == 100
    assert j["add_quantity"] == 50
    assert j["new_quantity"] == 150
    assert j["old_entry_price"] == 100.0
    assert j["add_price"] == 110.0
    assert j["new_entry_price"] == pytest.approx(103.333333, abs=1e-6)
    assert j["old_stop_loss"] == 95.0
    assert j["new_stop_loss"] == 102.0
    assert j["old_risk_pct"] == 0.5
    assert j["new_risk_pct"] == 0.2
    assert j["note"] == "T2 30m三买确认浮盈加仓"


def test_scale_in_rejects_floating_loss_and_breakeven_long_and_short(tmp_path: Path):
    pdir = str(tmp_path / "portfolio")
    pm.init_portfolio(portfolio_dir=pdir, equity=1_000_000.0, base_currency="CNY")

    # Long position
    long_pos = pm.open_position(
        symbol="600519",
        direction="long",
        sector_theme="cn_consumer",
        entry_price=1500.0,
        tactical_stop_30m=1450.0,
        quantity=100,
        portfolio_dir=pdir,
    )
    long_id = long_pos["position"]["position_id"]

    with pytest.raises(ValueError, match="浮亏或平价摊平"):
        pm.scale_in_position(long_id, add_quantity=100, add_price=1490.0, portfolio_dir=pdir)
    with pytest.raises(ValueError, match="浮亏或平价摊平"):
        pm.scale_in_position(long_id, add_quantity=100, add_price=1500.0, portfolio_dir=pdir)

    # Short commodity future
    short_pos = pm.open_position(
        symbol="CU0",
        direction="short",
        sector_theme="base_metals",
        entry_price=80_000.0,
        tactical_stop_30m=81_000.0,
        quantity=1,  # 1 * 1000 * 5 = 5,000 CNY <= 10,000 cap
        portfolio_dir=pdir,
    )
    short_id = short_pos["position"]["position_id"]

    with pytest.raises(ValueError, match="浮亏或平价摊平"):
        pm.scale_in_position(short_id, add_quantity=1, add_price=80_500.0, portfolio_dir=pdir)
    with pytest.raises(ValueError, match="浮亏或平价摊平"):
        pm.scale_in_position(short_id, add_quantity=1, add_price=80_000.0, portfolio_dir=pdir)

    # Valid short profit scale-in (price < 80_000, stop tightened downward 81_000 -> 79_500)
    ok_short = pm.scale_in_position(
        short_id,
        add_quantity=1,
        add_price=78_000.0,
        new_stop_loss=79_500.0,
        note="空头浮盈顺势加仓",
        portfolio_dir=pdir,
    )
    assert ok_short["new_quantity"] == 2
    assert ok_short["new_entry_price"] == 79_000.0
    assert ok_short["new_stop_loss"] == 79_500.0
    # Combined risk = (79500 - 79000) * 2 * 5 = 5000; added tranche risk = (79500 - 78000) * 1 * 5 = 7500
    assert ok_short["new_risk_amount"] == 5000.0


def test_scale_in_rejects_stop_widening_and_stop_crossing(tmp_path: Path):
    pdir = str(tmp_path / "portfolio")
    pm.init_portfolio(portfolio_dir=pdir, equity=100_000.0, base_currency="USD")
    pid = pm.open_position(
        symbol="NVDA",
        direction="long",
        sector_theme="ai_compute",
        entry_price=100.0,
        tactical_stop_30m=95.0,
        quantity=100,
        portfolio_dir=pdir,
    )["position"]["position_id"]

    # Loosening stop (95 -> 92) must be rejected
    with pytest.raises(ValueError, match="移动止损只紧不松"):
        pm.scale_in_position(pid, add_quantity=50, add_price=110.0, new_stop_loss=92.0, portfolio_dir=pdir)

    # Stop above or equal to add_price must be rejected
    with pytest.raises(ValueError, match="stop_consistency_violation"):
        pm.scale_in_position(pid, add_quantity=50, add_price=110.0, new_stop_loss=110.0, portfolio_dir=pdir)
    with pytest.raises(ValueError, match="stop_consistency_violation"):
        pm.scale_in_position(pid, add_quantity=50, add_price=110.0, new_stop_loss=112.0, portfolio_dir=pdir)


def test_scale_in_risk_budget_gate_and_breakeven_stop_frees_budget(tmp_path: Path):
    pdir = str(tmp_path / "portfolio")
    pm.init_portfolio(
        portfolio_dir=pdir,
        equity=100_000.0,
        base_currency="USD",
        default_risk_per_trade_pct=1.0,  # $1,000 single-sector cap
        max_total_open_risk_pct=6.0,
        max_macro_factor_risk_pct=3.0,
    )
    # Full 1% risk budget consumed on T1: (100 - 95) * 200 = $1,000
    pid = pm.open_position(
        symbol="NVDA",
        direction="long",
        sector_theme="ai_compute",
        entry_price=100.0,
        tactical_stop_30m=95.0,
        quantity=200,
        macro_factors=["us_real_rate"],
        portfolio_dir=pdir,
    )["position"]["position_id"]

    # 1) Without tightening stop, adding 50 shares at 120 breaches $1,000 cap -> REJECTED with hint
    with pytest.raises(ValueError, match="保本/浮盈区释放风险额度"):
        pm.scale_in_position(pid, add_quantity=50, add_price=120.0, portfolio_dir=pdir)

    # 2) Move stop into profit/breakeven zone (110) directly while scaling in 80 shares at 120:
    #    new_qty = 280, new_avg = (100*200 + 120*80)/280 = 105.714286
    #    eff_stop = 110 > 105.714286 -> combined open risk = 0.0 (locked profit = 1200.0)!
    #    added_tranche_risk = (120 - 110) * 80 = $800 <= $1,000 cap -> ALLOWED!
    res = pm.scale_in_position(
        pid,
        add_quantity=80,
        add_price=120.0,
        new_stop_loss=110.0,
        note="上移止损锁利释放风险后加仓",
        portfolio_dir=pdir,
    )
    assert res["new_quantity"] == 280
    assert res["new_entry_price"] == pytest.approx(105.714286, abs=1e-6)
    assert res["new_risk_amount"] == 0.0
    assert res["new_risk_pct"] == 0.0
    assert res["position"]["locked_profit_amount"] == 1200.0
    assert res["added_tranche_risk_amount"] == 800.0


def test_scale_in_total_and_macro_factor_caps_and_fx_validation(tmp_path: Path):
    pdir = str(tmp_path / "portfolio")
    pm.init_portfolio(
        portfolio_dir=pdir,
        equity=100_000.0,
        base_currency="USD",
        default_risk_per_trade_pct=2.0,   # $2,000 sector cap
        max_total_open_risk_pct=3.5,      # $3,500 total cap
        max_macro_factor_risk_pct=2.5,    # $2,500 macro factor cap
    )
    # Position 1 in sector A uses $1,800 of macro factor "usd_rate"
    pm.open_position(
        symbol="PWR",
        direction="long",
        sector_theme="power_grid",
        entry_price=200.0,
        tactical_stop_30m=182.0,
        quantity=100,  # $1,800 risk
        macro_factors=["usd_rate"],
        portfolio_dir=pdir,
    )
    # Position 2 in sector B uses $500 of macro factor "usd_rate" (total = $2,300 <= $2,500 macro / $3,500 total)
    pid2 = pm.open_position(
        symbol="NVDA",
        direction="long",
        sector_theme="ai_compute",
        entry_price=100.0,
        tactical_stop_30m=95.0,
        quantity=100,  # $500 risk
        macro_factors=["usd_rate"],
        portfolio_dir=pdir,
    )["position"]["position_id"]

    # Scaling in NVDA (combined risk $1,250 <= $2,000 sector, total $3,050 <= $3,500 total)
    # breaches macro factor "usd_rate" ($1,800 + $1,250 = $3,050 > $2,500) -> REJECTED
    with pytest.raises(ValueError, match="macro_factor_overexposed"):
        pm.scale_in_position(pid2, add_quantity=50, add_price=110.0, portfolio_dir=pdir)

    # Also verify total_open_risk_exceeded when macro cap is not the bottleneck
    # Open a non-overlapping sector C using $1,000 -> total open risk = $3,300 <= $3,500
    pid3 = pm.open_position(
        symbol="GC=F",
        direction="long",
        sector_theme="precious_metals",
        entry_price=2000.0,
        tactical_stop_30m=1990.0,
        quantity=1,  # 1 * 10 * 100 = $1,000 risk
        macro_factors=["real_yield"],
        portfolio_dir=pdir,
    )["position"]["position_id"]
    # Trigger total_open_risk_exceeded cleanly: others=$2,300, adding 1 GC=F at 2010 with stop 1995 -> added risk=$1,500 -> $3,800 > $3,500
    with pytest.raises(ValueError, match="add_tranche_total_open_risk_exceeded"):
        pm.scale_in_position(
            pid3,
            add_quantity=1,
            add_price=2010.0,
            new_stop_loss=1995.0,
            portfolio_dir=pdir,
        )

    # Same-currency USD trade with non-1.0 fx_rate_to_base MUST be rejected
    with pytest.raises(ValueError, match="Same-currency trade"):
        pm.scale_in_position(
            pid2,
            add_quantity=10,
            add_price=110.0,
            new_stop_loss=105.0,
            fx_rate_to_base=0.14,
            portfolio_dir=pdir,
        )


def test_scale_in_cli_subcommand(tmp_path: Path):
    pdir = str(tmp_path / "portfolio")

    def run_cli(argv: list[str]) -> tuple[int, dict]:
        out_buf, err_buf = io.StringIO(), io.StringIO()
        with redirect_stdout(out_buf), redirect_stderr(err_buf):
            rc = pm.main(["--portfolio-dir", pdir, *argv])
        raw = out_buf.getvalue().strip() or err_buf.getvalue().strip()
        return rc, json.loads(raw)

    assert run_cli(["init", "--equity", "100000"])[0] == 0
    rc, opened = run_cli([
        "open", "--symbol", "NVDA", "--direction", "long", "--sector", "ai_compute",
        "--entry", "100", "--stop", "95", "--quantity", "100",
    ])
    assert rc == 0
    pid = opened["position"]["position_id"]

    rc, scaled = run_cli([
        "scale-in", "--id", pid, "--quantity", "50", "--price", "112",
        "--new-stop-loss", "104", "--note", "CLI T2加仓",
    ])
    assert rc == 0
    assert scaled["new_quantity"] == 150
    assert scaled["new_stop_loss"] == 104.0
    assert scaled["note"] == "CLI T2加仓"


# ---------------------------------------------------------------------------
# 2. Research archive status auto-sync (holding <-> watchlist) & anti-pollution
# ---------------------------------------------------------------------------
def test_archive_status_sync_open_close_void_rotate_and_isolation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    pdir = str(tmp_path / "portfolio")
    rdir = str(tmp_path / "research")
    pm.init_portfolio(portfolio_dir=pdir, equity=500_000.0, base_currency="USD")

    # Seed research archives in isolated tmp research_dir
    ra.init_archive("NVDA", name="NVIDIA", research_root=rdir)
    ra.init_archive("AVGO", name="Broadcom", research_root=rdir)
    assert ra.load_archive("NVDA", research_root=rdir)["state"]["status"] == "watchlist"
    assert ra.load_archive("AVGO", research_root=rdir)["state"]["status"] == "watchlist"

    # A) Anti-pollution: calling open_position with temp portfolio_dir & research_dir=None MUST NOT touch any research dir
    opened_iso = pm.open_position(
        symbol="NVDA",
        direction="long",
        sector_theme="ai_compute",
        entry_price=120.0,
        tactical_stop_30m=115.0,
        quantity=100,
        portfolio_dir=pdir,
    )
    assert opened_iso["archive_status_sync"]["synced"] is False
    assert opened_iso["archive_status_sync"]["skipped_reason"] == "research_dir_isolated"
    assert ra.load_archive("NVDA", research_root=rdir)["state"]["status"] == "watchlist"

    # Void it cleanly first
    pm.void_position(opened_iso["position"]["position_id"], portfolio_dir=pdir)

    # B) Anti-pollution 2: monkeypatching DEFAULT_PORTFOLIO_DIR to tmp without monkeypatching ra.DEFAULT_RESEARCH_DIR
    #    MUST STILL refuse to touch the real repo research/ dir!
    monkeypatch.setattr(pm, "DEFAULT_PORTFOLIO_DIR", pdir)
    assert pm._resolve_sync_research_dir(pdir, None) is None

    # C) Explicit research_dir -> open syncs NVDA watchlist -> holding
    opened = pm.open_position(
        symbol="NVDA",
        direction="long",
        sector_theme="ai_compute",
        entry_price=120.0,
        tactical_stop_30m=115.0,
        quantity=100,
        portfolio_dir=pdir,
        research_dir=rdir,
    )
    pid_nvda = opened["position"]["position_id"]
    assert opened["archive_status_sync"]["synced"] is True
    assert opened["archive_status_sync"]["previous_status"] == "watchlist"
    assert opened["archive_status_sync"]["current_status"] == "holding"
    assert ra.load_archive("NVDA", research_root=rdir)["state"]["status"] == "holding"

    # D) Open unarchived symbol PWR -> does NOT create an empty archive
    opened_pwr = pm.open_position(
        symbol="PWR",
        direction="long",
        sector_theme="power_grid",
        entry_price=300.0,
        tactical_stop_30m=290.0,
        quantity=50,
        portfolio_dir=pdir,
        research_dir=rdir,
    )
    assert opened_pwr["archive_status_sync"]["synced"] is False
    assert opened_pwr["archive_status_sync"]["skipped_reason"] == "archive_not_found"
    with pytest.raises(FileNotFoundError):
        ra.load_archive("PWR", research_root=rdir)

    # E) Rotate NVDA -> AVGO syncs NVDA holding -> watchlist and AVGO watchlist -> holding
    rotated = pm.rotate_leader(
        old_position_id=pid_nvda,
        exit_price=130.0,
        exit_reason="switch_to_avgo",
        new_position_kwargs={
            "symbol": "AVGO",
            "direction": "long",
            "sector_theme": "ai_compute",
            "entry_price": 180.0,
            "tactical_stop_30m": 170.0,
            "quantity": 100,
        },
        portfolio_dir=pdir,
        research_dir=rdir,
    )
    pid_avgo = rotated["opened_position"]["position_id"]
    assert rotated["archive_status_sync"]["closed_symbol"]["synced"] is True
    assert rotated["archive_status_sync"]["closed_symbol"]["current_status"] == "watchlist"
    assert rotated["archive_status_sync"]["opened_symbol"]["synced"] is True
    assert rotated["archive_status_sync"]["opened_symbol"]["current_status"] == "holding"
    assert ra.load_archive("NVDA", research_root=rdir)["state"]["status"] == "watchlist"
    assert ra.load_archive("AVGO", research_root=rdir)["state"]["status"] == "holding"

    # F) Close AVGO -> AVGO returns to watchlist
    closed = pm.close_position(
        position_id=pid_avgo,
        exit_price=195.0,
        exit_reason="target_hit",
        portfolio_dir=pdir,
        research_dir=rdir,
    )
    assert closed["archive_status_sync"]["synced"] is True
    assert closed["archive_status_sync"]["current_status"] == "watchlist"
    assert ra.load_archive("AVGO", research_root=rdir)["state"]["status"] == "watchlist"

    # G) Open NVDA then void -> NVDA goes holding -> watchlist
    opened2 = pm.open_position(
        symbol="NVDA",
        direction="long",
        sector_theme="ai_compute",
        entry_price=125.0,
        tactical_stop_30m=120.0,
        quantity=100,
        portfolio_dir=pdir,
        research_dir=rdir,
    )
    assert ra.load_archive("NVDA", research_root=rdir)["state"]["status"] == "holding"
    voided = pm.void_position(
        position_id=opened2["position"]["position_id"],
        reason="误录作废",
        portfolio_dir=pdir,
        research_dir=rdir,
    )
    assert voided["archive_status_sync"]["synced"] is True
    assert voided["archive_status_sync"]["current_status"] == "watchlist"
    assert ra.load_archive("NVDA", research_root=rdir)["state"]["status"] == "watchlist"

    # H) Sync failure (corrupted state.json) MUST NEVER abort committed portfolio open
    folder, _, _, _ = ra.resolve_archive_dir("NVDA", research_root=rdir)
    Path(folder, "state.json").write_text("{corrupt json", encoding="utf-8")
    opened_corrupt = pm.open_position(
        symbol="NVDA",
        direction="long",
        sector_theme="ai_compute",
        entry_price=126.0,
        tactical_stop_30m=120.0,
        quantity=100,
        portfolio_dir=pdir,
        research_dir=rdir,
    )
    assert opened_corrupt["status"] == "opened"
    assert opened_corrupt["archive_status_sync"]["synced"] is False
    assert "error" in opened_corrupt["archive_status_sync"]
    assert any("archive_status_sync_failed" in w for w in opened_corrupt["warnings"])
    assert any(p["symbol"] == "NVDA" for p in pm.load_positions(pdir, status="open"))


def _init_ledger(tmp_path: Path) -> str:
    pdir = str(tmp_path / "portfolio")
    pm.init_portfolio(
        portfolio_dir=pdir,
        equity=100_000.0,
        base_currency="USD",
        default_risk_per_trade_pct=1.0,
        max_total_open_risk_pct=6.0,
        max_macro_factor_risk_pct=3.0,
    )
    return pdir


def test_p1_4_scale_in_precision_and_small_fx_rate_consistency(tmp_path: Path):
    """P1-4: Scale-in validation & persistence share identical precision; small FX rates are not truncated."""
    pdir = _init_ledger(tmp_path)

    # Case 1: Long avg 0.100001; add_price=0.1000011 and new_stop_loss=0.10000105 round to 0.100001 -> MUST reject
    opened_penny = pm.open_position(
        symbol="DOGE-USD",
        direction="long",
        sector_theme="crypto_meme",
        entry_price=0.100001,
        tactical_stop_30m=0.090000,
        quantity=10000,
        portfolio_dir=pdir,
    )
    pid_penny = opened_penny["position"]["position_id"]
    with pytest.raises(ValueError, match="floating_loss_add_forbidden"):
        pm.scale_in_position(
            position_id=pid_penny,
            add_quantity=1000,
            add_price=0.1000011,
            new_stop_loss=0.10000105,
            portfolio_dir=pdir,
        )

    # Case 2: USDIDR=X with fx_rate_to_base=0.00006149 -> persisted fx_to_account MUST match margin math
    opened_idr = pm.open_position(
        symbol="USDIDR=X",
        direction="long",
        sector_theme="fx_em",
        entry_price=16200.0,
        tactical_stop_30m=16000.0,
        quantity=1000,
        fx_to_account=0.00006149,
        portfolio_dir=pdir,
    )
    pid_idr = opened_idr["position"]["position_id"]
    assert opened_idr["position"]["fx_to_account"] == pytest.approx(0.00006149, abs=1e-12)

    scaled_idr = pm.scale_in_position(
        position_id=pid_idr,
        add_quantity=1000,
        add_price=16600.0,
        new_stop_loss=16350.0,
        fx_rate_to_base=0.00006149,
        portfolio_dir=pdir,
    )
    pos_after = scaled_idr["position"]
    assert pos_after["fx_to_account"] == pytest.approx(0.00006149, abs=1e-12)
    recomputed_margin = pm._position_estimated_margin_account(pos_after)
    assert pos_after["estimated_margin_account"] == pytest.approx(recomputed_margin, abs=0.01)


def test_p1_5_cross_market_same_symbol_archive_isolation(tmp_path: Path):
    """P1-5: Opening/closing US:SOL MUST only sync US/SOL and NEVER touch CRYPTO/SOL-USD."""
    pdir = _init_ledger(tmp_path)
    rdir = str(tmp_path / "research")
    ra.init_archive("US:SOL", name="Emeren Group", direction_bias="long", research_root=rdir)
    ra.init_archive("SOL", name="Solana", direction_bias="long", research_root=rdir)

    assert ra.load_archive("US:SOL", research_root=rdir)["state"]["status"] == "watchlist"
    assert ra.load_archive("SOL", research_root=rdir)["state"]["status"] == "watchlist"

    # Open US:SOL -> ONLY US/SOL becomes holding; CRYPTO/SOL-USD stays watchlist
    opened_us = pm.open_position(
        symbol="US:SOL",
        direction="long",
        sector_theme="us_solar",
        entry_price=10.0,
        tactical_stop_30m=9.0,
        quantity=100,
        portfolio_dir=pdir,
        research_dir=rdir,
    )
    assert opened_us["archive_status_sync"]["synced"] is True
    assert opened_us["archive_status_sync"]["market"] == "US"
    assert ra.load_archive("US:SOL", research_root=rdir)["state"]["status"] == "holding"
    assert ra.load_archive("SOL", research_root=rdir)["state"]["status"] == "watchlist"

    # Also open CRYPTO SOL -> CRYPTO/SOL-USD becomes holding
    opened_crypto = pm.open_position(
        symbol="SOL",
        direction="long",
        sector_theme="crypto_l1",
        entry_price=150.0,
        tactical_stop_30m=140.0,
        quantity=10,
        portfolio_dir=pdir,
        research_dir=rdir,
    )
    assert opened_crypto["archive_status_sync"]["market"] == "CRYPTO"
    assert ra.load_archive("SOL", research_root=rdir)["state"]["status"] == "holding"

    # Close US:SOL -> US/SOL returns to watchlist even though CRYPTO/SOL-USD is still open!
    closed_us = pm.close_position(
        position_id=opened_us["position"]["position_id"],
        exit_price=11.0,
        exit_reason="take_profit",
        portfolio_dir=pdir,
        research_dir=rdir,
    )
    assert closed_us["archive_status_sync"]["synced"] is True
    assert closed_us["archive_status_sync"]["market"] == "US"
    assert ra.load_archive("US:SOL", research_root=rdir)["state"]["status"] == "watchlist"
    assert ra.load_archive("SOL", research_root=rdir)["state"]["status"] == "holding"


def test_p1_6_idempotent_replay_compensates_failed_archive_sync(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """P1-6: If archive sync fails transiently after SQLite commit, idempotent replay MUST compensate against live ledger."""
    pdir = _init_ledger(tmp_path)
    rdir = str(tmp_path / "research")
    ra.init_archive("NVDA", name="NVIDIA", direction_bias="long", research_root=rdir)

    real_atomic_write_json = ra._atomic_write_json
    fail_once = {"open": True, "close": True}

    def flaky_write_open(path, data):
        if fail_once["open"] and str(path).endswith("state.json"):
            fail_once["open"] = False
            raise OSError("transient disk full")
        return real_atomic_write_json(path, data)

    monkeypatch.setattr(ra, "_atomic_write_json", flaky_write_open)

    # 1) First open commits SQLite but archive write fails -> archive stays watchlist
    r1 = pm.open_position(
        symbol="NVDA",
        direction="long",
        sector_theme="ai_compute",
        entry_price=120.0,
        tactical_stop_30m=115.0,
        quantity=100,
        idempotency_key="open-nvda-1",
        portfolio_dir=pdir,
        research_dir=rdir,
    )
    assert r1["status"] == "opened"
    assert r1["archive_status_sync"]["synced"] is False
    assert "error" in r1["archive_status_sync"]
    assert ra.load_archive("NVDA", research_root=rdir)["state"]["status"] == "watchlist"

    # 2) Retry with same idempotency_key -> idempotent_replay=True AND archive compensated to holding!
    r1_retry = pm.open_position(
        symbol="NVDA",
        direction="long",
        sector_theme="ai_compute",
        entry_price=120.0,
        tactical_stop_30m=115.0,
        quantity=100,
        idempotency_key="open-nvda-1",
        portfolio_dir=pdir,
        research_dir=rdir,
    )
    assert r1_retry["idempotent_replay"] is True
    assert r1_retry["archive_status_sync"]["synced"] is True
    assert r1_retry["archive_status_sync"]["current_status"] == "holding"
    assert ra.load_archive("NVDA", research_root=rdir)["state"]["status"] == "holding"

    # 3) Close with transient failure + idempotent replay compensates back to watchlist
    def flaky_write_close(path, data):
        if fail_once["close"] and str(path).endswith("state.json"):
            fail_once["close"] = False
            raise OSError("transient disk full on close")
        return real_atomic_write_json(path, data)

    monkeypatch.setattr(ra, "_atomic_write_json", flaky_write_close)
    pid = r1["position"]["position_id"]
    c1 = pm.close_position(
        position_id=pid,
        exit_price=130.0,
        exit_reason="target_hit",
        idempotency_key="close-nvda-1",
        portfolio_dir=pdir,
        research_dir=rdir,
    )
    assert c1["status"] == "closed"
    assert c1["archive_status_sync"]["synced"] is False
    assert ra.load_archive("NVDA", research_root=rdir)["state"]["status"] == "holding"

    c1_retry = pm.close_position(
        position_id=pid,
        exit_price=130.0,
        exit_reason="target_hit",
        idempotency_key="close-nvda-1",
        portfolio_dir=pdir,
        research_dir=rdir,
    )
    assert c1_retry["idempotent_replay"] is True
    assert c1_retry["archive_status_sync"]["synced"] is True
    assert c1_retry["archive_status_sync"]["current_status"] == "watchlist"
    assert ra.load_archive("NVDA", research_root=rdir)["state"]["status"] == "watchlist"

    # 4) Stale open_position replay AFTER close MUST NOT revert watchlist back to holding!
    stale_open_replay = pm.open_position(
        symbol="NVDA",
        direction="long",
        sector_theme="ai_compute",
        entry_price=120.0,
        tactical_stop_30m=115.0,
        quantity=100,
        idempotency_key="open-nvda-1",
        portfolio_dir=pdir,
        research_dir=rdir,
    )
    assert stale_open_replay["idempotent_replay"] is True
    assert ra.load_archive("NVDA", research_root=rdir)["state"]["status"] == "watchlist"

