"""Tests for Layer 1 Top-Down Strategic Compass (hardened against all Codex review findings)."""
from __future__ import annotations

import json
import sys
import threading
import time
from pathlib import Path
from unittest.mock import patch

import pandas as pd
import pytest
from fastapi.testclient import TestClient

from backend import app as app_mod
from backend import deep_research, macro_compass
from backend.cache import CACHE
import data_store
import research_archive
import scan_opportunities


SAMPLE_AI_JSON = {
    "regime_title": "高真实利率尾声 + 资源国供给收缩与过剩品分化",
    "regime_summary": "美国真实利率仍处偏紧区间但通胀粘性抬升，中国财政托底但地产需求偏弱，未来1Q~1Y宜做多供给硬约束品种、做空过剩弱预期商品。",
    "macro_signals": {
        "us_macro": "美债10Y真实利率维持高位，美元区间偏强。",
        "china_macro": "中国制造业PMI在荣枯线附近震荡，地产链承压。",
        "geopolitics_supply": "主产国矿业配额与出口限制强化铜/镍供给溢价。",
        "tech_industry": "AI数据中心资本开支向电力电网设备外溢。",
    },
    "avoid_sectors": [
        {"sector": "传统地产建材链", "reason": "竣工端需求缺乏弹性，中枢持续下移。"},
        {"sector": "同质化光伏组件", "reason": "产能出清未完成，价格战压制盈利。"},
    ],
    "directions": [
        {
            "id": "copper_supply_squeeze",
            "rank": 1,
            "title": "全球铜矿供给收缩与电力需求共振",
            "direction": "long",
            "horizon": "2Q~1Y",
            "conviction": "high",
            "asset_scope": "commodities",
            "primary_driver": {"dimension": "地缘与资源国政策", "summary": "智利与刚果金供给扰动频发。"},
            "secondary_driver": {"dimension": "技术与产业迭代", "summary": "AI数据中心与欧美电网更新拉动用铜增量。"},
            "thesis": "未来1~2年新增大型铜矿投产稀缺，加工费TC维持低位印证矿端紧缺，具备跨季度多头逻辑。",
            "key_catalyst": "二季度长协冶炼加工费谈判与铜矿季报产量指引。",
            "invalidation": "全球制造业PMI跌破47且矿端TC反弹至60美元以上。",
            "recommended_universes": ["commodities_base", "metals_equities", "nonexistent_pool"],
            "is_new_theme": False,
            "leaders": [
                {"symbol": "沪铜", "name": "沪铜", "leg": "long", "role": "sole_leader", "reason": "国内流动性和定价锚最强"},
                {"symbol": "CU0", "name": "别名重复应去重", "leg": "long", "role": "co_leader", "reason": "alias duplicate"},
                {"symbol": "FCX", "name": "自由港", "leg": "long", "role": "co_leader", "reason": "全球纯度最高铜矿股"},
                {"symbol": "SCCO", "name": "应截断", "leg": "long", "role": "co_leader", "reason": "over cap"},
            ],
            "evidence": [
                {
                    "fact": "铜精矿现货TC跌至负区间",
                    "level": "L2",
                    "date": "2026-11-20",
                    "source": "ICSG",
                    "url": "https://icsg.org/report",
                },
                {
                    "fact": "非法协议与反斜杠URL应被剥离",
                    "level": "L4",
                    "date": "2026-11-21",
                    "source": "Reuters",
                    "url": r"https://elsewhere.example\@official.example/path",
                },
            ],
        },
        {
            "id": "lithium_oversupply_short",
            "rank": 2,
            "title": "锂盐新产能爬坡过剩做空",
            "direction": "short",
            "horizon": "1Q~2Q",
            "conviction": "high",
            "asset_scope": "commodities",
            "primary_driver": {"dimension": "商品供需平衡与库存周期", "summary": "非洲及南美低成本锂矿持续放量。"},
            "secondary_driver": {"dimension": "宏观双核", "summary": "高利率抑制海外电动车渗透斜率。"},
            "thesis": "即使短期现货偏紧或出现阶段性大B结构，未来四个季度过剩格局未变，强现实弱预期劈叉适合空头表达。",
            "key_catalyst": "澳洲锂矿季度产销公告与仓单注册量激增。",
            "invalidation": "头部澳矿大规模宣布停产且锂盐社会库存连降6周。",
            "recommended_universes": ["commodities_chem_newenergy"],
            "is_new_theme": False,
            "leaders": [
                {"symbol": "LC0", "name": "碳酸锂", "leg": "short", "role": "sole_leader", "reason": "国内锂电定价主战场"},
            ],
            "evidence": [
                {"fact": "广期所注册仓单创阶段新高", "level": "L1", "date": "2026-11-22", "source": "GFEX", "url": ""},
                {"fact": "澳洲主要锂矿季度指引维持高产", "level": "L2", "date": "2026-11-19", "source": "Pilbara", "url": ""},
            ],
        },
        {
            "id": "cn_grid_export_new_theme",
            "rank": 3,
            "title": "电网设备出海（新挖掘主题）",
            "direction": "long",
            "horizon": "2Q~1Y",
            "conviction": "medium",
            "asset_scope": "a_shares",
            "primary_driver": {"dimension": "技术与产业迭代", "summary": "欧美变压器交期仍超两年。"},
            "secondary_driver": {"dimension": "产业政策与监管", "summary": "海外电网投资计划持续上修。"},
            "thesis": "出口高毛利订单结构改善驱动业绩持续上修，现有标的池未覆盖该细分方向。",
            "key_catalyst": "海关变压器月度出口同比数据。",
            "invalidation": "欧美加征针对性高关税或出口订单增速转负。",
            "recommended_universes": [],
            "is_new_theme": True,
            "suggested_pool_name": "commodities_base",  # Collides with existing pool -> must auto-suffix
            "suggested_pool_description": "A股电网设备出海真龙头",
            "leaders": [
                {"symbol": "002028.SZ", "name": "思源电气", "leg": "long", "role": "sole_leader", "reason": "海外高压开关与变压器龙头"},
            ],
            "evidence": [
                {"fact": "海关变压器出口金额同比增长32%", "level": "L1", "date": "2026-11-18", "source": "海关总署", "url": ""},
                {"fact": "北美电网扩建资本开支上调18%", "level": "L2", "date": "2026-11-15", "source": "EIA", "url": ""},
            ],
        },
    ],
    "risk_warnings": ["若美元指数突破108，有色金属短期将承受流动性折价。"],
}


