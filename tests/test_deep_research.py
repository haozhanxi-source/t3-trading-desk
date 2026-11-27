"""Offline-safe unit & integration tests for Web AI Deep Research orchestration."""
from __future__ import annotations

import copy
import json
import os
import sys
import tempfile
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS_DIR = os.path.join(ROOT_DIR, "scripts")
for p in (ROOT_DIR, SCRIPTS_DIR):
    if p not in sys.path:
        sys.path.insert(0, p)

import market_snapshot
import portfolio_manager as pm
import research_archive as ra
import research_data

from backend import deep_research
from backend.app import create_app
from backend.cache import CACHE


def _sample_ai_payload(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "thesis_summary": "印尼 RKAB 配额节奏偏松叠加纯镍显性库存持续累库，中期偏空但需防近月低库存挤仓。",
        "direction_bias": "short",
        "status": "actionable_short",
        "layer1_drivers": {
            "primary": "资源国政策叙事：印尼 RKAB 2026 配额审批落地加快",
            "secondary": "供需平衡预期：精炼镍与 MHP 新产能爬坡压制中期平衡",
        },
        "chanlun_anchors": {
            "daily_direction": "空头",
            "daily_zs": [125000, 134000],
            "intraday_30m_signal": "30m 反抽不过 129500 形成二卖候选",
            "tactical_stop_30m": 130200,
            "structural_stop_daily": 134500,
        },
        "valuation_or_balance": {
            "regime": "过剩压制与成本支撑博弈",
            "key_anchor": "一体化火法现金成本约 118,000 元/吨",
            "upside_vs_downside": "基准看至 116,000，反向挤仓风险 135,000",
        },
        "thesis_md": (
            "# [CN_FUTURES] 沪镍 (NI0) 深度研究报告\n\n"
            "## 第三层：1Q~1Y 纯深度研究\n"
            "印尼镍矿配额边际放宽，精炼镍显性库存维持高位，中期供需过剩格局未改。\n\n"
            "## 第三层：红蓝对抗\n"
            "1. 隐性库存黑箱核验：LME 与 SHFE 双双累库，未见隐性去库背离。\n"
            "2. 政策抢出口错配：若印尼矿业税突袭上调，短期可能引发 10% 以上脉冲反弹。\n\n"
            "## 第四层：交易表达与 30min 缠论执行框架\n"
            "日线维持空头主结构，等待 30min 级别顶分型二卖确认后轻仓切入，30m 战术止损 130,200。\n"
        ),
        "evidence": [
            {
                "metric": "SHFE 镍期货仓单库存",
                "value": 31250,
                "unit": "吨",
                "period": "2026-11",
                "basis": "SHFE 官方日报",
                "kind": "fact",
                "level": "L1",
                "published_at": "2026-11-24",
                "source_url": "https://www.shfe.com.cn/",
                "source_locator": "brief.json:hard_data_bundle",
                "note": "显性库存处于近三年高位区间",
            },
            {
                "metric": "现货基差率",
                "value": -0.45,
                "unit": "%",
                "period": "2026-11",
                "basis": "生意社",
                "kind": "fact",
                "level": "L2",
                "note": "现货贴水反映当下现实偏弱",
            },
        ],
        "triggers": [
            {
                "id": "trig_ni0_30m_stop",
                "kind": "chanlun",
                "metric": "close",
                "operator": ">=",
                "threshold": 130200,
                "action": "exit_review",
                "status": "armed",
                "description": "突破 30min 战术止损位，空头节奏破坏果断撤离",
            },
            {
                "id": "trig_ni0_basis_squeeze",
                "kind": "term_structure",
                "metric": "basis_pct",
                "operator": ">=",
                "threshold": 3.5,
                "action": "alert",
                "status": "armed",
                "description": "现货大幅升水警惕近月挤仓风险",
            },
        ],
        "events": [
            {"date": "2026-12-15", "title": "印尼能矿部 2027 RKAB 配额听证窗口"},
            {"date": "2027-01-15", "title": "沪镍主力移仓换月窗口"},
        ],
        "pending_verifications": ["核验 12 月菲律宾红土镍矿雨季发运降幅"],
        "revision_note": "完成 NI0 首次完整第三层深研与 30m 缠论防线建档",
    }
    base.update(overrides)
    return base


def test_stream_parsers_claude_and_codex() -> None:
    cl_tool = json.dumps(
        {
            "type": "assistant",
            "message": {
                "content": [
                    {"type": "tool_use", "name": "WebSearch", "input": {"query": "Indonesia RKAB nickel quota 2026"}}
                ]
            },
        }
    )
    parsed_cl = deep_research.parse_claude_stream_line(cl_tool)
    assert parsed_cl is not None
    assert parsed_cl["kind"] == "assistant"
    assert any("印尼" in x or "RKAB" in x for x in parsed_cl["activities"])

    cl_res = json.dumps({"type": "result", "is_error": False, "result": '{"ok": true}', "duration_ms": 1200})
    parsed_res = deep_research.parse_claude_stream_line(cl_res)
    assert parsed_res is not None
    assert parsed_res["kind"] == "result"
    assert parsed_res["result"] == '{"ok": true}'

    cx_msg = json.dumps(
        {"type": "item.completed", "item": {"id": "item_1", "type": "agent_message", "text": '{"ok": true}'}}
    )
    parsed_cx = deep_research.parse_codex_stream_line(cx_msg)
    assert parsed_cx == {"kind": "agent_message", "text": '{"ok": true}'}

    cx_search = json.dumps({"type": "item.started", "item": {"type": "web_search", "query": "SHFE nickel inventory"}})
    parsed_cx_search = deep_research.parse_codex_stream_line(cx_search)
    assert parsed_cx_search is not None
    assert parsed_cx_search["kind"] == "activity"
    assert "SHFE nickel inventory" in parsed_cx_search["activities"][0]


def test_extract_json_payload_variants() -> None:
    raw_plain = '{"thesis_summary": "A", "direction_bias": "long"}'
    assert deep_research.extract_json_payload(raw_plain)["direction_bias"] == "long"

    raw_fenced = '下面是完整结果：\n```json\n{"thesis_summary": "B", "direction_bias": "short"}\n```\n结束。'
    assert deep_research.extract_json_payload(raw_fenced)["direction_bias"] == "short"

    raw_embedded = 'Note: done.\n{"thesis_summary": "C", "direction_bias": "neutral"}\nthanks'
    assert deep_research.extract_json_payload(raw_embedded)["direction_bias"] == "neutral"

    with pytest.raises(ValueError):
        deep_research.extract_json_payload("no json here at all")


