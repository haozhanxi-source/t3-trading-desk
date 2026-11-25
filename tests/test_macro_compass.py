"""Tests for Layer 1 Top-Down Strategic Compass (macro_compass, SQLite persistence & FastAPI routes)."""
from __future__ import annotations

import json
import time
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from backend import app as app_mod
from backend import deep_research, macro_compass
from backend.cache import CACHE
import data_store


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
                {"symbol": "cu0", "name": "沪铜", "leg": "long", "role": "sole_leader", "reason": "国内流动性和定价锚最强"},
                {"symbol": "CU0", "name": "重复应去重", "leg": "long", "role": "co_leader", "reason": "duplicate"},
                {"symbol": "FCX", "name": "自由港", "leg": "long", "role": "co_leader", "reason": "全球纯度最高铜矿股"},
                {"symbol": "EXTRA", "name": "应截断", "leg": "long", "role": "co_leader", "reason": "over cap"},
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
                    "fact": "非法协议URL应被剥离",
                    "level": "L4",
                    "date": "2026-11-21",
                    "source": "bad",
                    "url": "javascript:alert(1)",
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
            "suggested_pool_name": "CN Grid Export!",
            "suggested_pool_description": "A股电网设备出海真龙头",
            "leaders": [
                {"symbol": "002028.SZ", "name": "思源电气", "leg": "long", "role": "sole_leader", "reason": "海外高压开关与变压器龙头"},
            ],
            "evidence": [
                {"fact": "海关变压器出口金额同比增长32%", "level": "L1", "date": "2026-11-18", "source": "海关总署", "url": ""},
            ],
        },
    ],
    "risk_warnings": ["若美元指数突破108，有色金属短期将承受流动性折价。"],
}


def test_normalize_compass_output_enforces_hygiene():
    valid_pools = {"commodities_base", "metals_equities", "commodities_chem_newenergy"}
    norm = macro_compass.normalize_compass_output(SAMPLE_AI_JSON, valid_universes=valid_pools)

    assert norm["regime_title"] == SAMPLE_AI_JSON["regime_title"]
    assert len(norm["directions"]) == 3

    d1 = norm["directions"][0]
    assert d1["direction"] == "long"
    # Invalid pool name stripped
    assert d1["recommended_universes"] == ["commodities_base", "metals_equities"]
    # Leader deduped + capped at 2 for non-pair direction
    assert [leader["symbol"] for leader in d1["leaders"]] == ["CU0", "FCX"]
    # Unsafe URL stripped, safe https preserved
    assert d1["evidence"][0]["url"] == "https://icsg.org/report"
    assert d1["evidence"][1]["url"] == ""

    d2 = norm["directions"][1]
    assert d2["direction"] == "short"
    assert d2["leaders"][0]["leg"] == "short"

    d3 = norm["directions"][2]
    assert d3["is_new_theme"] is True
    assert d3["suggested_pool_name"] == "cn_grid_export"


def test_normalize_compass_output_rejects_empty():
    with pytest.raises(ValueError):
        macro_compass.normalize_compass_output({"regime_title": "", "directions": []}, valid_universes=set())
    with pytest.raises(ValueError):
        macro_compass.normalize_compass_output(
            {"regime_title": "ok", "regime_summary": "ok", "directions": []},
            valid_universes=set(),
        )


def test_sqlite_compass_persistence_roundtrip(tmp_path: Path):
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
    assert latest is not None
    assert latest["run_id"] == "cmp_run_001"
    assert latest["directions_count"] == 3
    assert latest["result"]["directions"][0]["title"] == "全球铜矿供给收缩与电力需求共振"

    rows = store.list_compass_runs(limit=10)
    assert len(rows) == 1
    assert rows[0]["focus_prompt"] == "侧重商品多空"

    detail = store.get_compass_run("cmp_run_001")
    assert detail is not None
    assert detail["engine"] == "claude"
    assert store.get_compass_run("missing_id") is None


def test_collect_macro_overview_isolates_failures_and_caches():
    CACHE.clear()
    fake_live = {
        "VIX": {"last": 26.3, "chg_1d_pct": -1.2},
        "US10Y_Yield": {"last": 4.52, "chg_1d_pct": 0.4},
        "DXY_USD_Index": {"last": 104.2, "chg_1d_pct": 0.1},
    }
    fake_fred = {
        "status": "ok",
        "series": {
            "DFII10": {"status": "ok", "latest_value": 2.15, "change": 0.03, "latest_date": "2026-11-24"},
            "T10YIE": {"status": "ok", "latest_value": 2.37, "change": -0.01, "latest_date": "2026-11-24"},
            "FEDFUNDS": {"status": "ok", "latest_value": 4.5, "change": 0.0, "latest_date": "2026-11-01"},
            "PCOPPUSDM": {"status": "ok", "latest_value": 9450.0, "change": 120.0, "latest_date": "2026-10-01"},
        },
    }
    fake_anchors = {
        "status": "ok",
        "ok_count": 2,
        "total": 2,
        "items": [
            {"symbol": "SPY", "name": "标普500", "group": "us_equities", "group_label": "美股行业锚", "status": "ok", "last": 600.0, "ret_5d_pct": 1.1, "ret_20d_pct": 3.4, "ret_60d_pct": 8.2, "as_of": "2026-11-24"},
        ],
    }

    with (
        patch.object(macro_compass.market_snapshot, "fetch_macro_context", return_value=fake_live),
        patch.object(macro_compass.research_data, "fetch_fred_macro", return_value=fake_fred),
        patch.object(macro_compass.research_data, "fetch_china_macro_core", side_effect=RuntimeError("akshare timeout")),
        patch.object(macro_compass, "fetch_cross_asset_anchors", return_value=fake_anchors),
    ):
        first = macro_compass.collect_macro_overview(refresh=True)
        assert first["cache_hit"] is False
        assert first["sources_status"]["us_fred"] == "ok"
        assert first["sources_status"]["cn_macro"] == "error"
        assert any("VIX高波避险" in t["label"] for t in first["regime_tags"])
        assert any("实际利率偏紧" in t["label"] for t in first["regime_tags"])

        second = macro_compass.collect_macro_overview(refresh=False)
        assert second["cache_hit"] is True


def test_compass_api_end_to_end_task_and_history():
    CACHE.clear()
    macro_compass.clear_fallback_runs()
    app = app_mod.create_app()
    with TestClient(app) as client:
        # Initially empty
        r_init = client.get("/api/compass/latest")
        assert r_init.status_code == 200
        assert r_init.json()["latest"] is None

        # 503 when no engine CLI available
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
            r_macro = client.get("/api/compass/macro-overview")
            assert r_macro.status_code == 200
            assert r_macro.json()["sources_status"]["live_macro"] == "ok"

            r_start = client.post(
                "/api/tasks/macro-compass",
                json={"engine": "claude", "effort": "medium", "focus_prompt": "重点商品多空"},
            )
            assert r_start.status_code == 202
            task_id = r_start.json()["id"]

            # Poll task completion
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

        # Verify persisted latest & history endpoints
        r_latest = client.get("/api/compass/latest")
        assert r_latest.status_code == 200
        body = r_latest.json()
        assert body["latest"] is not None
        assert body["latest"]["run_id"] == task_id
        assert len(body["latest"]["result"]["directions"]) == 3
        assert len(body["history"]) == 1

        r_hist = client.get(f"/api/compass/history/{task_id}")
        assert r_hist.status_code == 200
        assert r_hist.json()["result"]["regime_title"] == SAMPLE_AI_JSON["regime_title"]