def test_normalize_compass_output_enforces_hygiene_and_alias_dedup():
    valid_pools = {"commodities_base", "metals_equities", "commodities_chem_newenergy"}
    norm = macro_compass.normalize_compass_output(SAMPLE_AI_JSON, valid_universes=valid_pools)

    assert norm["regime_title"] == SAMPLE_AI_JSON["regime_title"]
    assert len(norm["directions"]) == 3

    d1 = norm["directions"][0]
    assert d1["direction"] == "long"
    # Invalid pool name stripped
    assert d1["recommended_universes"] == ["commodities_base", "metals_equities"]
    # Alias deduped ('沪铜' + 'CU0' -> single 'CU0') and capped at 2
    assert [leader["symbol"] for leader in d1["leaders"]] == ["CU0", "FCX"]
    # Unsafe backslash/userinfo URL stripped, safe https preserved, verification_status tagged
    assert d1["evidence"][0]["url"] == "https://icsg.org/report"
    assert d1["evidence"][0]["verification_status"] == "ai_cited_unverified"
    assert d1["evidence"][1]["url"] == ""

    d2 = norm["directions"][1]
    assert d2["direction"] == "short"
    assert d2["leaders"][0]["leg"] == "short"

    d3 = norm["directions"][2]
    assert d3["is_new_theme"] is True
    # Colliding suggested pool name MUST be auto-suffixed to avoid overwriting existing pool
    assert d3["suggested_pool_name"] != "commodities_base"
    assert d3["suggested_pool_name"].startswith("commodities_base_theme")