def test_normalize_preserves_holding_status_and_cleans_invalid_items() -> None:
    payload = _sample_ai_payload(
        status="watchlist",
        direction_bias="INVALID_DIR",
        triggers=[
            {"id": "ok1", "kind": "chanlun", "metric": "close", "operator": ">=", "threshold": 130000, "action": "alert"},
            {"id": "bad_op", "kind": "chanlun", "metric": "close", "operator": "??", "threshold": 1, "action": "alert"},
        ],
        events=[
            {"date": "2026-12-20", "title": "Valid Event"},
            {"date": "bad-date", "title": "Invalid Event"},
        ],
    )
    normalized = deep_research.normalize_research_output(
        payload,
        existing_state={"status": "holding", "revision": 3},
        symbol="NI0",
    )
    # Active holding MUST NEVER be downgraded to watchlist by AI research
    assert normalized["status"] == "holding"
    assert normalized["direction_bias"] == "neutral"
    assert len(normalized["triggers"]) == 1
    assert normalized["triggers"][0]["id"] == "ok1"
    assert normalized["events"] == [{"date": "2026-12-20", "title": "Valid Event"}]


def test_run_deep_research_standard_and_adversarial_persists_archive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    res_dir = tmp_path / "research"
    monkeypatch.setattr(ra, "DEFAULT_RESEARCH_DIR", str(res_dir))
    CACHE.clear()

    # Initialize NI0 archive with an existing revision log line we must preserve
    ra.init_archive("NI0", name="沪镍", research_root=str(res_dir))
    ra.append_log("NI0", "手动建档初始化记录", research_root=str(res_dir))

    monkeypatch.setattr(
        research_data,
        "collect_research_data",
        lambda sym, **kw: {
            "symbol": sym,
            "asset_class": "cn_future",
            "sources_status": {"inventory": {"status": "ok"}},
            "fundamentals_and_supply": {"inventory": {"latest": 31250}},
        },
    )
    monkeypatch.setattr(
        market_snapshot,
        "get_snapshot",
        lambda sym, **kw: {
            "symbol": sym,
            "asset_class": "cn_future",
            "technicals": {"close": 127800},
            "chanlun": {"signal_summary": {"tier": "B", "direction": "short"}},
        },
    )
    monkeypatch.setattr(deep_research, "resolve_engine_binary", lambda eng: f"/fake/bin/{eng}")

    calls: list[dict[str, Any]] = []

    def fake_run_cli(*, engine, prompt, cwd, effort, emit, stage, timeout_seconds=540, cancel_event=None):
        calls.append({"engine": engine, "stage": stage, "cwd_has_brief": os.path.exists(os.path.join(cwd, "brief.json"))})
        emit("log", {"stage": stage, "engine": engine, "message": f"mock {engine} {stage}"})
        if stage == "adversarial_debate":
            return "## 反方质询\n1. 警惕印尼矿业税突袭导致 12% 挤仓风险。", {"engine": engine, "elapsed_s": 0.2}
        return json.dumps(_sample_ai_payload(), ensure_ascii=False), {"engine": engine, "elapsed_s": 0.3}

    monkeypatch.setattr(deep_research, "_run_agent_cli", fake_run_cli)

    events: list[tuple[str, dict[str, Any]]] = []
    result = deep_research.run_deep_research(
        {
            "symbol": "NI0",
            "market": "CN_FUTURES",
            "mode": "adversarial",
            "engine": "claude",
            "focus_prompt": "重点核查印尼 RKAB",
            "include_macro": True,
        },
        emit=lambda et, data: events.append((et, data)),
    )

    # Adversarial mode runs 3 phases: claude primary -> codex red team -> claude synthesis
    assert [c["stage"] for c in calls] == ["agent_research", "adversarial_debate", "agent_synthesis"]
    assert [c["engine"] for c in calls] == ["claude", "codex", "claude"]
    assert all(c["cwd_has_brief"] for c in calls)

    assert result["symbol"] == "NI0"
    assert result["direction_bias"] == "short"
    assert result["evidence_added"] == 2
    assert result["triggers_count"] == 2
    assert result["events_count"] == 2

    loaded = ra.load_archive("NI0", research_root=str(res_dir))
    assert loaded["state"]["direction_bias"] == "short"
    assert loaded["state"]["status"] == "actionable_short"
    assert "印尼 RKAB" in loaded["state"]["thesis_summary"]
    assert loaded["evidence_count"] == 2
    # Original manual log must remain preserved in thesis.md!
    assert "手动建档初始化记录" in loaded["thesis_md"]
    assert "跨模型红蓝对抗/CLAUDE" in loaded["thesis_md"]
    assert any(et == "progress" and d.get("done") == 5 for et, d in events)


