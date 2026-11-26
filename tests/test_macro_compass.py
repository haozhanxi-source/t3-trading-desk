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
import research_extensions
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
        {
            "sector": "传统地产建材链",
            "reason": "竣工端需求缺乏弹性，中枢持续下移。",
            "derived_strategy": "股票仅规避，可通过黑色系期货（铁矿/螺纹/玻璃）或商品货币澳元表达空头。",
            "short_proxy_possible": True,
            "short_proxy_hint": "黑色系期货 / 澳元空头代理",
        },
        {
            "sector": "同质化光伏组件",
            "reason": "产能出清未完成，价格战压制盈利。",
            "derived_strategy": "股票仅规避，上游多晶硅期货可关注过剩空头机会。",
            "short_proxy_possible": True,
            "short_proxy_hint": "多晶硅期货空头",
        },
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
    assert norm["avoid_sectors"][0]["short_proxy_possible"] is True
    assert "黑色系期货" in norm["avoid_sectors"][0]["derived_strategy"]
    assert norm["avoid_sectors"][0]["short_proxy_hint"] == "黑色系期货 / 澳元空头代理"

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


def test_normalize_compass_allows_us_equity_shorts_but_rejects_a_share_shorts_bad_drivers_and_sparse_evidence():
    # US equity / ETF short in Davis double-kill MUST now pass Option A validation
    us_short_payload = {
        "regime_title": "高利率紧缩与 AI 估值双杀",
        "regime_summary": "实际利率高企叠加算力资本开支延宕，美股高估值芯片板块允许顺势做空。",
        "avoid_sectors": [
            {
                "sector": "中国地产链 A 股",
                "reason": "销售与投资延续下行",
                "derived_strategy": "A 股严禁做空，改用铁矿/澳元代理",
                "short_proxy_possible": True,
                "short_proxy_hint": "空A股地产龙头",  # must be stripped
            }
        ],
        "directions": [
            {
                "id": "us_ai_semis_short",
                "rank": 1,
                "title": "美国高估值 AI 算力芯片做空",
                "direction": "short",
                "asset_scope": "us_equities",
                "primary_driver": {"dimension": "宏观双核", "summary": "实际利率维持高位压制久期估值。"},
                "secondary_driver": {"dimension": "技术与产业迭代", "summary": "数据中心审批延宕引发 CapEx 下修。"},
                "thesis": "盈利与估值同步下修形成戴维斯双杀，美股芯片 ETF 可顺势做空。",
                "priced_in_already": "12 月美联储加息与近 20 日芯片板块回调已部分计价。",
                "unpriced_gap": "云厂 2027 年 CapEx 实际下修幅度与 BIS 宽限期到期冲击尚未充分定价。",
                "value_chain_focus": "优先挑选估值最高、CapEx 依赖度最重的芯片 ETF 或高弹性算力链。",
                "reference_anchors": ["SMH"],
                "evidence": [
                    {"fact": "SMH 近 20 日下跌 23.5%", "level": "L1", "date": "2026-11-24", "source": "yfinance"},
                    {"fact": "多州收紧数据中心并网审批", "level": "L2", "date": "2026-11-20", "source": "Reuters"},
                ],
            },
            {
                "id": "us_utilities_vs_semis_pair",
                "rank": 2,
                "title": "多美国电网公用空半导体配对",
                "direction": "pair",
                "asset_scope": "us_equities",
                "primary_driver": {"dimension": "技术与产业迭代", "summary": "电网瓶颈确定性高于芯片扩张。"},
                "secondary_driver": {"dimension": "资金流与市场结构", "summary": "资金从高估值算力切换至防御公用事业。"},
                "thesis": "电网公用事业独立走强而半导体深陷双杀，构成同市场多空配对。",
                "long_leg_sector": "美国受监管电网公用事业",
                "short_leg_sector": "美国高估值半导体板块",
                "evidence": [
                    {"fact": "PJM 批发平均电价同比上涨 40.9%", "level": "L1", "date": "2026-11-20", "source": "PJM"},
                    {"fact": "半导体 ETF 资金净流出扩大", "level": "L2", "date": "2026-11-22", "source": "Bloomberg"},
                ],
            },
        ],
    }
    norm_us = macro_compass.normalize_compass_output(us_short_payload, valid_universes={"us_semis_ai", "us_power_grid"})
    assert len(norm_us["directions"]) == 2
    assert norm_us["directions"][0]["direction"] == "short"
    assert norm_us["directions"][0]["reference_anchors"] == ["SMH"]
    assert "12 月美联储加息" in norm_us["directions"][0]["priced_in_already"]
    assert "CapEx 实际下修" in norm_us["directions"][0]["unpriced_gap"]
    assert norm_us["directions"][1]["direction"] == "pair"
    assert norm_us["avoid_sectors"][0]["short_proxy_hint"] == ""

    # A-share shorts, A-share pairs, duplicate dimensions & sparse evidence MUST still be rejected
    bad_payload = {
        "regime_title": "测试违规过滤",
        "regime_summary": "包含多张违规卡片，应全部被拒。",
        "directions": [
            # 1. A-share pure short forbidden
            {
                "id": "bad_a_share_short",
                "title": "违规做空 A 股",
                "direction": "short",
                "asset_scope": "a_shares",
                "primary_driver": {"dimension": "宏观双核", "summary": "内需偏弱。"},
                "secondary_driver": {"dimension": "资金流与市场结构", "summary": "流出。"},
                "thesis": "违规做空 A 股。",
                "reference_anchors": ["600519.SS"],
                "evidence": [
                    {"fact": "事实1", "level": "L1", "date": "2026-11-20", "source": "NBS"},
                    {"fact": "事实2", "level": "L2", "date": "2026-11-21", "source": "交易所"},
                ],
            },
            # 2. A-share short leg inside a pair card (should fail because A-share short leg is stripped)
            {
                "id": "bad_a_share_pair",
                "title": "违规 A 股空头腿配对",
                "direction": "pair",
                "asset_scope": "cross_asset",
                "primary_driver": {"dimension": "宏观双核", "summary": "分化。"},
                "secondary_driver": {"dimension": "产业政策与监管", "summary": "政策。"},
                "thesis": "多商品空 A 股。",
                "leaders": [
                    {"symbol": "GC=F", "name": "黄金", "leg": "long", "role": "sole_leader", "reason": "多"},
                    {"symbol": "600519.SS", "name": "茅台", "leg": "short", "role": "sole_leader", "reason": "空A股禁止"},
                ],
                "evidence": [
                    {"fact": "事实1", "level": "L1", "date": "2026-11-20", "source": "交易所"},
                    {"fact": "事实2", "level": "L2", "date": "2026-11-21", "source": "协会"},
                ],
            },
            # 3. Duplicate dimensions + empty summary
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
            # 4. Empty / insufficient evidence
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


def test_blind_brief_and_prompt_contain_zero_universe_or_portfolio_anchoring():
    brief = macro_compass.build_compass_brief(
        macro_overview={"fetched_at_utc": "2026-11-25T08:00:00+00:00", "headline_metrics": []},
        focus_prompt="",
    )
    assert "existing_universes" not in brief
    assert "portfolio_context" not in brief

    prompt = macro_compass.build_compass_prompt(brief_path="/tmp/brief.json", focus_prompt="")
    for anchor_token in ("existing_universes", "NI0", "AUDUSD", "RKAB", "霍尔木兹", "GEV", "XLU", "PWR"):
        assert anchor_token not in prompt, f"Prompt should not anchor AI with {anchor_token!r}"
    assert "不代用户硬选具体个股" in prompt
    assert "滞后 1~2 个月" in prompt
    assert "priced_in_already" in prompt
    assert "unpriced_gap" in prompt
    assert "允许 4~5 张方向卡全是纯空头" not in prompt

    # Multi-select focus_scopes: deduped, unknown dropped, injected into prompt
    clean_scopes = macro_compass.normalize_focus_scopes(
        ["commodities_metals", "fx_rates", "commodities_metals", "bogus_scope"]
    )
    assert clean_scopes == ["commodities_metals", "fx_rates"]
    scoped_prompt = macro_compass.build_compass_prompt(
        brief_path="/tmp/brief.json",
        focus_scopes=clean_scopes,
        focus_prompt="重点看商品货币与铜铝分化",
    )
    assert "用户锁定的重点深挖战场（多选" in scoped_prompt
    assert "有色与贵金属" in scoped_prompt
    assert "外汇与利率周期" in scoped_prompt
    assert macro_compass.format_focus_scopes_summary(clean_scopes, "测试补充") == "专挖：有色与贵金属 + 外汇与利率周期 ｜ 测试补充"


def test_sector_first_cards_without_leaders_and_posthoc_universe_portfolio_mapping():
    universes = {
        "commodities_base": {"description": "有色金属", "symbols": ["CU0", "AL0", "HG=F"]},
        "commodities_energy": {"description": "原油能源", "symbols": ["CL=F", "SC0"]},
        "fx_majors": {"description": "主流外汇", "symbols": ["USDJPY=X", "AUDUSD=X"]},
    }
    open_positions = [
        {"symbol": "CU0", "direction": "long", "sector_theme": "铜矿供给收缩"},
    ]
    sector_only_payload = {
        "regime_title": "高真实利率分化：硬供给约束板块占优",
        "regime_summary": "美债高利率压制长久期泡沫，供给硬约束资源与过剩周期品形成多空分化。",
        "directions": [
            {
                "id": "global_copper_supply",
                "rank": 1,
                "title": "全球铜与基本金属供给约束板块",
                "direction": "long",
                "horizon": "2Q~1Y",
                "conviction": "high",
                "asset_scope": "commodities",
                "primary_driver": {"dimension": "地缘与资源国政策", "summary": "主产国供给扰动频发。"},
                "secondary_driver": {"dimension": "商品供需平衡与库存周期", "summary": "全球显性库存低位。"},
                "thesis": "未来四个季度新矿投产缺口持续。",
                "value_chain_focus": "优先关注上游矿端自给率高的品种与成本曲线左端标的。",
                "sector_keywords": ["铜", "基本金属", "copper"],
                "reference_anchors": ["HG=F"],
                "key_catalyst": "季度产销报告。",
                "invalidation": "显性库存连涨八周。",
                "evidence": [
                    {"fact": "矿端冶炼加工费维持低位", "level": "L2", "date": "2026-11-20", "source": "ICSG"},
                    {"fact": "主要产区产量指引下调", "level": "L1", "date": "2026-11-22", "source": "矿业季报"},
                ],
            },
            {
                "id": "energy_pair",
                "rank": 2,
                "title": "能源产业链多强空弱配对",
                "direction": "pair",
                "horizon": "1Q~2Q",
                "conviction": "medium",
                "asset_scope": "commodities",
                "primary_driver": {"dimension": "商品供需平衡与库存周期", "summary": "成品油裂解偏紧而上游过剩。"},
                "secondary_driver": {"dimension": "宏观双核", "summary": "全球工业增速分化。"},
                "thesis": "成品油库存低位与原油过剩形成跨品种套利机会。",
                "value_chain_focus": "多裂解紧缺端、空过剩原油基准。",
                "long_leg_sector": "海外成品油与柴油裂解端",
                "short_leg_sector": "全球过剩原油期货基准",
                "sector_keywords": ["原油", "柴油", "能源"],
                "key_catalyst": "周度库存报告。",
                "invalidation": "产油国宣布超预期联合减产。",
                "evidence": [
                    {"fact": "馏分油库存低于五年均值", "level": "L1", "date": "2026-11-20", "source": "EIA"},
                    {"fact": "非OPEC原油产量继续爬坡", "level": "L1", "date": "2026-11-18", "source": "IEA"},
                ],
            },
            {
                "id": "biotech_glp1_new",
                "rank": 3,
                "title": "新一代代谢创新药出海板块",
                "direction": "long",
                "horizon": "2Q~1Y",
                "conviction": "medium",
                "asset_scope": "a_shares",
                "primary_driver": {"dimension": "技术与产业迭代", "summary": "临床数据验证代际升级。"},
                "secondary_driver": {"dimension": "产业政策与监管", "summary": "医保与海外授权加速。"},
                "thesis": "对外授权首付款兑现打开远期盈利空间。",
                "value_chain_focus": "优先选已有海外大药企 BD 授权兑现的核心创新药龙头。",
                "sector_keywords": ["代谢创新药", "GLP-1", "医药出海"],
                "key_catalyst": "核心临床三期读数。",
                "invalidation": "海外授权终止或临床不及预期。",
                "evidence": [
                    {"fact": "行业对外授权交易额创新高", "level": "L2", "date": "2026-11-15", "source": "医药行业协会"},
                    {"fact": "监管审批周期同比缩短", "level": "L1", "date": "2026-11-12", "source": "药监局"},
                ],
            },
        ],
    }

    norm = macro_compass.normalize_compass_output(
        sector_only_payload,
        valid_universes=set(universes.keys()),
        universes=universes,
        open_positions=open_positions,
    )
    assert len(norm["directions"]) == 3

    d1, d2, d3 = norm["directions"]
    assert d1["leaders"] == []
    assert d1["recommended_universes"] == ["commodities_base"]
    assert d1["is_new_theme"] is False
    assert any("CU0" in item for item in d1["portfolio_overlap"])

    assert d2["direction"] == "pair"
    assert d2["long_leg_sector"] == "海外成品油与柴油裂解端"
    assert d2["recommended_universes"] == ["commodities_energy"]

    assert d3["recommended_universes"] == []
    assert d3["is_new_theme"] is True
    assert d3["suggested_pool_name"] == "theme_biotech_glp1_new"


def test_phosphate_sulfur_chemical_does_not_false_match_rubber_chem_pool():
    universes = {
        "commodities_chem_newenergy": {
            "description": "化工与新能源金属（PTA/甲醇/PVC/纯碱/玻璃/橡胶/碳酸锂/工业硅/多晶硅）",
            "symbols": ["TA0", "MA0", "V0", "SA0", "FG0", "RU0", "LC0", "SI0", "PS0"],
        },
    }
    # Phosphate & sulfur fertilizer has nothing to do with rubber/glass/PTA/lithium -> must NOT match!
    matched_phosphate = macro_compass.map_sector_to_universes(
        title="非中东一体化磷肥与硫磺资源化工板块",
        asset_scope="cross_asset",
        keywords=["磷肥", "硫磺", "资源化工", "fertilizer", "sulfur"],
        reference_anchors=[],
        universes=universes,
    )
    assert matched_phosphate == []

    # True member of that pool (lithium / rubber) still matches cleanly
    matched_lithium = macro_compass.map_sector_to_universes(
        title="碳酸锂与新能源金属过剩做空",
        asset_scope="commodities",
        keywords=["碳酸锂", "锂盐", "过剩"],
        reference_anchors=[],
        universes=universes,
    )
    assert matched_lithium == ["commodities_chem_newenergy"]


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
    fake_cn_anc = {"status": "ok", "ok_count": 1, "total": 1, "items": []}
    fake_pm = {
        "status": "ok",
        "source": "Polymarket Gamma API",
        "fetched_at_utc": "2026-11-26T00:00:00+00:00",
        "total_events": 1,
        "categories_ok": 3,
        "categories_total": 3,
        "headline_events": [],
        "events": [],
    }

    # Seed last-known-good first
    with (
        patch.object(macro_compass.market_snapshot, "fetch_macro_context", return_value=fake_live),
        patch.object(macro_compass.research_data, "fetch_fred_macro", return_value=fake_fred),
        patch.object(macro_compass.research_data, "fetch_china_macro_core", return_value=fake_cn),
        patch.object(macro_compass, "fetch_cross_asset_anchors", return_value=fake_anc),
        patch.object(macro_compass, "fetch_cn_futures_anchors", return_value=fake_cn_anc),
        patch.object(macro_compass, "fetch_polymarket_macro_odds", return_value=fake_pm),
    ):
        good = macro_compass.collect_macro_overview(refresh=True)
        assert good["sources_status"]["cn_macro"] == "ok"
        assert good["sources_status"]["polymarket_odds"] == "ok"

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
        patch.object(macro_compass, "fetch_cn_futures_anchors", return_value=fake_cn_anc),
        patch.object(macro_compass, "fetch_polymarket_macro_odds", return_value=fake_pm),
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


def test_compute_tape_movers_and_cn_futures_sqlite_fallback():
    # 1. Broad selloff triggers "全面紧缩/普跌" breadth label & surfaces extreme losers at top
    sample_items = [
        {"symbol": "COPX", "name": "全球铜矿", "group": "us_equities", "status": "ok", "last": 40.0, "ret_5d_pct": -11.8, "ret_20d_pct": -24.9, "ret_60d_pct": -38.0, "as_of": "2026-11-24"},
        {"symbol": "SN0", "name": "沪锡", "group": "cn_futures", "status": "ok", "last": 288990.0, "ret_5d_pct": -13.9, "ret_20d_pct": -25.4, "ret_60d_pct": -32.1, "as_of": "2026-11-25"},
        {"symbol": "SMH", "name": "半导体", "group": "us_equities", "status": "ok", "last": 210.0, "ret_5d_pct": -7.3, "ret_20d_pct": -23.5, "ret_60d_pct": -23.3, "as_of": "2026-11-24"},
        {"symbol": "HG=F", "name": "COMEX铜", "group": "commodities", "status": "ok", "last": 4.1, "ret_5d_pct": -4.9, "ret_20d_pct": -18.9, "ret_60d_pct": -17.3, "as_of": "2026-11-25"},
        {"symbol": "BTC-USD", "name": "比特币", "group": "cn_fx_crypto", "status": "ok", "last": 65000.0, "ret_5d_pct": -0.8, "ret_20d_pct": -23.2, "ret_60d_pct": -30.9, "as_of": "2026-11-25"},
        {"symbol": "ALI=F", "name": "COMEX铝", "group": "commodities", "status": "ok", "last": 2400.0, "ret_5d_pct": -6.1, "ret_20d_pct": -16.4, "ret_60d_pct": -19.7, "as_of": "2026-11-25"},
        {"symbol": "XLU", "name": "电力公用", "group": "us_equities", "status": "ok", "last": 82.0, "ret_5d_pct": -0.1, "ret_20d_pct": 1.3, "ret_60d_pct": 3.5, "as_of": "2026-11-24"},
    ]
    radar = macro_compass.compute_tape_movers(sample_items)
    assert radar["regime_breadth"]["breadth_label"] == "全面紧缩/普跌（空头主战场）"
    assert radar["regime_breadth"]["severe_drawdown_count"] == 6
    assert [m["symbol"] for m in radar["top_losers"][:3]] == ["COPX", "SN0", "BTC-USD"]
    assert any(m["symbol"] == "SMH" for m in radar["top_losers"])

    # Brief includes tape_movers without anchoring
    brief = macro_compass.build_compass_brief(
        macro_overview={"fetched_at_utc": "2026-11-25T08:00:00+00:00", "anchors": {"items": sample_items}},
    )
    assert brief["tape_movers"]["regime_breadth"]["breadth_label"] == "全面紧缩/普跌（空头主战场）"
    assert len(brief["tape_movers"]["top_losers"]) == 6

    # 2. CN futures anchor degrades cleanly to cached SQLite series when live get_snapshot times out
    idx = pd.date_range("2026-08-01", periods=65, freq="B")
    cached_close = pd.Series([100.0 + i for i in range(65)], index=idx)

    def fake_get_snapshot(sym, **_kwargs):
        if sym == "CU0":
            return {"error": "Read timed out"}
        return {
            "technicals": {
                "last_close": 288990.0,
                "return_5d_pct": -13.93,
                "return_20d_pct": -25.41,
                "return_60d_pct": -32.08,
                "as_of_bar": "2026-11-25",
            }
        }

    with (
        patch.object(macro_compass.market_snapshot, "get_snapshot", side_effect=fake_get_snapshot),
        patch.object(macro_compass, "_read_cached_cn_future_close", return_value=cached_close),
    ):
        cn_res = macro_compass.fetch_cn_futures_anchors(refresh=False, timeout_seconds=5.0)
    # 5 fresh + 1 stale SQLite fallback -> partial (NEVER 'ok') so degraded TTL & freshness tracking stay honest
    assert cn_res["status"] == "partial"
    assert cn_res["ok_count"] == len(macro_compass.CN_FUTURES_ANCHORS) - 1
    assert cn_res["stale_count"] == 1
    cu_item = next(it for it in cn_res["items"] if it["symbol"] == "CU0")
    assert cu_item["status"] == "stale"
    assert cu_item["stale_fallback"] is True
    assert cu_item["ret_60d_pct"] is not None
    sn_item = next(it for it in cn_res["items"] if it["symbol"] == "SN0")
    assert sn_item["status"] == "ok"
    assert sn_item["ret_20d_pct"] == -25.41


def test_codex_review_regressions_a_share_bypasses_stale_breadth_mixed_radar_and_timeout_clamp(tmp_path: Path):
    # 1. US:600519.SS MUST still normalize as a_share (Finding 2)
    sym, ac = macro_compass.market_snapshot.normalize_symbol("US:600519.SS")
    assert sym == "600519.SS" and ac == "a_share"

    # 2. cross_asset short card with A-share reference_anchor or spaced A-share short_leg_sector MUST be rejected (Finding 2)
    bypass_payload = {
        "regime_title": "绕过测试",
        "regime_summary": "测试各种 A 股做空绕过路径。",
        "directions": [
            {
                "id": "sneaky_anchor_short",
                "title": "消费白酒估值回归",
                "direction": "short",
                "asset_scope": "cross_asset",
                "primary_driver": {"dimension": "宏观双核", "summary": "内需弱"},
                "secondary_driver": {"dimension": "资金流与市场结构", "summary": "流出"},
                "thesis": "做空白酒。",
                "reference_anchors": ["600519.SS"],
                "evidence": [
                    {"fact": "事实1", "level": "L1", "date": "2026-11-20", "source": "NBS"},
                    {"fact": "事实2", "level": "L2", "date": "2026-11-21", "source": "协会"},
                ],
            },
            {
                "id": "spaced_a_share_pair",
                "title": "多黄金空白酒配对",
                "direction": "pair",
                "asset_scope": "cross_asset",
                "primary_driver": {"dimension": "宏观双核", "summary": "分化"},
                "secondary_driver": {"dimension": "资金流与市场结构", "summary": "切换"},
                "thesis": "配对。",
                "long_leg_sector": "海外黄金期货",
                "short_leg_sector": "A 股白酒",
                "evidence": [
                    {"fact": "事实1", "level": "L1", "date": "2026-11-20", "source": "WGC"},
                    {"fact": "事实2", "level": "L2", "date": "2026-11-21", "source": "协会"},
                ],
            },
        ],
    }
    with pytest.raises(ValueError, match="未通过纪律校验"):
        macro_compass.normalize_compass_output(bypass_payload, valid_universes=set())

    # 3. avoid_sectors short_proxy_hint handles whitespace & preserves negated prohibition + valid proxy (Finding 8)
    clean_bad, bad_flag = macro_compass._sanitize_avoid_short_proxy_hint("做空 A 股白酒")
    assert clean_bad == "" and bad_flag is True
    clean_good, good_flag = macro_compass._sanitize_avoid_short_proxy_hint("避免做空A股，改空铁矿")
    assert clean_good == "避免做空A股，改空铁矿" and good_flag is False

    # 4. Scan API rejects explicit and implicit A-share short legs with 422 (Finding 2)
    app = app_mod.create_app()
    with TestClient(app) as client:
        r_imp = client.post(
            "/api/tasks/scan",
            json={
                "universes": [],
                "symbols": ["600519.SS"],
                "compass_context": {
                    "direction": "short",
                    "expected_directions": {},
                },
            },
        )
        assert r_imp.status_code == 422
        assert "A 股严禁" in r_imp.json()["detail"]

    # 5. Stale fallback items excluded from live breadth & mixed-sign mover appears in only one list with matching driver_ret_pct (Findings 6 & 7)
    mixed_items = [
        {"symbol": "MIX", "name": "混合锚", "group": "commodities", "status": "ok", "last": 100.0, "ret_5d_pct": 1.0, "ret_20d_pct": 3.0, "ret_60d_pct": -20.0, "as_of": "2026-11-25"},
        {"symbol": "OLD1", "name": "旧期货1", "group": "cn_futures", "status": "stale", "stale_fallback": True, "last": 100.0, "ret_5d_pct": -5.0, "ret_20d_pct": -25.0, "ret_60d_pct": -30.0, "as_of": "2026-10-01"},
        {"symbol": "OLD2", "name": "旧期货2", "group": "cn_futures", "status": "stale", "stale_fallback": True, "last": 100.0, "ret_5d_pct": -5.0, "ret_20d_pct": -25.0, "ret_60d_pct": -30.0, "as_of": "2026-10-01"},
        {"symbol": "OLD3", "name": "旧期货3", "group": "cn_futures", "status": "stale", "stale_fallback": True, "last": 100.0, "ret_5d_pct": -5.0, "ret_20d_pct": -25.0, "ret_60d_pct": -30.0, "as_of": "2026-10-01"},
        {"symbol": "OLD4", "name": "旧期货4", "group": "cn_futures", "status": "stale", "stale_fallback": True, "last": 100.0, "ret_5d_pct": -5.0, "ret_20d_pct": -25.0, "ret_60d_pct": -30.0, "as_of": "2026-10-01"},
        {"symbol": "OLD5", "name": "旧期货5", "group": "cn_futures", "status": "stale", "stale_fallback": True, "last": 100.0, "ret_5d_pct": -5.0, "ret_20d_pct": -25.0, "ret_60d_pct": -30.0, "as_of": "2026-10-01"},
    ]
    radar = macro_compass.compute_tape_movers(mixed_items)
    assert radar["regime_breadth"]["total_available"] == 1
    assert radar["regime_breadth"]["breadth_label"] == "结构分化（多空双向择优）"
    assert [m["symbol"] for m in radar["top_losers"]] == ["MIX"]
    assert radar["top_losers"][0]["driver_window"] == "60D"
    assert radar["top_losers"][0]["driver_ret_pct"] == -20.0
    assert radar["top_gainers"] == []

    # 6. Nested scoped_http_timeout clamps to tighter outer timeout (Finding 5)
    with data_store.scoped_http_timeout((3.0, 5.0)):
        with data_store.scoped_http_timeout((5.0, 15.0)):
            assert data_store._HTTP_TIMEOUT_VAR.get() == (3.0, 5.0)

    # 7. _read_cached_cn_future_close uses load_cached_ohlcv and reads 80-bar series synced >21d ago (Finding 4)
    db_file = tmp_path / "cu_stale.db"
    store = data_store.DeskDataStore(str(db_file))
    idx = pd.date_range("2026-07-01", periods=80, freq="B")
    df_cu = pd.DataFrame(
        {
            "Open": [100.0 + i for i in range(80)],
            "High": [101.0 + i for i in range(80)],
            "Low": [99.0 + i for i in range(80)],
            "Close": [100.0 + i for i in range(80)],
            "Volume": [1000.0] * 80,
        },
        index=idx,
    )
    cu_key = data_store.SeriesKey("cn_future", "CU0", "1d", "akshare_sina_main", "raw_continuous")
    store.get_or_sync_ohlcv(
        cu_key,
        period="3y",
        timestamp_anchor="end",
        ttl_seconds=3600,
        refresh=True,
        supports_incremental=False,
        fetch_full=lambda: df_cu,
    )
    # Age last_synced_at_utc by 21 days to prove load_cached_ohlcv still reads it
    conn = store._connect()
    try:
        conn.execute("UPDATE ohlcv_series SET last_synced_at_utc = '2026-10-01T00:00:00+00:00'")
        conn.commit()
    finally:
        conn.close()

    with (
        patch.object(macro_compass.data_store, "is_store_enabled", return_value=True),
        patch.object(macro_compass.data_store, "get_store", return_value=store),
    ):
        cs = macro_compass._read_cached_cn_future_close("CU0")
    assert cs is not None and len(cs) == 80


def test_polymarket_parser_filters_noise_parses_odds_and_injects_into_brief_and_prompt():
    raw_by_category = {
        "macro_rates": [
            {
                "id": "770450",
                "title": "Fed Decision in December?",
                "slug": "fed-decision-in-december",
                "active": True,
                "closed": False,
                "volume24hr": 5_000_000,
                "volume": 60_000_000,
                "endDate": "2026-12-10T04:59:00Z",
                "markets": [
                    {
                        "groupItemTitle": "25 bps increase",
                        "outcomes": '["Yes", "No"]',
                        "outcomePrices": '["0.935", "0.065"]',
                        "oneDayPriceChange": 0.005,
                        "oneWeekPriceChange": 0.02,
                        "volume": "13000000",
                        "active": True,
                        "closed": False,
                    },
                    {
                        "groupItemTitle": "No change",
                        "outcomes": '["Yes", "No"]',
                        "outcomePrices": '["0.038", "0.962"]',
                        "oneDayPriceChange": -0.005,
                        "oneWeekPriceChange": -0.015,
                        "volume": "16000000",
                        "active": True,
                        "closed": False,
                    },
                    {
                        "groupItemTitle": "50+ bps decrease",
                        "outcomes": '["Yes", "No"]',
                        "outcomePrices": '["0.002", "0.998"]',
                        "volume": "9000000",
                        "active": True,
                        "closed": False,
                    },
                    {
                        "groupItemTitle": "Closed option",
                        "outcomes": '["Yes", "No"]',
                        "outcomePrices": '["1", "0"]',
                        "volume": "5000000",
                        "active": True,
                        "closed": True,
                    },
                ],
            },
            # Noise: market cap game -> MUST be filtered out
            {
                "id": "noise-mcap",
                "title": "Largest Company end of December 2026?",
                "slug": "largest-company-dec-2026",
                "active": True,
                "closed": False,
                "volume24hr": 900_000,
                "volume": 12_000_000,
                "markets": [
                    {"groupItemTitle": "Apple", "outcomes": '["Yes", "No"]', "outcomePrices": '["0.91", "0.09"]', "active": True, "closed": False}
                ],
            },
            # Noise: tiny illiquid market -> MUST be filtered out
            {
                "id": "illiquid-1",
                "title": "Minor GDP revision?",
                "slug": "minor-gdp-revision",
                "active": True,
                "closed": False,
                "volume24hr": 100,
                "volume": 500,
                "markets": [
                    {"outcomes": '["Yes", "No"]', "outcomePrices": '["0.5", "0.5"]', "active": True, "closed": False}
                ],
            },
        ],
        "trade_tariffs": [
            {
                "id": "1065849",
                "title": "U.S. tariff rate on China on December 31, 2026?",
                "slug": "us-tariff-rate-on-china",
                "active": True,
                "closed": False,
                "volume24hr": 21_000,
                "volume": 52_000,
                "endDate": "2026-12-31T00:00:00Z",
                "markets": [
                    {
                        "groupItemTitle": "5-15%",
                        "outcomes": '["Yes", "No"]',
                        "outcomePrices": '["0.885", "0.115"]',
                        "oneDayPriceChange": 0.195,
                        "oneWeekPriceChange": -0.03,
                        "volume": "20718",
                        "active": True,
                        "closed": False,
                    }
                ],
            },
        ],
        "geopolitics": [
            # Test bad slug URL stripping on binary event
            {
                "id": "geo-1",
                "title": "Major regional ceasefire holds through Q1?",
                "slug": "bad/slug?evil=1",
                "active": True,
                "closed": False,
                "volume24hr": 250_000,
                "volume": 3_000_000,
                "endDate": "2027-03-31T00:00:00Z",
                "markets": [
                    {
                        "outcomes": '["Yes", "No"]',
                        "outcomePrices": '["0.52", "0.48"]',
                        "oneDayPriceChange": -0.04,
                        "oneWeekPriceChange": 0.08,
                        "volume": "3000000",
                        "active": True,
                        "closed": False,
                    }
                ],
            },
        ],
    }

    parsed = research_extensions.parse_polymarket_events(
        raw_by_category,
        fetched_at_utc="2026-11-26T08:00:00+00:00",
    )
    assert parsed["status"] == "ok"
    assert parsed["total_events"] == 3
    assert [ev["id"] for ev in parsed["events"]] == ["770450", "1065849", "geo-1"]

    fed = parsed["events"][0]
    assert fed["leading_option"] == "25 bps increase"
    assert fed["leading_prob_pct"] == 93.5
    assert fed["leading_chg_7d_pp"] == 2.0
    assert fed["priced_in_band"] == "consensus"
    assert fed["url"] == "https://polymarket.com/event/fed-decision-in-december"
    # Closed option excluded, <0.5% 3rd option dropped once >=2 options present
    assert len(fed["top_outcomes"]) == 2

    geo = parsed["events"][2]
    assert geo["market_type"] == "binary"
    assert geo["priced_in_band"] == "contested"
    assert geo["leading_prob_pct"] == 52.0
    # Unsafe slug MUST NOT produce a URL
    assert geo["url"] == ""

    # Headline selection ensures all 3 categories are represented
    assert {ev["category"] for ev in parsed["headline_events"]} == {"macro_rates", "trade_tariffs", "geopolitics"}

    # Brief & prompt integration (zero hardcoded ticker anchoring & no event-prob == asset-priced-in fallacy)
    brief = macro_compass.build_compass_brief(
        macro_overview={
            "fetched_at_utc": "2026-11-26T08:00:00+00:00",
            "headline_metrics": [],
            "polymarket_odds": parsed,
        }
    )
    assert brief["polymarket_odds"]["status"] == "ok"
    assert len(brief["polymarket_odds"]["events"]) == 3
    # Single remaining grouped option ('5-15%') MUST keep its groupItemTitle instead of collapsing to 'Yes'
    tariff_ev = parsed["events"][1]
    assert tariff_ev["market_type"] == "multi"
    assert tariff_ev["leading_option"] == "5-15%"

    prompt = macro_compass.build_compass_prompt(brief_path="/tmp/brief.json")
    assert "polymarket_odds" in prompt
    assert "共识已反映在资产价格中" not in prompt
    assert "严禁把盘口高概率" in prompt
    for anchor_token in ("existing_universes", "NI0", "AUDUSD", "RKAB", "霍尔木兹", "GEV", "XLU", "PWR"):
        assert anchor_token not in prompt


def test_polymarket_codex_review_regressions_ladder_malformed_vectors_timeout_and_per_cat_fallback():
    # 1. Date ladder preserves per-deadline semantics & does NOT mislabel Nov 30 (94.5%) under Dec 31 consensus
    #    Cross-tag overlap prefers more specific category (trade_tariffs over macro_rates)
    #    Policy event containing "TikTok" is NOT blocked by entertainment noise filter
    #    Malformed/reversed price vectors & out-of-range deltas & acceptingOrders=False & low-24h dead pools handled strictly
    raw_by_category = {
        "macro_rates": [
            # Duplicate of tariff event -> must be claimed by trade_tariffs (higher specificity)
            {
                "id": "dup-tariff-1",
                "title": "U.S. tariff rate on China on December 31, 2026?",
                "slug": "us-tariff-china",
                "active": True,
                "closed": False,
                "negRisk": True,
                "volume24hr": 25_000,
                "volume": 80_000,
                "endDate": "2026-12-31T00:00:00Z",
                "markets": [
                    {
                        "groupItemTitle": "5-15%",
                        "outcomes": '["Yes", "No"]',
                        "outcomePrices": '["0.885", "0.115"]',
                        "active": True,
                        "closed": False,
                        "acceptingOrders": True,
                    }
                ],
            },
            # Policy event mentioning TikTok must NOT be filtered out as entertainment noise
            {
                "id": "policy-tiktok",
                "title": "U.S. enforces TikTok divestiture rule by Q2?",
                "slug": "us-tiktok-divestiture-q2",
                "active": True,
                "closed": False,
                "volume24hr": 30_000,
                "volume": 200_000,
                "endDate": "2027-06-30T00:00:00Z",
                "markets": [
                    {
                        "outcomes": '["Yes", "No"]',
                        "outcomePrices": '["0.40", "0.60"]',
                        "oneDayPriceChange": 2.5,  # Out-of-range -> MUST become None
                        "oneWeekPriceChange": -0.05,
                        "active": True,
                        "closed": False,
                        "acceptingOrders": True,
                    }
                ],
            },
            # Malformed vectors: length mismatch ("No","Yes" vs ["0.9"]) MUST be dropped; reversed ("No","Yes" vs ["0.9","0.1"]) keeps Yes=10% & drops untrusted delta
            {
                "id": "vec-edge",
                "title": "ECB rate decision next quarter?",
                "slug": "ecb-rate-decision",
                "active": True,
                "closed": False,
                "negRisk": True,
                "volume24hr": 18_000,
                "volume": 120_000,
                "endDate": "2027-03-31T00:00:00Z",
                "markets": [
                    {
                        "groupItemTitle": "Broken vector",
                        "outcomes": '["No", "Yes"]',
                        "outcomePrices": '["0.9"]',
                        "active": True,
                        "closed": False,
                        "acceptingOrders": True,
                    },
                    {
                        "groupItemTitle": "Reversed Yes",
                        "outcomes": '["No", "Yes"]',
                        "outcomePrices": '["0.9", "0.1"]',
                        "oneDayPriceChange": 0.05,
                        "oneWeekPriceChange": 0.12,
                        "active": True,
                        "closed": False,
                        "acceptingOrders": True,
                    },
                    {
                        "groupItemTitle": "Halted market",
                        "outcomes": '["Yes", "No"]',
                        "outcomePrices": '["0.80", "0.20"]',
                        "active": True,
                        "closed": False,
                        "acceptingOrders": False,
                    },
                ],
            },
        ],
        "trade_tariffs": [
            {
                "id": "dup-tariff-1",
                "title": "U.S. tariff rate on China on December 31, 2026?",
                "slug": "us-tariff-china",
                "active": True,
                "closed": False,
                "negRisk": True,
                "volume24hr": 25_000,
                "volume": 80_000,
                "endDate": "2026-12-31T00:00:00Z",
                "markets": [
                    {
                        "groupItemTitle": "5-15%",
                        "outcomes": '["Yes", "No"]',
                        "outcomePrices": '["0.885", "0.115"]',
                        "active": True,
                        "closed": False,
                        "acceptingOrders": True,
                    }
                ],
            },
            # High historical volume but dead 24h volume ($750 < $1,500) -> MUST be filtered out
            {
                "id": "stale-vol-tariff",
                "title": "US-Canada tariff reduction by December?",
                "slug": "us-canada-tariff",
                "active": True,
                "closed": False,
                "volume24hr": 750,
                "volume": 500_000,
                "markets": [
                    {
                        "groupItemTitle": "December 31",
                        "outcomes": '["Yes", "No"]',
                        "outcomePrices": '["0.225", "0.775"]',
                        "active": True,
                        "closed": False,
                        "acceptingOrders": True,
                    }
                ],
            },
        ],
        "geopolitics": [
            # Multi-deadline non-mutually-exclusive date ladder
            {
                "id": "ladder-ceasefire",
                "title": "Israel x Iran ceasefire continues through...?",
                "slug": "israel-iran-ceasefire",
                "active": True,
                "closed": False,
                "negRisk": False,
                "volume24hr": 330_000,
                "volume": 62_000_000,
                "endDate": "2026-12-31T20:29:00Z",
                "markets": [
                    {
                        "groupItemTitle": "December 31",
                        "question": "Ceasefire continues through December 31?",
                        "endDateIso": "2026-12-31",
                        "outcomes": '["Yes", "No"]',
                        "outcomePrices": '["0.745", "0.255"]',
                        "oneWeekPriceChange": 0.09,
                        "volume24hr": 120_000,
                        "active": True,
                        "closed": False,
                        "acceptingOrders": True,
                    },
                    {
                        "groupItemTitle": "November 30",
                        "question": "Ceasefire continues through November 30?",
                        "endDateIso": "2026-11-30",
                        "outcomes": '["Yes", "No"]',
                        "outcomePrices": '["0.945", "0.055"]',
                        "oneWeekPriceChange": 0.10,
                        "volume24hr": 110_000,
                        "active": True,
                        "closed": False,
                        "acceptingOrders": True,
                    },
                    {
                        "groupItemTitle": "December 15",
                        "question": "Ceasefire continues through December 15?",
                        "endDateIso": "2026-12-15",
                        "outcomes": '["Yes", "No"]',
                        "outcomePrices": '["0.825", "0.175"]',
                        "oneWeekPriceChange": 0.09,
                        "volume24hr": 100_000,
                        "active": True,
                        "closed": False,
                        "acceptingOrders": True,
                    },
                ],
            },
            # Past upstream endDate while still acceptingOrders=True -> flagged explicitly
            {
                "id": "past-end-date",
                "title": "Major election outcome?",
                "slug": "major-election-outcome",
                "active": True,
                "closed": False,
                "negRisk": True,
                "volume24hr": 100_000,
                "volume": 5_000_000,
                "endDate": "2026-10-28T03:59:00Z",
                "markets": [
                    {
                        "groupItemTitle": "Candidate A",
                        "outcomes": '["Yes", "No"]',
                        "outcomePrices": '["0.75", "0.25"]',
                        "active": True,
                        "closed": False,
                        "acceptingOrders": True,
                    }
                ],
            },
        ],
    }

    parsed = research_extensions.parse_polymarket_events(
        raw_by_category,
        fetched_at_utc="2026-11-26T08:00:00+00:00",
    )
    by_id = {ev["id"]: ev for ev in parsed["events"]}

    # Cross-tag dedup assigns tariff event to trade_tariffs, NOT macro_rates
    assert by_id["dup-tariff-1"]["category"] == "trade_tariffs"
    # Dead 24h tariff pool excluded
    assert "stale-vol-tariff" not in by_id
    # Policy TikTok event preserved, out-of-range 1D delta stripped to None
    assert "policy-tiktok" in by_id
    assert by_id["policy-tiktok"]["leading_chg_1d_pp"] is None
    assert by_id["policy-tiktok"]["leading_chg_7d_pp"] == -5.0

    # Malformed vector dropped, halted market dropped, reversed Yes extracted accurately with untrusted delta cleared
    vec_ev = by_id["vec-edge"]
    assert len(vec_ev["top_outcomes"]) == 1
    assert vec_ev["top_outcomes"][0]["label"] == "Reversed Yes"
    assert vec_ev["top_outcomes"][0]["prob_pct"] == 10.0
    assert vec_ev["top_outcomes"][0]["chg_1d_pp"] is None
    assert vec_ev["top_outcomes"][0]["chg_7d_pp"] is None

    # Date ladder preserves chronological order & binds headline probability to its OWN deadline (Dec 31 74.5%, not Nov 30 94.5%)
    ladder = by_id["ladder-ceasefire"]
    assert ladder["market_type"] == "date_ladder"
    assert ladder["consensus_band"] == "ladder"
    assert [o["label"] for o in ladder["top_outcomes"]] == ["November 30", "December 15", "December 31"]
    assert [o["end_date"] for o in ladder["top_outcomes"]] == ["2026-11-30", "2026-12-15", "2026-12-31"]
    assert ladder["leading_option"] == "December 31"
    assert ladder["leading_prob_pct"] == 74.5
    assert ladder["leading_end_date"] == "2026-12-31"

    # Past upstream endDate flagged
    assert by_id["past-end-date"]["end_date_passed"] is True

    # 2. Strict non-blocking timeout in fetch_polymarket_macro_odds (must NOT hang on executor shutdown!)
    def slow_get(*_args, **_kwargs):
        time.sleep(0.8)
        raise TimeoutError("slow")

    with research_extensions._POLYMARKET_INFLIGHT_LOCK:
        research_extensions._POLYMARKET_INFLIGHT.clear()
    with patch.object(research_extensions.requests, "get", side_effect=slow_get):
        t0 = time.monotonic()
        pm_timeout_res = research_extensions.fetch_polymarket_macro_odds(timeout_seconds=0.12)
        elapsed = time.monotonic() - t0
    assert elapsed < 0.45, f"fetch_polymarket_macro_odds blocked past deadline ({elapsed:.2f}s)"
    assert pm_timeout_res["status"] == "error"

    # 3. Per-category last-known-good preservation across ok(3) -> partial(1) -> error
    CACHE.clear()
    macro_compass.clear_macro_source_cache()
    fake_live = {"VIX": {"last": 20.0, "chg_1d_pct": 0.0, "as_of": "2026-11-26"}}
    fake_fred = {"status": "ok", "series": {}}
    fake_cn = {"status": "ok", "pmi": {"status": "error"}}
    fake_anc = {"status": "ok", "ok_count": 1, "total": 1, "items": []}
    fake_cn_anc = {"status": "ok", "ok_count": 1, "total": 1, "items": []}

    pm_ok = research_extensions.parse_polymarket_events(
        {
            "macro_rates": [raw_by_category["macro_rates"][1]],
            "trade_tariffs": [raw_by_category["trade_tariffs"][0]],
            "geopolitics": [raw_by_category["geopolitics"][0]],
        },
        fetched_at_utc="2026-11-26T08:00:00+00:00",
    )
    pm_partial = research_extensions.parse_polymarket_events(
        {"macro_rates": [raw_by_category["macro_rates"][1]]},
        category_errors={"trade_tariffs": "timeout", "geopolitics": "502"},
        fetched_at_utc="2026-11-26T08:05:00+00:00",
    )
    pm_err = {"status": "error", "source": "Polymarket Gamma API", "total_events": 0, "events": [], "error": "down"}

    with (
        patch.object(macro_compass.market_snapshot, "fetch_macro_context", return_value=fake_live),
        patch.object(macro_compass.research_data, "fetch_fred_macro", return_value=fake_fred),
        patch.object(macro_compass.research_data, "fetch_china_macro_core", return_value=fake_cn),
        patch.object(macro_compass, "fetch_cross_asset_anchors", return_value=fake_anc),
        patch.object(macro_compass, "fetch_cn_futures_anchors", return_value=fake_cn_anc),
    ):
        with patch.object(macro_compass, "fetch_polymarket_macro_odds", return_value=pm_ok):
            res_ok = macro_compass.collect_macro_overview(refresh=True)
            assert res_ok["sources_status"]["polymarket_odds"] == "ok"
            assert res_ok["polymarket_odds"]["status"] == "ok"
            assert res_ok["polymarket_odds"]["total_events"] == 3

        with patch.object(macro_compass, "fetch_polymarket_macro_odds", return_value=pm_partial):
            res_partial = macro_compass.collect_macro_overview(refresh=True)
            assert res_partial["sources_status"]["polymarket_odds"] == "partial"
            assert res_partial["polymarket_odds"]["status"] == "partial"
            # Must preserve all 3 categories by carrying over prior cached categories!
            assert res_partial["polymarket_odds"]["total_events"] == 3
            assert set(res_partial["polymarket_odds"]["stale_categories"]) == {"trade_tariffs", "geopolitics"}

        with patch.object(macro_compass, "fetch_polymarket_macro_odds", return_value=pm_err):
            res_err = macro_compass.collect_macro_overview(refresh=True)
            # Outer & inner statuses strictly aligned as 'stale', and ALL 3 events still preserved!
            assert res_err["sources_status"]["polymarket_odds"] == "stale"
            assert res_err["polymarket_odds"]["status"] == "stale"
            assert res_err["polymarket_odds"]["stale_fallback"] is True
            assert res_err["polymarket_odds"]["total_events"] == 3
            assert all(ev.get("stale_fallback") is True for ev in res_err["polymarket_odds"]["events"])


def test_polymarket_round2_regressions_missing_subdate_full_vector_dedup_ttl_and_accepting():
    # 1. Date ladder whose representative sub-market lacks end_date MUST NOT backfill parent endDate
    #    Full price/outcome vector validation rejects malformed OTHER side (empty label, null, >1.0)
    #    Missing acceptingOrders MUST yield accepting_orders=False (not True)
    raw = {
        "geopolitics": [
            {
                "id": "ladder-missing-subdate",
                "title": "Ceasefire continues through...?",
                "slug": "ceasefire-missing-subdate",
                "active": True,
                "closed": False,
                "negRisk": False,
                "volume24hr": 200_000,
                "volume": 2_000_000,
                "endDate": "2026-12-31T00:00:00Z",
                "markets": [
                    {
                        "groupItemTitle": "November 30",
                        "endDateIso": "2026-11-30",
                        "outcomes": '["Yes", "No"]',
                        "outcomePrices": '["0.95", "0.05"]',
                        "active": True,
                        "closed": False,
                    },
                    {
                        "groupItemTitle": "December 15",
                        # Missing endDate / endDateIso on purpose!
                        "outcomes": '["Yes", "No"]',
                        "outcomePrices": '["0.70", "0.30"]',
                        "active": True,
                        "closed": False,
                    },
                ],
            },
        ],
        "macro_rates": [
            {
                "id": "bad-other-side",
                "title": "Malformed counterparty vectors?",
                "slug": "malformed-counterparty",
                "active": True,
                "closed": False,
                "negRisk": True,
                "volume24hr": 50_000,
                "volume": 500_000,
                "markets": [
                    {"groupItemTitle": "EmptyOtherLabel", "outcomes": '["Yes", ""]', "outcomePrices": '["0.6", "0.4"]', "active": True, "closed": False},
                    {"groupItemTitle": "NullOtherPrice", "outcomes": '["Yes", "No"]', "outcomePrices": '["0.6", null]', "active": True, "closed": False},
                    {"groupItemTitle": "OverOneOtherPrice", "outcomes": '["Yes", "No"]', "outcomePrices": '["0.6", "1.2"]', "active": True, "closed": False},
                    {"groupItemTitle": "DictLabel", "outcomes": ["Yes", {"bad": 1}], "outcomePrices": [0.6, 0.4], "active": True, "closed": False},
                    {"groupItemTitle": "IntLabel", "outcomes": ["Yes", 123], "outcomePrices": [0.6, 0.4], "active": True, "closed": False},
                    {"groupItemTitle": "HugeIntPrice", "outcomes": ["Yes", "No"], "outcomePrices": [0.6, 10**400], "active": True, "closed": False},
                ],
            },
        ],
    }
    parsed = research_extensions.parse_polymarket_events(raw, fetched_at_utc="2026-11-26T08:00:00+00:00")
    by_id = {ev["id"]: ev for ev in parsed["events"]}
    assert "bad-other-side" not in by_id

    ladder = by_id["ladder-missing-subdate"]
    assert ladder["market_type"] == "date_ladder"
    assert ladder["leading_option"] == "December 15"
    assert ladder["leading_prob_pct"] == 70.0
    # Must NOT borrow parent event endDate '2026-12-31'
    assert ladder["leading_end_date"] is None
    # Missing acceptingOrders must NOT fabricate True
    assert ladder["accepting_orders"] is False

    # 2. Cross-category dedup across fresh + cached fallback: same event ID MUST NEVER duplicate in events or headlines
    #    AND core 5-source 600s TTL MUST NOT shrink when only Polymarket degrades, while Polymarket retries independently
    CACHE.clear()
    macro_compass.clear_macro_source_cache()
    shared_event_old = {
        "id": "shared-tariff-99",
        "title": "U.S. tariff rate on China?",
        "slug": "us-tariff-rate-99",
        "active": True,
        "closed": False,
        "negRisk": True,
        "volume24hr": 50_000,
        "volume": 500_000,
        "endDate": "2026-12-31T00:00:00Z",
        "markets": [
            {"groupItemTitle": "5-15%", "outcomes": '["Yes", "No"]', "outcomePrices": '["0.80", "0.20"]', "active": True, "closed": False, "acceptingOrders": True}
        ],
    }
    shared_event_new = {
        **shared_event_old,
        "markets": [
            {"groupItemTitle": "5-15%", "outcomes": '["Yes", "No"]', "outcomePrices": '["0.88", "0.12"]', "active": True, "closed": False, "acceptingOrders": True}
        ],
    }
    pm_full = research_extensions.parse_polymarket_events(
        {"macro_rates": [shared_event_old], "trade_tariffs": [shared_event_old], "geopolitics": []},
        fetched_at_utc="2026-11-26T08:00:00+00:00",
    )
    pm_partial_overlap = research_extensions.parse_polymarket_events(
        {"macro_rates": [shared_event_new]},
        category_errors={"trade_tariffs": "timeout", "geopolitics": "timeout"},
        fetched_at_utc="2026-11-26T08:05:00+00:00",
    )

    core_calls = {"live": 0}
    fake_live = {"VIX": {"last": 20.0, "chg_1d_pct": 0.0, "as_of": "2026-11-26"}}
    fake_fred = {"status": "ok", "series": {}}
    fake_cn = {"status": "ok", "pmi": {"status": "error"}}
    fake_anc = {"status": "ok", "ok_count": 1, "total": 1, "items": []}
    fake_cn_anc = {"status": "ok", "ok_count": 1, "total": 1, "items": []}

    def tracked_live():
        core_calls["live"] += 1
        return fake_live

    recorded_ttls: list[tuple[tuple, float]] = []
    orig_cache_set = CACHE.set

    def spy_cache_set(key, value, ttl):
        recorded_ttls.append((key, ttl))
        return orig_cache_set(key, value, ttl)

    with (
        patch.object(macro_compass.market_snapshot, "fetch_macro_context", side_effect=tracked_live),
        patch.object(macro_compass.research_data, "fetch_fred_macro", return_value=fake_fred),
        patch.object(macro_compass.research_data, "fetch_china_macro_core", return_value=fake_cn),
        patch.object(macro_compass, "fetch_cross_asset_anchors", return_value=fake_anc),
        patch.object(macro_compass, "fetch_cn_futures_anchors", return_value=fake_cn_anc),
        patch.object(CACHE, "set", side_effect=spy_cache_set),
    ):
        with patch.object(macro_compass, "fetch_polymarket_macro_odds", return_value=pm_full):
            r1 = macro_compass.collect_macro_overview(refresh=True)
            assert [ev["id"] for ev in r1["polymarket_odds"]["events"]] == ["shared-tariff-99"]
            assert r1["polymarket_odds"]["events"][0]["category"] == "trade_tariffs"

        recorded_ttls.clear()
        with patch.object(macro_compass, "fetch_polymarket_macro_odds", return_value=pm_partial_overlap):
            r2 = macro_compass.collect_macro_overview(refresh=True)
            # Zero duplicate IDs in events or headlines, ownership stays trade_tariffs with refreshed 88% odds!
            ev_ids = [ev["id"] for ev in r2["polymarket_odds"]["events"]]
            hl_ids = [ev["id"] for ev in r2["polymarket_odds"]["headline_events"]]
            assert ev_ids == ["shared-tariff-99"]
            assert hl_ids == ["shared-tariff-99"]
            assert r2["polymarket_odds"]["events"][0]["category"] == "trade_tariffs"
            assert r2["polymarket_odds"]["events"][0]["leading_prob_pct"] == 88.0

        # Core 5 sources are all ok -> main overview cache MUST keep full 600s TTL; Polymarket slot gets 60s
        ttl_by_key = dict(recorded_ttls)
        assert ttl_by_key[("compass_macro_overview",)] == macro_compass.TTL_COMPASS_MACRO_SECONDS
        assert ttl_by_key[("compass_polymarket_odds",)] == macro_compass.TTL_COMPASS_POLYMARKET_DEGRADED_SECONDS

        # Simulate independent Polymarket slot expiring while main overview cache is still hot:
        # 4 concurrent callers MUST coalesce into ONE Polymarket retry, NEVER re-fetch core 5 sources,
        # and EVERY caller MUST observe atomically consistent outer/inner Polymarket status!
        CACHE.invalidate(("compass_polymarket_odds",))
        live_before = core_calls["live"]
        pm_retry_calls = {"count": 0}

        def slow_pm_recover(*_args, **_kwargs):
            pm_retry_calls["count"] += 1
            time.sleep(0.05)
            return pm_full

        with patch.object(macro_compass, "fetch_polymarket_macro_odds", side_effect=slow_pm_recover):
            concurrent_results: list[dict] = []
            threads = [
                threading.Thread(
                    target=lambda: concurrent_results.append(macro_compass.collect_macro_overview(refresh=False))
                )
                for _ in range(4)
            ]
            for th in threads:
                th.start()
            for th in threads:
                th.join(timeout=2.0)

        assert core_calls["live"] == live_before
        assert pm_retry_calls["count"] == 1
        assert len(concurrent_results) == 4
        for res in concurrent_results:
            assert res["sources_status"]["polymarket_odds"] == "ok"
            assert res["polymarket_odds"]["status"] == "ok"
            assert "polymarket_odds" not in (res.get("stale_sources") or {})

        # Subsequent cached read serves overlaid 'ok' Polymarket state with 0 upstream calls
        with patch.object(macro_compass, "fetch_polymarket_macro_odds", side_effect=AssertionError("should hit independent cache")):
            r4 = macro_compass.collect_macro_overview(refresh=False)
            assert r4["sources_status"]["polymarket_odds"] == "ok"
            assert r4["polymarket_odds"]["status"] == "ok"
            assert "polymarket_odds" not in (r4.get("stale_sources") or {})

        # Double-checked locking race: caller A sees outer cache miss and pauses BEFORE acquiring
        # _PM_RETRY_INFLIGHT_LOCK while caller B completes the refresh & clears future -> A MUST reuse B's cached result!
        CACHE.invalidate(("compass_polymarket_odds",))
        dcl_calls = {"count": 0}
        a_saw_miss = threading.Event()
        b_finished = threading.Event()
        orig_cache_get = CACHE.get
        outer_miss_holder = {"seen": False}

        def staged_cache_get(key):
            res = orig_cache_get(key)
            if key == ("compass_polymarket_odds",) and not res[0] and not outer_miss_holder["seen"]:
                outer_miss_holder["seen"] = True
                a_saw_miss.set()
                b_finished.wait(timeout=2.0)
            return res

        def counted_pm_fetch(*_args, **_kwargs):
            dcl_calls["count"] += 1
            return pm_full

        dcl_results: dict[str, dict] = {}
        with (
            patch.object(CACHE, "get", side_effect=staged_cache_get),
            patch.object(macro_compass, "fetch_polymarket_macro_odds", side_effect=counted_pm_fetch),
        ):
            t_a = threading.Thread(target=lambda: dcl_results.__setitem__("a", macro_compass._overlay_polymarket_on_overview(r2, macro_compass._refresh_polymarket_singleflight())))
            t_a.start()
            assert a_saw_miss.wait(timeout=2.0)
            # Caller B runs to completion while Caller A is paused before taking _PM_RETRY_INFLIGHT_LOCK
            dcl_results["b"] = macro_compass._overlay_polymarket_on_overview(
                r2,
                macro_compass._refresh_polymarket_singleflight(),
            )
            b_finished.set()
            t_a.join(timeout=2.0)

        assert dcl_calls["count"] == 1
        assert dcl_results["a"]["polymarket_odds"] == dcl_results["b"]["polymarket_odds"]





