"""Offline-safe unit & integration tests for Web AI Deep Research orchestration."""
from __future__ import annotations

import json
import os
import sys
import threading
import time
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