def test_normalize_compass_rejects_equity_shorts_single_sided_pairs_bad_drivers_and_sparse_evidence():
    bad_payload = {
        "regime_title": "测试违规过滤",
        "regime_summary": "包含多张违规卡片，应全部被拒。",
        "directions": [
            # 1. Equity short legs inside a pair card (should fail pair check because short leg is stripped)
            {
                "id": "bad_equity_pair",
                "title": "违规股票做空配对",
                "direction": "pair",
                "asset_scope": "us_equities",
                "primary_driver": {"dimension": "宏观双核", "summary": "高利率。"},
                "secondary_driver": {"dimension": "技术与产业迭代", "summary": "AI分化。"},
                "thesis": "买强空弱美股。",
                "leaders": [
                    {"symbol": "NVDA", "name": "英伟达", "leg": "long", "role": "sole_leader", "reason": "多"},
                    {"symbol": "INTC", "name": "英特尔", "leg": "short", "role": "sole_leader", "reason": "空股票禁止"},
                ],
                "evidence": [
                    {"fact": "事实1", "level": "L1", "date": "2026-11-20", "source": "SEC"},
                    {"fact": "事实2", "level": "L2", "date": "2026-11-21", "source": "公司指引"},
                ],
            },
            # 2. Duplicate dimensions + empty summary
            {
                "id": "bad_dup_dim",
                "title": "重复维度卡片",
                "direction": "long",
                "asset_scope": "commodities",
                "primary_driver": {"dimension": "宏观双核", "summary": ""},
                "secondary_driver": {"dimension": "宏观双核", "summary": "重复"},
                "thesis": "单维充数。",
                "leaders": [{"symbol": "GC=F", "name": "黄金", "leg": "long", "role": "sole_leader", "reason": "龙头"}],
                "evidence": [
                    {"fact": "事实1", "level": "L1", "date": "2026-11-20", "source": "FRED"},
                    {"fact": "事实2", "level": "L2", "date": "2026-11-21", "source": "WGC"},
                ],
            },
            # 3. Empty / insufficient evidence
            {
                "id": "bad_no_ev",
                "title": "无证据卡片",
                "direction": "short",
                "asset_scope": "commodities",
                "primary_driver": {"dimension": "商品供需平衡与库存周期", "summary": "过剩。"},
                "secondary_driver": {"dimension": "宏观双核", "summary": "需求弱。"},
                "thesis": "无证据空头。",
                "leaders": [{"symbol": "RB0", "name": "螺纹", "leg": "short", "role": "sole_leader", "reason": "龙头"}],
                "evidence": [],
            },
        ],
    }
    with pytest.raises(ValueError, match="未通过纪律校验"):
        macro_compass.normalize_compass_output(bad_payload, valid_universes=set())


def test_url_validator_rejects_backslash_and_userinfo():
    assert research_archive.is_valid_http_url("https://icsg.org/report?id=1") is True
    assert research_archive.is_valid_http_url(r"https://elsewhere.example\@official.example/path") is False
    assert research_archive.is_valid_http_url("https://user:pass@official.example/path") is False
    assert research_archive.is_valid_http_url("https://official.example/foo\\bar") is False


def test_fetch_cross_asset_anchors_strict_windows_and_us10y_provenance():
    # Build 2-bar MultiIndex frame (100 -> 110): 5D/20D/60D must ALL be None, never +10%!
    idx = pd.date_range("2026-11-23", periods=2, freq="D")
    cols = pd.MultiIndex.from_tuples([("SPY", "Close"), ("GC=F", "Close")])
    df_short = pd.DataFrame([[100.0, 2000.0], [110.0, 2200.0]], index=idx, columns=cols)

    with patch.object(macro_compass.yf, "download", return_value=df_short):
        anc = macro_compass.fetch_cross_asset_anchors()
    spy_item = next(it for it in anc["items"] if it["symbol"] == "SPY")
    assert spy_item["status"] == "ok"
    assert spy_item["last"] == 110.0
    assert spy_item["ret_5d_pct"] is None
    assert spy_item["ret_20d_pct"] is None
    assert spy_item["ret_60d_pct"] is None

    # US10Y provenance: live yfinance value MUST NOT borrow FRED's stale date
    metrics, _ = macro_compass._build_headline_metrics_and_tags(
        live_ctx={"US10Y_Yield": {"last": 4.52, "chg_1d_pct": 0.4, "as_of": "2026-11-25"}},
        fred_macro={
            "status": "ok",
            "series": {
                "DGS10": {"status": "ok", "latest_value": 4.40, "change": 0.01, "latest_date": "2026-11-20"},
            },
        },
        china_macro={"status": "error"},
    )
    us10y = next(m for m in metrics if m["key"] == "US10Y")
    assert us10y["source"] == "yfinance"
    assert us10y["value"] == 4.52
    assert us10y["as_of"] == "2026-11-25"