def test_api_deep_research_single_flight_and_cancel(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    res_dir = tmp_path / "research"
    port_dir = tmp_path / "portfolio"
    monkeypatch.setattr(ra, "DEFAULT_RESEARCH_DIR", str(res_dir))
    monkeypatch.setattr(pm, "DEFAULT_PORTFOLIO_DIR", str(port_dir))
    CACHE.clear()

    monkeypatch.setattr(deep_research, "resolve_engine_binary", lambda eng: "/fake/bin/claude" if eng == "claude" else None)

    started = threading.Event()
    release = threading.Event()

    def slow_runner(params: dict[str, Any], emit, *, cancel_event: threading.Event | None = None):
        emit("step", {"stage": "collect_data", "message": "collecting"})
        started.set()
        while not release.is_set():
            if cancel_event is not None and cancel_event.is_set():
                raise deep_research.DeepResearchCancelledError("cancelled")
            time.sleep(0.02)
        return {"symbol": params["symbol"], "revision": 2}

    monkeypatch.setattr(deep_research, "run_deep_research", slow_runner)
    import backend.tasks as tasks_mod

    monkeypatch.setitem(tasks_mod.RUNNERS, "deep_research", slow_runner)

    app = create_app()
    with TestClient(app) as client:
        # 1. Engine availability endpoint
        eng_resp = client.get("/api/research/engines")
        assert eng_resp.status_code == 200
        eng_data = eng_resp.json()
        assert eng_data["default_engine"] == "claude"
        assert eng_data["any_available"] is True
        assert eng_data["adversarial_cross_model"] is False

        # 2. Unavailable engine returns 503
        r_503 = client.post(
            "/api/tasks/deep-research",
            json={"symbol": "NI0", "engine": "codex", "mode": "standard"},
        )
        assert r_503.status_code == 503

        # 3. Launch NI0 deep research + single-flight dedup on Chinese alias "沪镍"
        r1 = client.post(
            "/api/tasks/deep-research",
            json={"symbol": "NI0", "engine": "claude", "mode": "standard"},
        )
        assert r1.status_code == 202
        t1 = r1.json()
        assert t1["already_running"] is False
        assert started.wait(timeout=3.0)

        r2 = client.post(
            "/api/tasks/deep-research",
            json={"symbol": "沪镍", "engine": "claude", "mode": "standard"},
        )
        assert r2.status_code == 202
        t2 = r2.json()
        assert t2["already_running"] is True
        assert t2["id"] == t1["id"]

        # 4. Cancel running task cleanly
        rc = client.post(f"/api/tasks/{t1['id']}/cancel")
        assert rc.status_code == 200
        assert rc.json()["status"] == "cancelled"
        release.set()


def test_equity_prompt_and_template_include_upgraded_l3_audit_and_premortem(tmp_path: Path) -> None:
    prompt = deep_research.build_research_prompt(
        brief_path="brief.json",
        symbol="NVDA",
        market="US",
        asset_class="us_stock_or_etf",
        name="NVIDIA",
    )
    for required_phrase in (
        "受益纯度",
        "瓶颈久期",
        "NRE",
        "Mix Shift",
        "Drag Segment",
        "H2 Weighting",
        "Rule 10b-18",
        "红队 Pre-Mortem",
        "严禁用主观概率加权目标价",
        "严禁主张“因果链完好时杀估值就逆势加仓”",
    ):
        assert required_phrase in prompt, f"Missing expected stock Layer 3 rule: {required_phrase}"

    adv_prompt = deep_research.build_adversarial_prompt(
        brief_path="brief.json",
        symbol="NVDA",
        market="US",
        asset_class="us_stock_or_etf",
        name="NVIDIA",
        draft_summary="AI 算力龙头",
        draft_direction="long",
        draft_thesis_md="初稿正文",
    )
    assert "红队 Pre-Mortem" in adv_prompt
    assert "3~5 条具体、可证伪的失败路径" in adv_prompt

    # Commodity prompt stays clean (no stock-specific NRE/10b-18 pollution)
    comm_prompt = deep_research.build_research_prompt(
        brief_path="brief.json",
        symbol="NI0",
        market="CN_FUTURES",
        asset_class="cn_future",
        name="沪镍",
    )
    assert "Rule 10b-18" not in comm_prompt
    assert "强现实 vs 弱预期" in comm_prompt

    # Equity archive template upgraded with 10-item checklist & Pre-Mortem
    init_res = ra.init_archive("NVDA", name="NVIDIA", research_root=str(tmp_path))
    assert init_res["status"] == "created"
    loaded = ra.load_archive("NVDA", research_root=str(tmp_path))
    md = loaded["thesis_md"]
    assert "一手财报与订单核验清单（10 项" in md
    assert "期后 8-K 净现金与净债务桥" in md
    assert "红队 Pre-Mortem" in md
    assert "现价隐含预期拆解" in md


def test_prompt_includes_expectation_gap_across_all_asset_classes() -> None:
    cases = [
        ("NVDA", "US", "us_stock_or_etf", "NVIDIA"),
        ("NI0", "CN_FUTURES", "cn_future", "沪镍"),
        ("CL=F", "COMMODITIES", "commodity", "WTI原油"),
        ("USDJPY=X", "FX", "fx", "美元日元"),
        ("BTC", "CRYPTO", "crypto", "Bitcoin"),
    ]
    for sym, mkt, ac, name in cases:
        prompt = deep_research.build_research_prompt(
            brief_path="brief.json",
            symbol=sym,
            market=mkt,
            asset_class=ac,
            name=name,
        )
        for token in (
            '"expectation_gap"',
            '"priced_in_consensus"',
            '"our_variant_view"',
            '"crowding_assessment"',
            '"leading_kpis"',
            "underpriced",
            "fair",
            "crowded",
            "unknown",
            '"falsification_threshold"',
            '"cadence"',
        ):
            assert token in prompt, f"Missing {token} in prompt for {sym} ({ac})"


def test_normalize_research_output_expectation_gap_across_assets_and_fallback() -> None:
    # 1) Full & quirky expectation_gap normalization (coerces valid parts, drops malformed KPIs, caps at 6)
    payload_comm = _sample_ai_payload(
        expectation_gap={
            "priced_in_consensus": " 市场已计入 2026 印尼配额宽松与累库现实 ",
            "our_variant_view": " 高成本火法产线现金流亏损将在 Q2 触发集中检修减产 ",
            "crowding_assessment": "crowded（共识高度拥挤，警惕追空）",
            "leading_kpis": [
                "malformed_string_kpi",
                {"name": "   ", "current_baseline": "skip empty name"},
                {
                    "name": " 印尼 NPI 现汇现金利润 ",
                    "current_baseline": -450,
                    "expected_trajectory": "持续低于 -500 美元/吨触发减产",
                    "falsification_threshold": "回升至 > 0 美元/吨",
                    "cadence": "weekly",
                },
                {"name": "K2"},
                {"name": "K3"},
                {"name": "K4"},
                {"name": "K5"},
                {"name": "K6"},
                {"name": "K7_dropped_over_cap"},
            ],
        }
    )
    norm_comm = deep_research.normalize_research_output(payload_comm, existing_state={}, symbol="NI0")
    eg = norm_comm["expectation_gap"]
    assert eg["priced_in_consensus"] == "市场已计入 2026 印尼配额宽松与累库现实"
    assert eg["our_variant_view"] == "高成本火法产线现金流亏损将在 Q2 触发集中检修减产"
    assert eg["crowding_assessment"] == "crowded"
    assert len(eg["leading_kpis"]) == 6
    assert eg["leading_kpis"][0] == {
        "name": "印尼 NPI 现汇现金利润",
        "current_baseline": "-450",
        "expected_trajectory": "持续低于 -500 美元/吨触发减产",
        "falsification_threshold": "回升至 > 0 美元/吨",
        "cadence": "weekly",
    }

    # 2) Invalid crowding_assessment falls back to 'unknown' without failing
    payload_fx = _sample_ai_payload(
        expectation_gap={
            "priced_in_consensus": "市场定价年内美联储降息 50bp",
            "our_variant_view": "日本春斗薪资超预期推动 BOJ 提前加息",
            "crowding_assessment": "EXTREME_HYPE",
            "leading_kpis": "not-a-list",
        }
    )
    norm_fx = deep_research.normalize_research_output(payload_fx, existing_state={}, symbol="USDJPY=X")
    assert norm_fx["expectation_gap"]["crowding_assessment"] == "unknown"
    assert norm_fx["expectation_gap"]["leading_kpis"] == []

    # 3) Fallback: missing expectation_gap seeds priced_in_consensus from valuation_or_balance.implied_expectation_breakdown
    payload_eq = _sample_ai_payload(
        valuation_or_balance={
            "regime": "戴维斯双击",
            "key_anchor": "FY27E 28x GAAP PE",
            "implied_expectation_breakdown": "常态价值 $95 + 周期折现 $20 + 叙事残差 $25",
        }
    )
    payload_eq.pop("expectation_gap", None)
    norm_eq = deep_research.normalize_research_output(payload_eq, existing_state={}, symbol="NVDA")
    assert norm_eq["expectation_gap"] == {
        "priced_in_consensus": "常态价值 $95 + 周期折现 $20 + 叙事残差 $25",
        "our_variant_view": "",
        "crowding_assessment": "unknown",
        "leading_kpis": [],
    }

    # Explicit priced_in_consensus is NOT overwritten by implied_expectation_breakdown
    payload_eq_explicit = _sample_ai_payload(
        expectation_gap={
            "priced_in_consensus": "一致预期 FY27 数据中心增速放缓至 20%",
            "our_variant_view": "主权 AI 与推理集群拉动实际增速 > 35%",
            "crowding_assessment": "underpriced",
            "leading_kpis": [],
        },
        valuation_or_balance={
            "implied_expectation_breakdown": "常态价值 $95 + 周期折现 $20 + 叙事残差 $25",
        },
    )
    norm_eq_exp = deep_research.normalize_research_output(payload_eq_explicit, existing_state={}, symbol="NVDA")
    assert norm_eq_exp["expectation_gap"]["priced_in_consensus"] == "一致预期 FY27 数据中心增速放缓至 20%"
    assert norm_eq_exp["expectation_gap"]["crowding_assessment"] == "underpriced"


def test_merge_preserved_trigger_verifications_rules() -> None:
    valid_ver_a = {
        "method": "ai_web",
        "status": "clear",
        "verified_at_utc": "2026-11-25T08:30:00Z",
        "ttl_days": 7,
        "summary": "印尼能矿部尚未出台额外出口税",
        "source_url": "https://www.esdm.go.id/en",
        "evidence_tier": "L1",
    }
    valid_ver_b = {
        "method": "ai_web",
        "status": "clear",
        "verified_at_utc": "2026-11-26T09:00:00Z",
        "ttl_days": 7,
        "summary": "Preferred ID match verification",
        "source_url": "https://www.esdm.go.id/id",
        "evidence_tier": "L1",
    }
    valid_ver_manual = {
        "method": "manual",
        "status": "clear",
        "verified_at_utc": "2026-11-25T12:00:00Z",
        "ttl_days": 14,
        "observed": 42000,
        "summary": "调研确认样本冶炼厂未减产",
    }
    invalid_ver = {
        "method": "ai_web",
        "status": "clear",
        "verified_at_utc": "not-a-timestamp",
        "source_url": "https://example.com",
    }

    existing = [
        {
            "id": "trig_old_1",
            "kind": "policy",
            "evaluation_mode": "ai_web",
            "metric": "indonesia_export_tax",
            "operator": "event",
            "threshold": True,
            "action": "exit_review",
            "verification": valid_ver_a,
        },
        {
            "id": "trig_exact_id",
            "kind": "policy",
            "evaluation_mode": "ai_web",
            "metric": "indonesia_export_tax",
            "operator": "event",
            "threshold": True,
            "action": "exit_review",
            "verification": valid_ver_b,
        },
        {
            "id": "trig_manual_num",
            "kind": "supply_demand",
            "evaluation_mode": "manual",
            "metric": "smelter_cut_tonnes",
            "operator": ">=",
            "threshold": 50000,
            "action": "trim_or_exit",
            "verification": valid_ver_manual,
        },
        {
            "id": "trig_bad_ver",
            "kind": "policy",
            "evaluation_mode": "ai_web",
            "metric": "corrupt_ver_metric",
            "operator": "==",
            "threshold": "active",
            "action": "alert",
            "verification": invalid_ver,
        },
        {
            "id": "trig_auto_old",
            "kind": "chanlun",
            "evaluation_mode": "auto",
            "metric": "close",
            "operator": ">=",
            "threshold": 130200,
            "action": "exit_review",
            "verification": valid_ver_a,
        },
    ]

    new_triggers = [
        # 1) Multiple matches -> prefers matching id 'trig_exact_id' & case-insensitive metric
        {
            "id": "trig_exact_id",
            "kind": "policy",
            "evaluation_mode": "ai_web",
            "metric": "INDONESIA_EXPORT_TAX",
            "operator": "event",
            "threshold": True,
            "action": "exit_review",
        },
        # 2) Semantic float threshold equality (50000 vs "50000.0000000001") on manual trigger with renamed id
        {
            "id": "trig_manual_renamed",
            "kind": "supply_demand",
            "evaluation_mode": "manual",
            "metric": "smelter_cut_tonnes",
            "operator": ">=",
            "threshold": "50000.0000000001",
            "action": "trim_or_exit",
        },
        # 3) Threshold changed -> must NOT preserve verification
        {
            "id": "trig_thr_changed",
            "kind": "supply_demand",
            "evaluation_mode": "manual",
            "metric": "smelter_cut_tonnes",
            "operator": ">=",
            "threshold": 60000,
            "action": "trim_or_exit",
        },
        # 4) Bool vs int threshold mismatch (True vs 1) -> must NOT preserve verification
        {
            "id": "trig_bool_vs_int",
            "kind": "policy",
            "evaluation_mode": "ai_web",
            "metric": "indonesia_export_tax",
            "operator": "event",
            "threshold": 1,
            "action": "exit_review",
        },
        # 5) Operator changed -> must NOT preserve verification
        {
            "id": "trig_op_changed",
            "kind": "supply_demand",
            "evaluation_mode": "manual",
            "metric": "smelter_cut_tonnes",
            "operator": ">",
            "threshold": 50000,
            "action": "trim_or_exit",
        },
        # 6) Invalid verification in existing -> rejected by archive validator, not copied
        {
            "id": "trig_bad_ver",
            "kind": "policy",
            "evaluation_mode": "ai_web",
            "metric": "corrupt_ver_metric",
            "operator": "==",
            "threshold": "active",
            "action": "alert",
        },
        # 7) evaluation_mode == 'auto' -> NEVER gets or keeps verification
        {
            "id": "trig_auto_old",
            "kind": "chanlun",
            "evaluation_mode": "auto",
            "metric": "close",
            "operator": ">=",
            "threshold": 130200,
            "action": "exit_review",
            "verification": valid_ver_a,
        },
    ]

    merged = deep_research.merge_preserved_trigger_verifications(existing, new_triggers)
    assert len(merged) == 7

    # 1) Exact ID preferred & deep-copied
    assert merged[0]["verification"] == valid_ver_b
    assert merged[0]["verification"] is not valid_ver_b

    # 2) Manual trigger preserved across float formatting & id rename
    assert merged[1]["verification"] == valid_ver_manual
    assert merged[1]["verification"] is not valid_ver_manual

    # 3-6) Changed threshold / bool-int mismatch / changed operator / invalid verification -> no verification
    for idx in (2, 3, 4, 5):
        assert "verification" not in merged[idx], f"Index {idx} unexpectedly kept verification"

    # 7) Auto trigger stripped of verification
    assert "verification" not in merged[6]


def test_run_deep_research_persists_expectation_gap_and_preserves_verified_triggers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        monkeypatch.setattr(ra, "DEFAULT_RESEARCH_DIR", tmpdir)
        CACHE.clear()

        ra.init_archive("NI0", name="沪镍", research_root=tmpdir)
        ra.update_state(
            "NI0",
            {
                "triggers": [
                    {
                        "id": "trig_rkab_old",
                        "kind": "policy",
                        "evaluation_mode": "ai_web",
                        "metric": "rkab_quota_freeze",
                        "operator": "event",
                        "threshold": True,
                        "action": "exit_review",
                        "description": "  印尼冻结   RKAB 配额审批 ",
                    },
                    {
                        "id": "trig_royalty_old",
                        "kind": "policy",
                        "evaluation_mode": "ai_web",
                        "metric": "indonesia_royalty_rate_pct",
                        "operator": ">=",
                        "threshold": 10.0,
                        "action": "alert",
                    },
                    {
                        "id": "trig_close_auto",
                        "kind": "chanlun",
                        "evaluation_mode": "auto",
                        "metric": "close",
                        "operator": ">=",
                        "threshold": 130200,
                        "action": "exit_review",
                    },
                ]
            },
            research_root=tmpdir,
        )
        ver_ts = ra.utc_now_iso_seconds()
        ra.record_trigger_verifications(
            "NI0",
            {
                "trig_rkab_old": {
                    "method": "ai_web",
                    "status": "clear",
                    "verified_at_utc": ver_ts,
                    "summary": "印尼能矿部未冻结 RKAB 配额审批",
                    "source_url": "https://www.esdm.go.id/en",
                    "evidence_tier": "L1",
                },
                "trig_royalty_old": {
                    "method": "ai_web",
                    "status": "clear",
                    "verified_at_utc": ver_ts,
                    "observed": 8.0,
                    "summary": "当前镍矿特许权使用费维持 8%",
                    "source_url": "https://www.esdm.go.id/en",
                    "evidence_tier": "L1",
                },
            },
            research_root=tmpdir,
        )

        monkeypatch.setattr(
            research_data,
            "collect_research_data",
            lambda sym, **kw: {"symbol": sym, "asset_class": "cn_future"},
        )
        monkeypatch.setattr(
            market_snapshot,
            "get_snapshot",
            lambda sym, **kw: {"symbol": sym, "asset_class": "cn_future", "technicals": {"close": 126500}},
        )
        monkeypatch.setattr(deep_research, "resolve_engine_binary", lambda eng: f"/fake/bin/{eng}")

        ai_payload = _sample_ai_payload(
            expectation_gap={
                "priced_in_consensus": "市场一致预期精炼镍全年过剩 15 万吨",
                "our_variant_view": "印尼湿法中间品爬坡快于预期，过剩幅度扩大至 22 万吨",
                "crowding_assessment": "fair",
                "leading_kpis": [
                    {
                        "name": "SHFE+LME 显性纯镍总库存",
                        "current_baseline": "19.2 万吨",
                        "expected_trajectory": "Q1 突破 22 万吨",
                        "falsification_threshold": "连续 4 周去库累计 > 1.5 万吨",
                        "cadence": "weekly",
                    }
                ],
            },
            triggers=[
                # Unchanged definition (even with new id) -> MUST preserve verification
                {
                    "id": "trig_rkab_refreshed",
                    "kind": "policy",
                    "evaluation_mode": "ai_web",
                    "metric": "rkab_quota_freeze",
                    "operator": "event",
                    "threshold": True,
                    "action": "exit_review",
                    "description": "印尼冻结 RKAB 配额审批",
                },
                # Changed threshold (10.0 -> 12.0) -> MUST drop stale verification
                {
                    "id": "trig_royalty_old",
                    "kind": "policy",
                    "evaluation_mode": "ai_web",
                    "metric": "indonesia_royalty_rate_pct",
                    "operator": ">=",
                    "threshold": 12.0,
                    "action": "alert",
                    "description": "镍矿特许权使用费上调至 >= 12%",
                },
                # Auto metric -> NEVER carries verification
                {
                    "id": "trig_close_auto",
                    "kind": "chanlun",
                    "evaluation_mode": "auto",
                    "metric": "close",
                    "operator": ">=",
                    "threshold": 130200,
                    "action": "exit_review",
                },
            ],
        )

        monkeypatch.setattr(
            deep_research,
            "_run_agent_cli",
            lambda **kw: (json.dumps(ai_payload, ensure_ascii=False), {"engine": kw["engine"], "elapsed_s": 0.1}),
        )

        deep_research.run_deep_research(
            {"symbol": "NI0", "market": "CN_FUTURES", "mode": "standard", "engine": "claude"},
            emit=lambda _et, _d: None,
        )

        loaded = ra.load_archive("NI0", research_root=tmpdir)
        st = loaded["state"]
        assert st["expectation_gap"]["priced_in_consensus"] == "市场一致预期精炼镍全年过剩 15 万吨"
        assert st["expectation_gap"]["our_variant_view"] == "印尼湿法中间品爬坡快于预期，过剩幅度扩大至 22 万吨"
        assert st["expectation_gap"]["crowding_assessment"] == "fair"
        assert len(st["expectation_gap"]["leading_kpis"]) == 1
        assert st["expectation_gap"]["leading_kpis"][0]["name"] == "SHFE+LME 显性纯镍总库存"

        by_id = {t["id"]: t for t in st["triggers"]}
        assert "verification" in by_id["trig_rkab_refreshed"]
        assert by_id["trig_rkab_refreshed"]["verification"]["verified_at_utc"] == ver_ts
        assert "verification" not in by_id["trig_royalty_old"]
        assert "verification" not in by_id["trig_close_auto"]

        evaluated = {t["id"]: t for t in ra.evaluate_triggers(st, {"close": 126500})}
        assert evaluated["trig_rkab_refreshed"]["evaluated"] is True
        assert evaluated["trig_rkab_refreshed"]["fired"] is False
        assert evaluated["trig_royalty_old"]["evaluated"] is False
        assert evaluated["trig_royalty_old"].get("needs_verification") is True
        assert evaluated["trig_close_auto"]["evaluated"] is True
        assert evaluated["trig_close_auto"]["evaluation_source"] == "live"


def test_merge_preserved_trigger_verifications_strict_fingerprint_and_fail_closed() -> None:
    valid_ver = {
        "method": "ai_web",
        "status": "clear",
        "verified_at_utc": "2026-11-25T08:00:00Z",
        "ttl_days": 7,
        "summary": "已核验官网公告无异常",
        "source_url": "https://www.esdm.go.id/en",
        "evidence_tier": "L1",
    }

    existing = [
        {
            "id": "trig_desc",
            "kind": "policy",
            "evaluation_mode": "ai_web",
            "metric": "export_notice",
            "operator": "event",
            "threshold": True,
            "action": "exit_review",
            "status": "armed",
            "description": "查印尼公告",
            "verification_ttl_days": 7,
            "verification": valid_ver,
        },
        {
            "id": "trig_status",
            "kind": "policy",
            "evaluation_mode": "ai_web",
            "metric": "quota_freeze",
            "operator": "event",
            "threshold": True,
            "action": "alert",
            "status": "armed",
            "description": "查配额冻结",
            "verification": valid_ver,
        },
        {
            "id": "trig_ttl",
            "kind": "policy",
            "evaluation_mode": "ai_web",
            "metric": "mine_ban",
            "operator": "event",
            "threshold": True,
            "action": "exit_review",
            "status": "armed",
            "description": "查矿山停产公告",
            "ttl_days": 7,
            "verification": valid_ver,
        },
        {
            "id": "trig_eq_code",
            "kind": "policy",
            "evaluation_mode": "ai_web",
            "metric": "regulation_code",
            "operator": "==",
            "threshold": "001",
            "action": "alert",
            "status": "armed",
            "description": "核对法规编号",
            "verification": valid_ver,
        },
        {
            "id": "trig_contains_code",
            "kind": "policy",
            "evaluation_mode": "ai_web",
            "metric": "notice_code",
            "operator": "contains",
            "threshold": "001",
            "action": "alert",
            "status": "armed",
            "description": "核对公告代码",
            "verification": valid_ver,
        },
        {
            "id": "trig_num_order",
            "kind": "supply_demand",
            "evaluation_mode": "manual",
            "metric": "inventory_kt",
            "operator": ">=",
            "threshold": 100,
            "action": "alert",
            "status": "armed",
            "description": "核对隐性库存",
            "verification": {
                "method": "manual",
                "status": "clear",
                "verified_at_utc": "2026-11-25T08:00:00Z",
                "ttl_days": 7,
                "observed": 80,
                "summary": "调研确认隐性库存 80kt",
            },
        },
        {
            "id": "trig_ambig_1",
            "kind": "policy",
            "evaluation_mode": "ai_web",
            "metric": "dup_policy",
            "operator": "event",
            "threshold": True,
            "action": "alert",
            "status": "armed",
            "description": "重复策略触发器",
            "verification": valid_ver,
        },
        {
            "id": "trig_ambig_2",
            "kind": "policy",
            "evaluation_mode": "ai_web",
            "metric": "dup_policy",
            "operator": "event",
            "threshold": True,
            "action": "alert",
            "status": "armed",
            "description": "重复策略触发器",
            "verification": {**valid_ver, "summary": "另一条重复触发器核验"},
        },
    ]

    new_triggers = [
        # 1) Description changed ("查印尼公告" -> "查菲律宾公告") -> MUST drop verification
        {
            "id": "trig_desc",
            "kind": "policy",
            "evaluation_mode": "ai_web",
            "metric": "export_notice",
            "operator": "event",
            "threshold": True,
            "action": "exit_review",
            "status": "armed",
            "description": "查菲律宾公告",
            "verification_ttl_days": 7,
        },
        # 2) Status changed ("armed" -> "paused") -> MUST drop verification
        {
            "id": "trig_status",
            "kind": "policy",
            "evaluation_mode": "ai_web",
            "metric": "quota_freeze",
            "operator": "event",
            "threshold": True,
            "action": "alert",
            "status": "paused",
            "description": "查配额冻结",
        },
        # 3) TTL shortened (7 -> 1) -> MUST drop verification on merge
        {
            "id": "trig_ttl",
            "kind": "policy",
            "evaluation_mode": "ai_web",
            "metric": "mine_ban",
            "operator": "event",
            "threshold": True,
            "action": "exit_review",
            "status": "armed",
            "description": "查矿山停产公告",
            "ttl_days": 1,
        },
        # 4) Text threshold "001" vs "1" under == -> MUST NOT inherit
        {
            "id": "trig_eq_code",
            "kind": "policy",
            "evaluation_mode": "ai_web",
            "metric": "regulation_code",
            "operator": "==",
            "threshold": "1",
            "action": "alert",
            "status": "armed",
            "description": "核对法规编号",
        },
        # 5) Text threshold "001" vs 1 (str vs int) under == -> MUST NOT inherit
        {
            "id": "trig_eq_code_num",
            "kind": "policy",
            "evaluation_mode": "ai_web",
            "metric": "regulation_code",
            "operator": "==",
            "threshold": 1,
            "action": "alert",
            "status": "armed",
            "description": "核对法规编号",
        },
        # 6) Text threshold "001" vs "1" under contains -> MUST NOT inherit
        {
            "id": "trig_contains_code",
            "kind": "policy",
            "evaluation_mode": "ai_web",
            "metric": "notice_code",
            "operator": "contains",
            "threshold": "1",
            "action": "alert",
            "status": "armed",
            "description": "核对公告代码",
        },
        # 7) Numeric ordering >= still matches 100 vs 100.0
        {
            "id": "trig_num_order_renamed",
            "kind": "supply_demand",
            "evaluation_mode": "manual",
            "metric": "inventory_kt",
            "operator": ">=",
            "threshold": 100.0,
            "action": "alert",
            "status": "armed",
            "description": "  核对隐性库存  ",
        },
        # 8) Ambiguous multiple matching old triggers without matching id -> MUST fail closed
        {
            "id": "trig_ambig_unmatched",
            "kind": "policy",
            "evaluation_mode": "ai_web",
            "metric": "dup_policy",
            "operator": "event",
            "threshold": True,
            "action": "alert",
            "status": "armed",
            "description": "重复策略触发器",
        },
    ]

    existing_snapshot = copy.deepcopy(existing)
    new_snapshot = copy.deepcopy(new_triggers)

    merged = deep_research.merge_preserved_trigger_verifications(existing, new_triggers)
    assert existing == existing_snapshot
    assert new_triggers == new_snapshot

    by_id = {t["id"]: t for t in merged}
    for dropped_id in (
        "trig_desc",
        "trig_status",
        "trig_ttl",
        "trig_eq_code",
        "trig_eq_code_num",
        "trig_contains_code",
        "trig_ambig_unmatched",
    ):
        assert "verification" not in by_id[dropped_id], f"{dropped_id} unexpectedly inherited verification"

    assert "verification" in by_id["trig_num_order_renamed"]
    assert by_id["trig_num_order_renamed"]["verification"]["observed"] == 80

    # Also verify research_archive rejects 2-day-old verification with ver.ttl_days=7 when trigger ttl_days=1
    state_short_ttl = {
        "triggers": [
            {
                "id": "trig_short_ttl_eval",
                "kind": "policy",
                "evaluation_mode": "ai_web",
                "metric": "mine_ban",
                "operator": "event",
                "threshold": True,
                "action": "exit_review",
                "status": "armed",
                "ttl_days": 1,
                "verification": valid_ver,  # verified 2026-11-25T08:00:00Z with ttl_days=7
            }
        ]
    }
    eval_res = ra.evaluate_triggers(
        state_short_ttl,
        {},
        now_utc=datetime.fromisoformat("2026-11-27T09:00:00+00:00"),
    )
    assert eval_res[0]["evaluated"] is False
    assert eval_res[0].get("verification_stale") is True
    assert "过期" in eval_res[0].get("error", "")


def test_round2_whitespace_threshold_and_auto_id_ambiguity_do_not_inherit() -> None:
    """Round-2 HIGH #2: exact whitespace in text thresholds and auto-generated trig_ai_N IDs must fail closed."""
    valid_ver_clear = {
        "method": "ai_web",
        "status": "clear",
        "observed": None,
        "observed_text": "no match",
        "summary": "clear",
        "evidence_tier": "L1",
        "source_title": "Official Notice",
        "source_url": "https://example.gov/notice",
        "verified_at_utc": "2026-11-26T08:00:00+00:00",
        "ttl_days": 7,
    }
    valid_ver_fired = {
        **valid_ver_clear,
        "status": "fired",
        "summary": "fired",
    }

    # 1) Whitespace change "001 " -> "001" under == and contains must NOT inherit
    for op in ("==", "contains"):
        old_t = [
            {
                "id": "code_check",
                "kind": "policy",
                "evaluation_mode": "ai_web",
                "metric": "policy_code",
                "operator": op,
                "threshold": "001 ",
                "action": "alert",
                "status": "armed",
                "description": "检查公告代码",
                "verification": valid_ver_clear,
            }
        ]
        new_t = [
            {
                "id": "code_check",
                "kind": "policy",
                "evaluation_mode": "ai_web",
                "metric": "policy_code",
                "operator": op,
                "threshold": "001",
                "action": "alert",
                "status": "armed",
                "description": "检查公告代码",
            }
        ]
        merged = deep_research.merge_preserved_trigger_verifications(old_t, new_t)
        assert "verification" not in merged[0], f"Whitespace change under {op} must not inherit"

    # 2) Two identical existing triggers (clear vs fired) with auto-generated IDs:
    #    new trigger without explicit ID gets normalized to trig_ai_1 -> must NOT inherit!
    old_ambig = [
        {
            "id": "trig_ai_1",
            "kind": "policy",
            "evaluation_mode": "ai_web",
            "metric": "export_ban",
            "operator": "event",
            "threshold": True,
            "action": "exit",
            "status": "armed",
            "description": "出口禁令",
            "verification": valid_ver_clear,
        },
        {
            "id": "trig_ai_2",
            "kind": "policy",
            "evaluation_mode": "ai_web",
            "metric": "export_ban",
            "operator": "event",
            "threshold": True,
            "action": "exit",
            "status": "armed",
            "description": "出口禁令",
            "verification": valid_ver_fired,
        },
    ]
    new_auto = [
        {
            "id": "trig_ai_1",
            "kind": "policy",
            "evaluation_mode": "ai_web",
            "metric": "export_ban",
            "operator": "event",
            "threshold": True,
            "action": "exit",
            "status": "armed",
            "description": "出口禁令",
        }
    ]
    merged_ambig = deep_research.merge_preserved_trigger_verifications(old_ambig, new_auto)
    assert "verification" not in merged_ambig[0]

    # 3) Round-3 HIGH: contains threshold 1.0 vs 1 must NOT inherit (str(1.0) == "1.0" != "1")
    old_float_contains = [
        {
            "id": "code_num",
            "kind": "policy",
            "evaluation_mode": "ai_web",
            "metric": "official_code",
            "operator": "contains",
            "threshold": 1.0,
            "action": "exit",
            "status": "armed",
            "description": "官方代码包含检查",
            "verification": valid_ver_clear,
        }
    ]
    new_int_contains = [
        {
            "id": "code_num",
            "kind": "policy",
            "evaluation_mode": "ai_web",
            "metric": "official_code",
            "operator": "contains",
            "threshold": 1,
            "action": "exit",
            "status": "armed",
            "description": "官方代码包含检查",
        }
    ]
    merged_num = deep_research.merge_preserved_trigger_verifications(old_float_contains, new_int_contains)
    assert "verification" not in merged_num[0]

    # 4) Round-4 HIGH: contains threshold -0.0 vs 0.0 and dict key-order changes must NOT inherit
    old_neg_zero = [
        {
            "id": "neg_zero_contains",
            "kind": "policy",
            "evaluation_mode": "ai_web",
            "metric": "delta_token",
            "operator": "contains",
            "threshold": -0.0,
            "action": "exit",
            "status": "armed",
            "description": "负零标记检查",
            "verification": valid_ver_clear,
        }
    ]
    new_pos_zero = [
        {
            "id": "neg_zero_contains",
            "kind": "policy",
            "evaluation_mode": "ai_web",
            "metric": "delta_token",
            "operator": "contains",
            "threshold": 0.0,
            "action": "exit",
            "status": "armed",
            "description": "负零标记检查",
        }
    ]
    merged_zero = deep_research.merge_preserved_trigger_verifications(old_neg_zero, new_pos_zero)
    assert "verification" not in merged_zero[0]

    old_dict_order = [
        {
            "id": "dict_order_contains",
            "kind": "policy",
            "evaluation_mode": "ai_web",
            "metric": "payload_token",
            "operator": "contains",
            "threshold": {"a": 1, "b": 2},
            "action": "exit",
            "status": "armed",
            "description": "对象键值顺序检查",
            "verification": valid_ver_clear,
        }
    ]
    new_dict_order = [
        {
            "id": "dict_order_contains",
            "kind": "policy",
            "evaluation_mode": "ai_web",
            "metric": "payload_token",
            "operator": "contains",
            "threshold": {"b": 2, "a": 1},
            "action": "exit",
            "status": "armed",
            "description": "对象键值顺序检查",
        }
    ]
    merged_dict = deep_research.merge_preserved_trigger_verifications(old_dict_order, new_dict_order)
    assert "verification" not in merged_dict[0]


def test_round5_async_verification_rejects_type_sign_and_dict_order_threshold_drift() -> None:
    """Round-5 HIGH: record_trigger_verifications must reject stale clear when threshold drifts across 1/1.0, -0.0/0.0, or dict key order."""
    with tempfile.TemporaryDirectory() as tmpdir:
        ra.init_archive("NI0", name="沪镍", research_root=tmpdir)
        initial_triggers = [
            {
                "id": "t_int_float",
                "kind": "policy",
                "evaluation_mode": "ai_web",
                "metric": "official_code",
                "operator": "contains",
                "threshold": 1,
                "action": "exit",
                "status": "armed",
                "description": "int vs float",
            },
            {
                "id": "t_neg_zero",
                "kind": "policy",
                "evaluation_mode": "ai_web",
                "metric": "delta_token",
                "operator": "contains",
                "threshold": -0.0,
                "action": "exit",
                "status": "armed",
                "description": "-0.0 vs 0.0",
            },
            {
                "id": "t_dict_order",
                "kind": "policy",
                "evaluation_mode": "ai_web",
                "metric": "payload_token",
                "operator": "contains",
                "threshold": {"a": 1, "b": 2},
                "action": "exit",
                "status": "armed",
                "description": "dict order",
            },
        ]
        ra.update_state("NI0", {"triggers": initial_triggers}, research_root=tmpdir)

        # Snapshot fingerprints before concurrent mutation (as trigger_verifier.py does)
        expected_fps = {t["id"]: ra.trigger_definition_fingerprint(t) for t in initial_triggers}

        # Mutate thresholds concurrently while async verification is in flight
        mutated_triggers = [
            {**initial_triggers[0], "threshold": 1.0},
            {**initial_triggers[1], "threshold": 0.0},
            {**initial_triggers[2], "threshold": {"b": 2, "a": 1}},
        ]
        ra.update_state("NI0", {"triggers": mutated_triggers}, research_root=tmpdir)

        ver_ts = ra.utc_now_iso_seconds()
        stale_clear = {
            "method": "ai_web",
            "status": "clear",
            "verified_at_utc": ver_ts,
            "summary": "stale clear from pre-mutation definition",
            "source_url": "https://example.gov/notice",
            "evidence_tier": "L1",
        }
        res = ra.record_trigger_verifications(
            "NI0",
            {t["id"]: stale_clear for t in initial_triggers},
            expected_fingerprints=expected_fps,
            research_root=tmpdir,
        )
        assert res["updated_ids"] == []
        assert {c["id"] for c in res["skipped_conflicts"]} == {"t_int_float", "t_neg_zero", "t_dict_order"}

        loaded = ra.load_archive("NI0", research_root=tmpdir)
        for trig in loaded["state"]["triggers"]:
            assert "verification" not in trig, f"Stale clear leaked onto mutated trigger {trig['id']}"