def test_collect_macro_overview_nonblocking_timeout_stale_fallback_and_coalescing():
    CACHE.clear()
    macro_compass.clear_macro_source_cache()

    fake_live = {"VIX": {"last": 26.3, "chg_1d_pct": -1.2, "as_of": "2026-11-25"}}
    fake_fred = {
        "status": "ok",
        "series": {"DFII10": {"status": "ok", "latest_value": 2.15, "change": 0.03, "latest_date": "2026-11-24"}},
    }
    fake_cn = {"status": "ok", "pmi": {"status": "ok", "records": [{"月份": "2026年10月份", "制造业-指数": 49.5}]}}
    fake_anc = {"status": "ok", "ok_count": 1, "total": 1, "items": []}

    # Seed last-known-good first
    with (
        patch.object(macro_compass.market_snapshot, "fetch_macro_context", return_value=fake_live),
        patch.object(macro_compass.research_data, "fetch_fred_macro", return_value=fake_fred),
        patch.object(macro_compass.research_data, "fetch_china_macro_core", return_value=fake_cn),
        patch.object(macro_compass, "fetch_cross_asset_anchors", return_value=fake_anc),
    ):
        good = macro_compass.collect_macro_overview(refresh=True)
        assert good["sources_status"]["cn_macro"] == "ok"

    # Now make cn_macro sleep 0.35s while overall deadline is 0.06s -> must return promptly & mark stale!
    call_counter = {"live": 0}

    def slow_cn():
        time.sleep(0.35)
        return fake_cn

    def tracked_live():
        call_counter["live"] += 1
        time.sleep(0.03)
        return fake_live

    CACHE.clear()
    with (
        patch.object(macro_compass.market_snapshot, "fetch_macro_context", side_effect=tracked_live),
        patch.object(macro_compass.research_data, "fetch_fred_macro", return_value=fake_fred),
        patch.object(macro_compass.research_data, "fetch_china_macro_core", side_effect=slow_cn),
        patch.object(macro_compass, "fetch_cross_asset_anchors", return_value=fake_anc),
    ):
        t0 = time.monotonic()
        # Concurrent callers must coalesce into ONE upstream batch
        results: list[dict] = []
        threads = [
            threading.Thread(
                target=lambda: results.append(macro_compass.collect_macro_overview(refresh=False, deadline_seconds=0.08))
            )
            for _ in range(3)
        ]
        for th in threads:
            th.start()
        for th in threads:
            th.join(timeout=2.0)
        elapsed = time.monotonic() - t0

    assert elapsed < 0.28, f"Timed-out slow source blocked pool exit ({elapsed:.2f}s)"
    assert call_counter["live"] == 1, "Concurrent cold-cache requests should coalesce into 1 upstream fetch"
    assert len(results) == 3
    assert results[0]["sources_status"]["cn_macro"] == "stale"
    assert "cn_macro" in results[0]["stale_sources"]


def test_cli_nonblocking_timeout_on_partial_line_without_newline(tmp_path: Path):
    # Subprocess writes a partial line WITHOUT newline and sleeps 1.0s; timeout=0.12s must fire promptly!
    script = tmp_path / "fake_claude.sh"
    script.write_text(
        f"#!/usr/bin/env bash\n{sys.executable} -c 'import sys, time; sys.stdout.write(\"partial-no-newline\"); sys.stdout.flush(); time.sleep(1.0)'\n",
        encoding="utf-8",
    )
    script.chmod(0o755)

    t0 = time.monotonic()
    with (
        patch.object(deep_research, "resolve_engine_binary", return_value=str(script)),
        pytest.raises(TimeoutError),
    ):
        deep_research._run_agent_cli(
            engine="claude",
            prompt="hello",
            cwd=str(tmp_path),
            effort="low",
            emit=lambda _t, _d: None,
            stage="agent_synthesize",
            timeout_seconds=0.12,
        )
    elapsed = time.monotonic() - t0
    assert elapsed < 0.55, f"Partial-line stdout blocked CLI timeout ({elapsed:.2f}s)"


def test_cancel_vs_persist_atomic_boundary_blocks_split_state():
    CACHE.clear()
    macro_compass.clear_fallback_runs()
    app = app_mod.create_app()

    entered_cli = threading.Event()
    release_cli = threading.Event()

    def paused_cli(**kwargs):
        entered_cli.set()
        release_cli.wait(timeout=3.0)
        return json.dumps(SAMPLE_AI_JSON, ensure_ascii=False), {"engine": "claude", "elapsed_s": 0.1}

    fake_macro = {
        "fetched_at_utc": "2026-11-25T08:00:00+00:00",
        "cache_hit": False,
        "sources_status": {"live_macro": "ok"},
        "regime_tags": [],
        "headline_metrics": [],
        "anchors": {"status": "ok", "items": []},
    }

    with (
        patch.object(deep_research, "detect_available_engines", return_value={"default_engine": "claude", "any_available": True, "engines": []}),
        patch.object(deep_research, "resolve_engine_binary", return_value="/usr/local/bin/claude"),
        patch.object(macro_compass, "collect_macro_overview", return_value=fake_macro),
        patch.object(deep_research, "_run_agent_cli", side_effect=paused_cli),
        TestClient(app) as client,
    ):
        r1 = client.post("/api/tasks/macro-compass", json={"engine": "claude"})
        assert r1.status_code == 202
        tid = r1.json()["id"]
        assert entered_cli.wait(timeout=2.0)

        # Cancel while worker is mid-flight
        rc = client.post(f"/api/tasks/{tid}/cancel")
        assert rc.status_code == 200
        assert rc.json()["status"] == "cancelled"

        # Starting a second run while cancelled worker hasn't exited MUST return 409 (no overlapping worker)
        r_dup = client.post("/api/tasks/macro-compass", json={"engine": "claude"})
        assert r_dup.status_code == 409

        # Release paused worker -> commit boundary MUST reject persistence!
        release_cli.set()
        time.sleep(0.2)

        r_latest = client.get("/api/compass/latest")
        assert r_latest.status_code == 200
        assert r_latest.json()["latest"] is None
        assert r_latest.json()["history"] == []


def test_universe_create_only_returns_409_on_collision(tmp_path: Path):
    univ_file = tmp_path / "universes.json"
    univ_file.write_text(
        json.dumps({"commodities_base": {"description": "existing", "symbols": ["CU0"]}}),
        encoding="utf-8",
    )
    app = app_mod.create_app()
    orig_save = scan_opportunities.save_universe

    def bound_save(name, symbols, description="", *, allow_overwrite=True):
        return orig_save(name, symbols, description=description, universes_path=str(univ_file), allow_overwrite=allow_overwrite)

    with patch.object(scan_opportunities, "save_universe", side_effect=bound_save), TestClient(app) as client:
        r_409 = client.post(
            "/api/universes",
            json={"name": "commodities_base", "description": "clobber", "symbols": ["NVDA"], "overwrite": False},
        )
        assert r_409.status_code == 409

        # Explicit overwrite succeeds
        r_ok = client.post(
            "/api/universes",
            json={"name": "commodities_base", "description": "updated", "symbols": ["CU0", "AL0"], "overwrite": True},
        )
        assert r_ok.status_code == 200
        assert r_ok.json()["symbols"] == ["CU0", "AL0"]


def test_sqlite_compass_persistence_and_scan_compass_context_roundtrip(tmp_path: Path):
    db_file = tmp_path / "desk_test.db"
    store = data_store.DeskDataStore(str(db_file))
    norm = macro_compass.normalize_compass_output(
        SAMPLE_AI_JSON,
        valid_universes={"commodities_base", "metals_equities", "commodities_chem_newenergy"},
    )
    rid = store.record_compass_run(
        run_id="cmp_run_001",
        task_id="task_001",
        engine="claude",
        effort="medium",
        focus_prompt="侧重商品多空",
        result=norm,
    )
    assert rid == "cmp_run_001"
    latest = store.get_latest_compass_run()
    assert latest is not None and latest["directions_count"] == 3

    # Verify scan_runs preserves compass_context for Layer 2 directional alignment replay
    ctx = {
        "run_id": "cmp_run_001",
        "direction_id": "lithium_oversupply_short",
        "direction_title": "锂盐新产能爬坡过剩做空",
        "direction": "short",
        "expected_directions": {"LC0": "short"},
    }
    store.record_scan_run(
        run_id="scan_001",
        task_id="scan_001",
        universes=[],
        symbols=["LC0"],
        rows=[{"market": "adhoc", "symbol": "LC0", "close": 75000.0}],
        compass_context=ctx,
    )
    loaded_scan = store.get_scan_run("scan_001")
    assert loaded_scan is not None
    assert loaded_scan["compass_context"] == ctx


def test_compass_api_end_to_end_task_and_history():
    CACHE.clear()
    macro_compass.clear_fallback_runs()
    app = app_mod.create_app()
    with TestClient(app) as client:
        r_init = client.get("/api/compass/latest")
        assert r_init.status_code == 200
        assert r_init.json()["latest"] is None

        with (
            patch.object(deep_research, "detect_available_engines", return_value={"default_engine": None, "any_available": False, "engines": []}),
            patch.object(deep_research, "resolve_engine_binary", return_value=None),
        ):
            r_503 = client.post("/api/tasks/macro-compass", json={"engine": "claude"})
            assert r_503.status_code == 503

        fake_macro = {
            "fetched_at_utc": "2026-11-25T08:00:00+00:00",
            "cache_hit": False,
            "sources_status": {"live_macro": "ok"},
            "regime_tags": [{"label": "实际利率偏紧 (2.15%)", "tone": "warning"}],
            "headline_metrics": [],
            "anchors": {"status": "ok", "items": []},
        }

        def fake_agent_cli(**kwargs):
            emit = kwargs["emit"]
            emit("log", {"stage": "agent_synthesize", "message": "联网检索：global copper supply 2026"})
            return json.dumps(SAMPLE_AI_JSON, ensure_ascii=False), {"engine": "claude", "elapsed_s": 1.2}

        with (
            patch.object(deep_research, "detect_available_engines", return_value={"default_engine": "claude", "any_available": True, "engines": []}),
            patch.object(deep_research, "resolve_engine_binary", return_value="/usr/local/bin/claude"),
            patch.object(macro_compass, "collect_macro_overview", return_value=fake_macro),
            patch.object(deep_research, "_run_agent_cli", side_effect=fake_agent_cli),
        ):
            r_start = client.post(
                "/api/tasks/macro-compass",
                json={"engine": "claude", "effort": "medium", "focus_prompt": "重点商品多空"},
            )
            assert r_start.status_code == 202
            task_id = r_start.json()["id"]

            deadline = time.monotonic() + 5.0
            detail = None
            while time.monotonic() < deadline:
                r_t = client.get(f"/api/tasks/{task_id}")
                assert r_t.status_code == 200
                detail = r_t.json()
                if detail["status"] in ("completed", "failed", "cancelled"):
                    break
                time.sleep(0.05)

            assert detail is not None
            assert detail["status"] == "completed", f"Unexpected status: {detail}"

        r_latest = client.get("/api/compass/latest")
        assert r_latest.status_code == 200
        body = r_latest.json()
        assert body["latest"] is not None
        assert body["latest"]["run_id"] == task_id
        assert len(body["latest"]["result"]["directions"]) == 3
        assert len(body["history"]) == 1
