#!/usr/bin/env python3
"""Offline unit tests for scripts/research_data.py (zero live network calls)."""
import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

import pandas as pd

ROOT = os.path.dirname(os.path.dirname(__file__))
sys.path.insert(0, os.path.join(ROOT, "scripts"))

import research_data as rd  # noqa: E402
import research_extensions as rext  # noqa: E402


SAMPLE_FRED_CSV = """observation_date,DFII10
2026-11-11,.
2026-11-12,2.86
2026-11-13,2.91
2026-11-16,
2026-11-17,2.89
"""

SAMPLE_CFTC_CSV = """"CORN - CHICAGO BOARD OF TRADE",261110,2026-11-10,002602,CBT ,00,002 ,  1979758,  463839,  862967,  298636,   23135,   74284,  243161,  265789,  308572
"CRUDE OIL, LIGHT SWEET-WTI - ICE FUTURES EUROPE",261110,2026-11-10,067411,ICEU,01,067 ,  776209,  463136,  343228,    5979,  105413,   53770,   10969,   25081,   27693
"COPPER- #1 - COMMODITY EXCHANGE INC.",261110,2026-11-10,085692,CMX ,00,085 ,  314115,   66394,  114603,   52210,   37548,   12193,   57425,   22361,   38966
"""

SAMPLE_EIA_WPSR_CSV = """"STUB_1","11/13/26","11/6/26","Difference","Percent Change","11/14/25","Difference","Percent Change"
"Crude Oil","694.158","693.952","0.206","0.000","835.081","-140.923","-16.900"
"Commercial (Excluding SPR)","417.797","414.417","3.380","0.800","424.155","-6.358","-1.500"
"Strategic Petroleum Reserve (SPR)","276.361","279.535","-3.174","-1.100","410.926","-134.565","-32.700"
"Total Motor Gasoline","215.949","210.757","5.193","2.500","207.391","8.558","4.100"
"Distillate Fuel Oil","104.901","103.330","1.571","1.500","111.080","-6.179","-5.600"
"STUB_1","STUB_2","11/13/26","11/6/26","Difference","11/14/25","Difference","11/13/26","11/14/25","Percent Change"
"Crude Oil Supply ","(1)     Domestic Production","14,032","14,013","19","13,834","198","14,010","13,748","1.9"
"Crude Oil Supply ","(7)     Net Imports (Including SPR)","1,088","3,243","-2,155","1,792","-704","2,138","1,611","32.7"
"""

SAMPLE_EIA_NGS_CSV = """\ufeffEnergy Information Administration
"Working Gas in Underground Storage, Lower 48"
"Released: November 13, 2026 at 10:30 a.m. (eastern time) for the Week Ending November 6, 2026"
Region,Stocks in billion (Bcf),,,,,,,,,,,,Historical Comparisons
,,,,,,,,,,,,,Year Ago (11/06/25),,,,,,5-Year (2021-25) Average
,06-Nov-26,,,30-Oct-26,,,net change (Bcf),,,implied flow (Bcf),,,stocks (Bcf),,,% change,,,stocks (Bcf),,,% change
East,"938","","","926","",,"12","",,"12",,"","913",,,"2.7",,,"910",,,"3.1",,"",""
Midwest,"1,129","","","1,111","",,"18","",,"18",,"","1,115",,,"1.3",,,"1,102",,,"2.5",,"",""
Total,"3,862","","","3,801","",,"61","",,"61",,"","3,954",,,"-2.3",,,"3,787",,,"2.0",,"",""
"""

SAMPLE_FARSIDE_HTML = """
<html><body>
<table class="etf">
<thead>
  <tr><th></th><th>IBIT</th><th>FBTC</th><th>GBTC</th><th>Total</th></tr>
  <tr><th>Date</th><th>IBIT</th><th>FBTC</th><th>GBTC</th><th></th></tr>
</thead>
<tbody>
  <tr><td>13 Nov 2026</td><td>(143.5)</td><td>(41.9)</td><td>0.0</td><td>(185.4)</td></tr>
  <tr><td>16 Nov 2026</td><td>76.5</td><td>(11.3)</td><td>(34.6)</td><td>7.9</td></tr>
  <tr><td>17 Nov 2026</td><td>(209.5)</td><td>0.0</td><td>(17.7)</td><td>(239.9)</td></tr>
  <tr><td>18 Nov 2026</td><td>(148.7)</td><td>(69.3)</td><td>(20.3)</td><td>(306.5)</td></tr>
  <tr><td>Total</td><td>63,911</td><td>10,083</td><td>(28,221)</td><td>54,163</td></tr>
</tbody>
</table>
</body></html>
"""

SAMPLE_SEC_COMPANYFACTS = {
    "cik": 1050915,
    "entityName": "QUANTA SERVICES, INC.",
    "facts": {
        "us-gaap": {
            "Revenues": {
                "units": {
                    "USD": [
                        {"start": "2025-07-01", "end": "2025-09-30", "val": 6000000000, "form": "10-Q", "fy": 2025, "fp": "Q3", "filed": "2025-10-30"},
                        {"start": "2025-07-01", "end": "2025-09-30", "val": 6050000000, "form": "10-Q", "fy": 2026, "fp": "Q3", "filed": "2026-10-29"},
                        {"start": "2026-07-01", "end": "2026-09-30", "val": 12014877000, "form": "10-Q", "fy": 2026, "fp": "Q3", "filed": "2026-10-29"},
                    ]
                }
            },
            "NetIncomeLoss": {
                "units": {
                    "USD": [
                        {"start": "2026-07-01", "end": "2026-09-30", "val": 420000000, "form": "10-Q", "fy": 2026, "fp": "Q3", "filed": "2026-10-29"},
                    ]
                }
            },
            "EarningsPerShareDiluted": {
                "units": {
                    "USD/shares": [
                        {"start": "2026-07-01", "end": "2026-09-30", "val": 2.81, "form": "10-Q", "fy": 2026, "fp": "Q3", "filed": "2026-10-29"},
                    ]
                }
            },
            "NetCashProvidedByUsedInOperatingActivities": {
                "units": {
                    "USD": [
                        {"start": "2026-01-01", "end": "2026-09-30", "val": 2940929000, "form": "10-Q", "fy": 2026, "fp": "Q3", "filed": "2026-10-29"},
                    ]
                }
            },
            "PaymentsToAcquireProductiveAssets": {
                "units": {
                    "USD": [
                        {"start": "2026-01-01", "end": "2026-09-30", "val": 510000000, "form": "10-Q", "fy": 2026, "fp": "Q3", "filed": "2026-10-29"},
                    ]
                }
            },
            "RevenueRemainingPerformanceObligation": {
                "units": {
                    "USD": [
                        {"end": "2026-09-30", "val": 41750000000, "form": "10-Q", "fy": 2026, "fp": "Q3", "filed": "2026-10-29"},
                        {"end": "2026-06-30", "val": 33550000000, "form": "10-Q", "fy": 2026, "fp": "Q2", "filed": "2026-07-30"},
                    ]
                }
            },
        }
    },
}


class TestPureParsers(unittest.TestCase):
    def test_parse_fred_csv_skips_missing_and_computes_change(self):
        res = rd.parse_fred_csv(SAMPLE_FRED_CSV, series_id="DFII10")
        self.assertEqual(res["series_id"], "DFII10")
        self.assertEqual(res["latest_date"], "2026-11-17")
        self.assertEqual(res["latest_value"], 2.89)
        self.assertEqual(res["previous_date"], "2026-11-13")
        self.assertEqual(res["previous_value"], 2.91)
        self.assertAlmostEqual(res["change"], -0.02, places=6)
        self.assertEqual(res["observations_count"], 3)

    def test_parse_cftc_disagg_handles_quoted_comma(self):
        wti = rd.parse_cftc_disagg(SAMPLE_CFTC_CSV, ["CRUDE OIL, LIGHT SWEET"])
        self.assertEqual(wti["report_date"], "2026-11-10")
        self.assertEqual(wti["open_interest"], 776209)
        self.assertEqual(wti["managed_money"]["long"], 10969)
        self.assertEqual(wti["managed_money"]["short"], 25081)
        self.assertEqual(wti["managed_money"]["net"], 10969 - 25081)
        self.assertEqual(wti["prod_merc"]["net"], 463136 - 343228)

        copper = rd.parse_cftc_disagg(SAMPLE_CFTC_CSV, "COPPER")
        self.assertEqual(copper["open_interest"], 314115)
        self.assertEqual(copper["managed_money"]["net"], 57425 - 22361)

    def test_parse_eia_wpsr_csv(self):
        res = rd.parse_eia_wpsr_csv(SAMPLE_EIA_WPSR_CSV)
        self.assertEqual(res["report_date"], "11/13/26")
        comm = res["metrics"]["commercial_crude_stocks_mb"]
        self.assertAlmostEqual(comm["current"], 417.797)
        self.assertAlmostEqual(comm["weekly_change"], 3.38)
        prod = res["metrics"]["us_crude_domestic_production_kbd"]
        self.assertAlmostEqual(prod["current"], 14032.0)
        self.assertAlmostEqual(prod["weekly_change"], 19.0)

    def test_parse_eia_ngs_csv(self):
        res = rd.parse_eia_ngs_csv(SAMPLE_EIA_NGS_CSV)
        self.assertEqual(res["report_date"], "06-Nov-26")
        tot = res["total_working_gas"]
        self.assertEqual(tot["current_bcf"], 3862.0)
        self.assertEqual(tot["net_change_bcf"], 61.0)
        self.assertEqual(tot["five_year_avg_bcf"], 3787.0)
        self.assertEqual(tot["vs_five_year_avg_bcf"], 75.0)
        self.assertEqual(tot["vs_five_year_avg_pct"], 2.0)
        self.assertIn("East", res["regions"])

    def test_extract_sec_companyfacts_fallback_dedup_and_annotations(self):
        res = rd.extract_sec_companyfacts(SAMPLE_SEC_COMPANYFACTS, limit=6)
        self.assertEqual(res["cik"], "0001050915")
        metrics = res["metrics"]
        self.assertEqual(metrics["revenue"]["tag"], "Revenues")
        self.assertEqual(len(metrics["revenue"]["records"]), 2)
        self.assertEqual(metrics["revenue"]["records"][0]["val"], 12014877000)
        # Comparative 2025-09-30 deduplicated to latest filed (6050000000)
        self.assertEqual(metrics["revenue"]["records"][1]["val"], 6050000000)
        self.assertEqual(metrics["operating_cash_flow"]["basis"], "GAAP_XBRL_reported_may_be_YTD")
        self.assertEqual(metrics["capex"]["tag"], "PaymentsToAcquireProductiveAssets")
        self.assertIn("RPO", metrics["remaining_performance_obligation"]["note"])
        self.assertEqual(metrics["remaining_performance_obligation"]["records"][0]["val"], 41750000000)

    def test_summarize_term_structure_backwardation_contango_flat(self):
        back_df = pd.DataFrame([
            {"symbol": "LC0", "name": "碳酸锂连续", "trade": 91500.0, "volume": 330000, "position": 340000},
            {"symbol": "LC2705", "name": "碳酸锂2705", "trade": 90620.0, "volume": 70000, "position": 200000},
            {"symbol": "LC2701", "name": "碳酸锂2701", "trade": 91500.0, "volume": 330000, "position": 340000},
            {"symbol": "LC2709", "name": "碳酸锂2709", "trade": 89900.0, "volume": 12000, "position": 50000},
        ])
        res = rd.summarize_term_structure(back_df)
        self.assertEqual(res["structure"], "Backwardation")
        self.assertEqual(res["front_contract"], "LC2701")
        self.assertEqual(res["back_contract"], "LC2709")
        self.assertEqual(res["contracts_count"], 3)
        self.assertAlmostEqual(res["near_minus_far_spread"], 1600.0)

        contango_rows = [
            {"symbol": "CU2701", "trade": 74000.0},
            {"symbol": "CU2703", "trade": 74800.0},
        ]
        self.assertEqual(rd.summarize_term_structure(contango_rows)["structure"], "Contango")

        flat_rows = [
            {"symbol": "RB2701", "trade": 3300.0},
            {"symbol": "RB2705", "trade": 3300.1},
        ]
        self.assertEqual(rd.summarize_term_structure(flat_rows)["structure"], "Flat")

    def test_parse_farside_btc_etf_html(self):
        res = rd.parse_farside_btc_etf_html(SAMPLE_FARSIDE_HTML, last_n=3)
        self.assertEqual(res["recent_days_count"], 3)
        self.assertAlmostEqual(res["recent_total_net_flow_musd"], 7.9 - 239.9 - 306.5, places=2)
        self.assertEqual(res["recent_daily_flows"][-1]["date"], "18 Nov 2026")
        self.assertEqual(res["recent_daily_flows"][-1]["total_net_flow_musd"], -306.5)
        self.assertEqual(res["recent_daily_flows"][-1]["etf_flows_musd"]["IBIT"], -148.7)
        self.assertEqual(res["cumulative_summary"]["cumulative_net_flow_musd"], 54163.0)

    def test_parse_usda_wasde_xml(self):
        sample_xml = """<?xml version="1.0" encoding="utf-8"?>
        <Report Name="wasde">
          <sr12>
            <Report Name="sr12" Report_Month="November 2026" sub_report_title="U.S. Feed Grain and Corn Supply and Use" sub_report_subtitle="Million Bushels">
              <matrix1>
                <attribute1 attribute1="Ending Stocks">
                  <m1_year_group market_year1="2025/26 Est.">
                    <m1_month_group forecast_month1=""><Cell cell_value1="1938" /></m1_month_group>
                  </m1_year_group>
                  <m1_year_group market_year1="2026/27 Proj.">
                    <m1_month_group forecast_month1="Nov"><Cell cell_value1="2150" /></m1_month_group>
                  </m1_year_group>
                </attribute1>
              </matrix1>
            </Report>
          </sr12>
        </Report>"""
        res = rd.parse_usda_wasde_xml(sample_xml, "Corn", ["sr12"])
        self.assertEqual(res["commodity"], "Corn")
        self.assertEqual(res["report_month"], "November 2026")
        self.assertEqual(res["tables"][0]["rows"][0]["attribute"], "Ending Stocks")
        self.assertEqual(res["tables"][0]["rows"][0]["values"][-1]["value"], 2150.0)


class TestRoutingAndIsolation(unittest.TestCase):
    def test_fetch_fred_macro_per_series_isolation(self):
        class FakeResp:
            def __init__(self, text):
                self.text = text

            def raise_for_status(self):
                return None

        def fake_get(url, timeout=15):
            if "DFII10" in url:
                return FakeResp(SAMPLE_FRED_CSV)
            raise TimeoutError("FRED series timeout")

        with patch.object(rd.requests, "get", side_effect=fake_get):
            res = rd.fetch_fred_macro()

        self.assertEqual(res["status"], "partial")
        self.assertEqual(res["series"]["DFII10"]["status"], "ok")
        self.assertEqual(res["series"]["DFII10"]["latest_value"], 2.89)
        self.assertEqual(res["series"]["DGS10"]["status"], "error")
        self.assertIn("timeout", res["series"]["DGS10"]["error"])

    def test_us_stock_single_source_failure_isolated(self):
        mock_snap = {"query": "PWR", "symbol": "PWR", "asset_class": "us_stock_or_etf", "technicals": {"last_close": 600.0}}
        mock_yf = {
            "basis_warning": "yfinance forward estimates are typically Non-GAAP/Adjusted; compare with SEC GAAP",
            "analyst_price_targets": {"mean": 770.0},
        }
        with (
            patch.object(rd, "_call_get_snapshot", return_value=mock_snap),
            patch.object(rd, "fetch_sec_edgar", side_effect=TimeoutError("SEC timeout")),
            patch.object(rd, "fetch_yfinance_consensus", return_value=mock_yf),
        ):
            out = rd.collect_research_data("PWR", include_macro=False)

        self.assertEqual(out["asset_class"], "us_stock_or_etf")
        self.assertEqual(out["fundamentals_and_supply"]["sec_edgar"]["status"], "error")
        self.assertIn("SEC timeout", out["fundamentals_and_supply"]["sec_edgar"]["error"])
        self.assertEqual(out["fundamentals_and_supply"]["yfinance_consensus"]["status"], "ok")
        self.assertEqual(out["sources_status"]["sec_edgar"]["status"], "error")
        self.assertEqual(out["sources_status"]["yfinance_consensus"]["status"], "ok")
        self.assertEqual(out["macro_dual_core"]["status"], "skipped")

    def test_a_share_routing_and_isolation(self):
        mock_snap = {"query": "600519", "symbol": "600519.SS", "asset_class": "a_share"}
        with (
            patch.object(rd, "_call_get_snapshot", return_value=mock_snap),
            patch.object(rd, "fetch_ashare_financial_abstract", return_value={"symbol": "600519", "periods_count": 6, "records": []}),
            patch.object(rd, "fetch_ashare_profit_forecast", side_effect=RuntimeError("THS forecast err")),
            patch.object(rext, "fetch_ashare_valuation_percentiles", return_value={"symbol": "600519", "pe_ttm_5y_percentile": 22.5}),
            patch.object(rd, "fetch_ashare_hsgt_flow", return_value={"northbound": [], "all_channels": []}),
            patch.object(rext, "fetch_freight_indices") as spy_freight,
            patch.object(rext, "fetch_cross_market_spread") as spy_spread,
        ):
            out = rd.collect_research_data("600519", include_macro=False)

        self.assertEqual(out["asset_class"], "a_share")
        self.assertEqual(out["fundamentals_and_supply"]["ths_financial_abstract"]["status"], "ok")
        self.assertEqual(out["fundamentals_and_supply"]["ths_profit_forecast"]["status"], "error")
        self.assertEqual(out["fundamentals_and_supply"]["valuation_5y_em"]["status"], "ok")
        self.assertEqual(out["fundamentals_and_supply"]["hsgt_fund_flow"]["status"], "ok")
        spy_freight.assert_not_called()
        spy_spread.assert_not_called()

    def test_cn_future_sc0_daily_curve_cross_spread_and_freight_routing(self):
        mock_snap = {
            "query": "SC0",
            "symbol": "SC0",
            "asset_class": "cn_future",
            "technicals": {"last_close": 585.9, "as_of_bar": "2026-11-19"},
        }
        with (
            patch.object(rd, "_call_get_snapshot", return_value=mock_snap),
            patch.object(rext, "fetch_cn_daily_term_structure", return_value={"structure": "Backwardation (现货升水/多头正展期)", "annualized_roll_yield_curve_pct": 18.5}),
            patch.object(rext, "fetch_cross_market_spread", return_value={"group": "crude_oil", "brent_minus_wti_usd_bbl": 7.06}),
            patch.object(rext, "fetch_freight_indices", return_value={"requested_keys": ["BDTI", "BCTI"], "indices": {"BDTI": {"latest_value": 4564.0}}}),
        ):
            out = rd.collect_research_data("SC0", include_macro=False)

        self.assertEqual(out["asset_class"], "cn_future")
        self.assertEqual(out["fundamentals_and_supply"]["inventory_em"]["status"], "skipped")
        self.assertEqual(out["fundamentals_and_supply"]["spot_and_basis"]["status"], "skipped")
        self.assertEqual(out["fundamentals_and_supply"]["term_structure"]["status"], "ok")
        self.assertEqual(out["fundamentals_and_supply"]["cross_market_spread"]["status"], "ok")
        self.assertEqual(out["fundamentals_and_supply"]["baltic_freight"]["status"], "ok")

    def test_commodity_routing_with_wasde_and_isolation(self):
        mock_snap = {"query": "ZC=F", "symbol": "ZC=F", "asset_class": "commodity"}
        with (
            patch.object(rd, "_call_get_snapshot", return_value=mock_snap),
            patch.object(rd, "fetch_cftc_cot", return_value={"open_interest": 1979758}),
            patch.object(rd, "fetch_usda_wasde", return_value={"commodity": "Corn", "report_month": "November 2026"}),
            patch.object(rext, "fetch_overseas_term_structure", return_value={"structure": "Contango (远月升水/空头正展期)", "annualized_roll_yield_curve_pct": -4.2}),
            patch.object(rext, "fetch_freight_indices", return_value={"requested_keys": ["BDI", "BSI"], "indices": {"BDI": {"latest_value": 2914.0}}}),
            patch.object(rext, "fetch_cross_market_spread") as spy_spread,
        ):
            out = rd.collect_research_data("ZC=F", include_macro=False)

        self.assertEqual(out["fundamentals_and_supply"]["cftc_cot"]["status"], "ok")
        self.assertEqual(out["fundamentals_and_supply"]["eia"]["status"], "skipped")
        self.assertEqual(out["fundamentals_and_supply"]["usda_wasde"]["status"], "ok")
        self.assertEqual(out["fundamentals_and_supply"]["term_structure"]["status"], "ok")
        self.assertEqual(out["fundamentals_and_supply"]["baltic_freight"]["status"], "ok")
        self.assertNotIn("cross_market_spread", out["fundamentals_and_supply"])
        spy_spread.assert_not_called()

    def test_crypto_routing_btc_vs_eth_and_oi_isolation(self):
        mock_snap_btc = {"query": "BTC", "symbol": "BTC-USD", "asset_class": "crypto"}
        with (
            patch.object(rd, "_call_get_snapshot", return_value=mock_snap_btc),
            patch.object(rd, "fetch_defillama_stablecoins", return_value={"total_circulating_usd": 200e9}),
            patch.object(
                rd,
                "fetch_crypto_exchange_oi",
                side_effect=lambda eid, base: (
                    {"exchange": eid, "open_interest_value_usd": 2e9}
                    if eid == "okx"
                    else (_ for _ in ()).throw(RuntimeError("Binance geo-blocked"))
                ),
            ),
            patch.object(rd, "fetch_farside_btc_etf_flows", return_value={"recent_total_net_flow_musd": -50.0}),
        ):
            out = rd.collect_research_data("BTC", include_macro=False)

        self.assertEqual(out["fundamentals_and_supply"]["stablecoins_defillama"]["status"], "ok")
        self.assertEqual(out["fundamentals_and_supply"]["perp_open_interest"]["okx"]["status"], "ok")
        self.assertEqual(out["fundamentals_and_supply"]["perp_open_interest"]["binance"]["status"], "error")
        self.assertEqual(out["fundamentals_and_supply"]["farside_btc_etf"]["status"], "ok")

    def test_call_get_snapshot_full_chanlun_signature_compat(self):
        def fake_no_full(raw_symbol, period="3y", include_macro=True, include_fundamentals=True):
            return {"symbol": raw_symbol, "asset_class": "us_stock_or_etf"}

        def fake_with_full(raw_symbol, period="3y", include_macro=True, include_fundamentals=True, full_chanlun=False):
            return {"symbol": raw_symbol, "asset_class": "us_stock_or_etf", "full_chanlun": full_chanlun}

        with patch.object(rd, "get_snapshot", fake_no_full):
            res1 = rd._call_get_snapshot("PWR", "3y", True)
            self.assertNotIn("full_chanlun", res1)

        with patch.object(rd, "get_snapshot", fake_with_full):
            res2 = rd._call_get_snapshot("PWR", "3y", True)
            self.assertTrue(res2["full_chanlun"])

    def test_cli_main_output_file_and_no_macro(self):
        mock_snap = {"query": "PWR", "symbol": "PWR", "asset_class": "us_stock_or_etf"}
        with tempfile.TemporaryDirectory() as tmpdir:
            out_path = os.path.join(tmpdir, "pwr.json")
            buf = io.StringIO()
            with (
                patch.object(rd, "_call_get_snapshot", return_value=mock_snap),
                patch.object(rd, "fetch_sec_edgar", return_value={"cik": "0001050915"}),
                patch.object(rd, "fetch_yfinance_consensus", return_value={"analyst_price_targets": {}}),
                redirect_stdout(buf),
            ):
                rc = rd.main(["PWR", "--no-macro", "--output", out_path])
            self.assertEqual(rc, 0)
            self.assertTrue(os.path.exists(out_path))
            with open(out_path, encoding="utf-8") as fh:
                saved = json.load(fh)
            self.assertEqual(saved["symbol"], "PWR")
            self.assertEqual(saved["macro_dual_core"]["status"], "skipped")
            self.assertEqual(json.loads(buf.getvalue())["symbol"], "PWR")

    def test_codex_regressions_cftc_wasde_nan_basis_timeout_and_sec_tags(self):
        # 1) CFTC exact contract_code selects NYMEX WTI (067651) over ICE Europe (067411) & picks latest date
        sample_cftc = (
            '"CRUDE OIL, LIGHT SWEET-WTI - ICE FUTURES EUROPE",261110,2026-11-10,067411,I,067,00,776209,200,100,50,50,10,10969,25081,1000\n'
            '"WTI-PHYSICAL - NEW YORK MERCANTILE EXCHANGE",261103,2026-11-03,067651,N,067,00,1800000,400000,500000,100000,90000,50000,200000,150000,80000\n'
            '"WTI-PHYSICAL - NEW YORK MERCANTILE EXCHANGE",261110,2026-11-10,067651,N,067,00,1917473,410000,510000,100000,90000,50000,210000,140000,80000\n'
        )
        cftc_res = rd.parse_cftc_disagg(sample_cftc, market_query=["CRUDE OIL, LIGHT SWEET"], contract_code="067651")
        self.assertEqual(cftc_res["contract_code"], "067651")
        self.assertEqual(cftc_res["report_date"], "2026-11-10")
        self.assertEqual(cftc_res["open_interest"], 1917473)

        # 2) WASDE parses attribute4 & world matrix with Ending Stocks (7th column) and rejects all-empty tables
        sample_wasde = """
        <WASDE>
          <sr15><Report Report_Month="November 2026" sub_report_title="U.S. Soybeans">
            <attribute4 attribute4="Ending Stocks">
              <m1_year_group market_year4="2026/27">
                <m1_month_group forecast_month4="Nov"><Cell cell_value4="350" /></m1_month_group>
              </m1_year_group>
            </attribute4>
          </Report></sr15>
          <sr28><Report Report_Month="November 2026" sub_report_title="World Soybean">
            <matrix4 region_header4="2026/27">
              <m1_region_group2 region4="World 2/">
                <m1_attribute_group2 attribute4="Beginning Stocks"><Cell cell_value4="115.0" /></m1_attribute_group2>
                <m1_attribute_group2 attribute4="Production"><Cell cell_value4="428.0" /></m1_attribute_group2>
                <m1_attribute_group2 attribute4="Imports"><Cell cell_value4="179.0" /></m1_attribute_group2>
                <m1_attribute_group2 attribute4="Domestic Crush"><Cell cell_value4="359.0" /></m1_attribute_group2>
                <m1_attribute_group2 attribute4="Domestic Total"><Cell cell_value4="412.0" /></m1_attribute_group2>
                <m1_attribute_group2 attribute4="Exports"><Cell cell_value4="184.0" /></m1_attribute_group2>
                <m1_attribute_group2 attribute4="Ending Stocks"><Cell cell_value4="126.01" /></m1_attribute_group2>
              </m1_region_group2>
            </matrix4>
          </Report></sr28>
        </WASDE>
        """
        wasde_out = rd.parse_usda_wasde_xml(sample_wasde, "Soybeans", ["sr15", "sr28"])
        self.assertEqual(wasde_out["tables"][0]["rows"][0]["attribute"], "Ending Stocks")
        world_metrics = wasde_out["tables"][1]["rows"][0]["metrics"]
        self.assertEqual(world_metrics["Ending Stocks"], 126.01)
        with self.assertRaises(ValueError):
            rd.parse_usda_wasde_xml("<WASDE><sr15><Report /></sr15></WASDE>", "Soybeans", ["sr15"])

        # 3) _to_float rejects "NaN"/"Inf" strings; Farside all-missing returns None not 0.0
        self.assertIsNone(rd._to_float("NaN"))
        self.assertIsNone(rd._to_float("Inf"))
        self.assertIsNone(rd._to_float("-inf"))
        farside_empty_html = "<table><tr><th>Date</th><th>IBIT</th><th>Total</th></tr><tr><td>18 Nov 2026</td><td>-</td><td>-</td></tr></table>"
        f_res = rd.parse_farside_btc_etf_html(farside_empty_html)
        self.assertIsNone(f_res["recent_total_net_flow_musd"])

        # 4) Term structure returns full contracts + insufficient_contracts + decade-wrap 3-digit sorting
        ts_single = rd.summarize_term_structure([{"symbol": "LC2701", "trade": 91500, "tradedate": "2026-11-19"}])
        self.assertEqual(ts_single["structure"], "insufficient_contracts")
        self.assertEqual(rd._parse_contract_month("CF912", ref_year=2030), (202912, "2912"))
        self.assertEqual(rd._parse_contract_month("CF001", ref_year=2030), (203001, "3001"))
        ts_czce = rd.summarize_term_structure([
            {"symbol": "CF705", "trade": 15200},
            {"symbol": "CF612", "trade": 15600},
            {"symbol": "CF701", "trade": 15500},
        ])
        self.assertEqual([c["symbol"] for c in ts_czce["contracts"]], ["CF612", "CF701", "CF705"])
        self.assertEqual(ts_czce["structure"], "Backwardation")

        # 5) SEC XBRL merges across candidate tags, keeps higher-priority tag over later sub-tag for same period
        facts_switch = {
            "entityName": "Switch Corp",
            "cik": 123,
            "facts": {
                "us-gaap": {
                    "Revenues": {
                        "units": {"USD": [{"start": "2024-01-01", "end": "2024-12-31", "val": 100, "form": "10-K", "filed": "2025-02-01"}]}
                    },
                    "SalesRevenueServicesNet": {
                        "units": {"USD": [{"start": "2024-01-01", "end": "2024-12-31", "val": 10, "form": "10-K", "filed": "2026-02-01"}]}
                    },
                    "RevenueFromContractWithCustomerExcludingAssessedTax": {
                        "units": {"USD": [{"start": "2026-07-01", "end": "2026-09-30", "val": 50, "form": "10-Q", "filed": "2026-11-01"}]}
                    },
                }
            },
        }
        sec_out = rd.extract_sec_companyfacts(facts_switch)
        rev = sec_out["metrics"]["revenue"]
        self.assertEqual(rev["records"][0]["end"], "2026-09-30")
        self.assertEqual(rev["records"][0]["period_type"], "quarterly")
        self.assertEqual(rev["records"][1]["val"], 100)  # higher-priority Revenues (100) not clobbered by SalesRevenueServicesNet (10)
        self.assertEqual(rev["records"][1]["period_type"], "annual")

        # 6) _run_with_timeout daemon timeout does NOT swallow main stdout & restores sys.stdout when idle
        import time

        def hang():
            print("noisy worker print")
            time.sleep(5)

        with self.assertRaises(TimeoutError):
            rd._run_with_timeout(hang, timeout_s=0.05, silence_stdout=True)
        buf = io.StringIO()
        with redirect_stdout(buf):
            print("main stdout intact")
        self.assertIn("main stdout intact", buf.getvalue())

        # 7) All-empty yfinance returns error & CLI returns 1 when snapshot + all sources fail, sanitizing Inf
        with (
            patch.object(rd, "_call_get_snapshot", return_value={"error": "No data", "bad": float("inf")}),
            patch.object(rd, "fetch_sec_edgar", side_effect=RuntimeError("err")),
            patch.object(rd, "fetch_yfinance_consensus", return_value={"status": "error", "error": "empty"}),
            redirect_stdout(io.StringIO()),
        ):
            rc_fail = rd.main(["BADSYM", "--no-macro"])
            self.assertEqual(rc_fail, 1)


class TestMultiAssetExtensionsAndStrictRouting(unittest.TestCase):
    def test_overseas_curve_candidates_and_roll_yield(self):
        from datetime import date

        # CL=F exact NYMEX expiry: Dec26 expires 2026-11-20 (3 bdays before Nov 25) -> active on Nov 19, expired on Nov 21
        cands_cl_active = rext.build_overseas_contract_candidates("CL=F", ref_date=date(2026, 11, 19))
        self.assertEqual(len(cands_cl_active), 6)
        self.assertEqual(cands_cl_active[0]["ticker"], "CLZ26.NYM")
        self.assertEqual(cands_cl_active[-1]["ticker"], "CLZ27.NYM")
        cands_cl_post = rext.build_overseas_contract_candidates("CL=F", ref_date=date(2026, 11, 21))
        self.assertEqual(cands_cl_post[0]["ticker"], "CLF27.NYM")

        # BZ=F CME Rule 698 (M-2 last bday): BZF27 expires 2026-11-30 -> still active on Nov 26, expired on Dec 1
        cands_bz_26 = rext.build_overseas_contract_candidates("BZ=F", ref_date=date(2026, 11, 26))
        self.assertEqual(cands_bz_26[0]["ticker"], "BZF27.NYM")
        self.assertNotIn("BZZ26.NYM", [c["ticker"] for c in cands_bz_26])
        cands_bz_dec1 = rext.build_overseas_contract_candidates("BZ=F", ref_date=date(2026, 12, 1))
        self.assertEqual(cands_bz_dec1[0]["ticker"], "BZG27.NYM")

        cands_hg = rext.build_overseas_contract_candidates("HG=F", ref_date=date(2026, 11, 19), max_contracts=5)
        self.assertEqual([c["ticker"] for c in cands_hg], ["HGZ26.CMX", "HGH27.CMX", "HGK27.CMX", "HGN27.CMX", "HGU27.CMX"])

        curve = rext.summarize_futures_curve(
            [
                {"contract": "CLZ27.NYM", "expiry_ym": 202712, "price": 72.0, "date": "2026-11-19"},
                {"contract": "CLZ26.NYM", "expiry_ym": 202612, "price": 83.72, "date": "2026-11-19"},
                {"contract": "CLF27.NYM", "expiry_ym": 202701, "price": 82.77, "date": "2026-11-19"},
            ],
            unit="USD/bbl",
        )
        self.assertIn("Backwardation", curve["structure"])
        self.assertEqual(curve["front_contract"], "CLZ26.NYM")
        self.assertEqual(curve["far_contract"], "CLZ27.NYM")
        self.assertEqual(curve["months_front_to_second"], 1)
        self.assertEqual(curve["months_front_to_far"], 12)
        self.assertGreater(curve["annualized_roll_yield_near_pct"], 10.0)
        self.assertAlmostEqual(curve["annualized_roll_yield_curve_pct"], ((83.72 / 72.0) - 1.0) * 100.0, places=2)

        # Flat front-to-far [100, 90, 100] must NOT mask huge near-month roll (+133.33%)
        humped = rext.summarize_futures_curve(
            [
                {"contract": "X2612", "expiry_ym": 202612, "price": 100.0, "date": "2026-11-19"},
                {"contract": "X2701", "expiry_ym": 202701, "price": 90.0, "date": "2026-11-19"},
                {"contract": "X2702", "expiry_ym": 202702, "price": 100.0, "date": "2026-11-19"},
            ]
        )
        self.assertIn("首尾平坦", humped["structure"])
        self.assertAlmostEqual(humped["annualized_roll_yield_near_pct"], 133.333, places=2)
        self.assertIn("近月段存在明显结构倾斜", humped["roll_yield_note"])

        # CN realtime term structure also computes annualized roll yield
        cn_curve = rd.summarize_term_structure([
            {"symbol": "LC2701", "trade": 91500.0},
            {"symbol": "LC2705", "trade": 90000.0},
            {"symbol": "LC2709", "trade": 88500.0},
        ])
        self.assertGreater(cn_curve["annualized_roll_yield_near_pct"], 0)
        self.assertGreater(cn_curve["annualized_roll_yield_curve_pct"], 0)

    def test_cross_market_spread_all_six_groups(self):
        crude = rext.compute_cross_market_spread(
            "crude_oil", {"CL=F": 82.77, "BZ=F": 89.83, "SC0": 585.9, "USDCNY=X": 6.6561}
        )
        self.assertAlmostEqual(crude["brent_minus_wti_usd_bbl"], 7.06, places=2)
        self.assertAlmostEqual(crude["sc_usd_bbl"], 585.9 / 6.6561, places=2)
        self.assertLess(crude["sc_minus_brent_usd_bbl"], 0)

        copper = rext.compute_cross_market_spread("copper", {"CU0": 92000.0, "HG=F": 5.5, "USDCNY=X": 6.65})
        self.assertAlmostEqual(copper["comex_usd_mt"], 5.5 * rext.LB_PER_MT, places=1)
        self.assertAlmostEqual(copper["shfe_usd_mt_ex_vat"], 92000.0 / 1.13 / 6.65, places=1)

        gold = rext.compute_cross_market_spread("gold", {"AU0": 770.0, "GC=F": 3569.2, "USDCNY=X": 6.6561})
        self.assertAlmostEqual(gold["comex_cny_g"], 3569.2 / rext.GRAMS_PER_TROY_OZ * 6.6561, places=2)
        self.assertIn("盘面交易报价不含增值税", gold["tax_note"])

        silver = rext.compute_cross_market_spread("silver", {"AG0": 10200.0, "SI=F": 41.875, "USDCNY=X": 6.6561})
        self.assertAlmostEqual(silver["comex_cny_kg_tax_incl"], 41.875 * rext.TROY_OZ_PER_KG * 6.6561 * 1.13, places=1)

        sugar = rext.compute_cross_market_spread("sugar", {"SR0": 5900.0, "SB=F": 17.15, "USDCNY=X": 6.6561})
        self.assertIn("15%配额内/50%配额外关税", sugar["basis_note"])
        self.assertGreater(sugar["ratio"], 1.0)

        cotton = rext.compute_cross_market_spread("cotton", {"CF0": 14000.0, "CT=F": 79.96, "USDCNY=X": 6.6561})
        self.assertIn("ICE美棉2号期货", cotton["basis_note"])
        self.assertNotIn("FOB", cotton["basis_note"])
        self.assertGreater(cotton["ice_futures_usd_mt"], 1500.0)

    def test_freight_cftc_tff_rate_diff_ashare_us_and_farside_eth_parsers(self):
        # 1) Freight parser handles newest-first ordering
        dates = pd.date_range("2026-08-01", periods=70, freq="B")[::-1]
        freight_df = pd.DataFrame({
            "截止日期": [d.strftime("%Y-%m-%d") for d in dates],
            "油轮运价指数原油运价指数BDTI": [4564.0 + i * 10.0 for i in range(70)],
            "波罗的海好望角型船运价指数BCI": [4500.0 - i * 5.0 for i in range(70)],
        })
        fr = rext.parse_freight_index_df(freight_df, ["BDTI", "BCI"])
        self.assertEqual(fr["BDTI"]["latest_value"], 4564.0)
        self.assertIsNotNone(fr["BDTI"]["chg_5d_pct"])
        self.assertIsNotNone(fr["BDTI"]["chg_60d_pct"])

        # 2) CFTC TFF inverse quote sign alignment (USDJPY=X) & strict contract_code refusal
        tff_csv = (
            '"JAPANESE YEN - CHICAGO MERCANTILE EXCHANGE",261110,2026-11-10,097741,CME,00,097,'
            '388875,19338,145649,1000,161322,70978,2000,89089,105944,3000\n'
        )
        jpy = rext.parse_cftc_tff(
            tff_csv,
            contract_code="097741",
            futures_direction="inverse",
            note="inverse",
        )
        self.assertEqual(jpy["leveraged_funds"]["net"], 89089 - 105944)  # -16855 short JPY futures
        self.assertEqual(jpy["pair_aligned_lev_money_net"], 16855)  # +16855 long USDJPY
        with self.assertRaises(ValueError):
            # Missing requested contract_code must NOT silently match another code via market_query
            rext.parse_cftc_tff(tff_csv, market_query=["JAPANESE YEN"], contract_code="999999")

        # 3) US-CN bond spread parser
        bond_df = pd.DataFrame([
            {"日期": "2026-11-18", "中国国债收益率10年": 1.59, "中国国债收益率2年": 1.23, "美国国债收益率10年": 5.07, "美国国债收益率2年": 4.80},
            {"日期": "2026-11-19", "中国国债收益率10年": 1.5755, "中国国债收益率2年": 1.2276, "美国国债收益率10年": 5.04, "美国国债收益率2年": 4.76},
        ])
        bd = rext.parse_bond_zh_us_rate_df(bond_df)
        self.assertAlmostEqual(bd["us_minus_cn_10y_bp"], (5.04 - 1.5755) * 100.0, places=1)
        self.assertAlmostEqual(bd["us_minus_cn_2y_bp"], (4.76 - 1.2276) * 100.0, places=1)

        # 4) A-share 5Y valuation percentiles: sparse (<60) returns None + note; >=min_sample_days computes & stable-dedups
        val_df = pd.DataFrame({
            "数据日期": ["2026-01-01", "2026-01-02", "2026-01-03", "2026-01-04", "2026-01-05", "2026-01-05"],
            "PE(TTM)": [-10.0, 10.0, 20.0, 30.0, 99.0, 20.0],
            "PE(静)": [10.0, 12.0, 18.0, 25.0, 19.0, 19.0],
            "市净率": [1.0, 2.0, 3.0, 4.0, 9.0, 2.0],
            "市销率": [5.0] * 6,
            "市现率": [8.0] * 6,
            "总市值": [1e11] * 6,
        })
        vp_sparse = rext.compute_ashare_valuation_percentiles(val_df)
        self.assertIsNone(vp_sparse["pe_ttm_5y_percentile"])
        self.assertIn("低于最小阈值", vp_sparse["pe_ttm_coverage_note"])
        self.assertEqual(vp_sparse["pe_ttm"], 20.0)  # stable dedup kept later row (20.0) over 99.0

        vp_ok = rext.compute_ashare_valuation_percentiles(val_df, min_sample_days=3)
        self.assertEqual(vp_ok["pe_ttm_positive_days"], 4)
        self.assertEqual(vp_ok["pe_ttm_5y_percentile"], 75.0)
        self.assertEqual(vp_ok["pb_5y_percentile"], 60.0)

        # 5) US valuation/positioning: filters out past dates & pd.NaT, falls back cleanly
        from datetime import date

        up = rext.extract_us_valuation_positioning(
            {"marketCap": 8e10, "forwardPE": 27.0, "shortPercentOfFloat": 0.0265, "enterpriseToEbitda": 25.1},
            {"Earnings Date": [date(2023, 5, 1), pd.NaT, date(2027, 2, 18), date(2027, 2, 22)]},
            ref_date=date(2026, 11, 20),
        )
        self.assertEqual(up["next_earnings_date"], "2027-02-18")
        self.assertAlmostEqual(up["short_percent_of_float"], 0.0265)

        up_nat = rext.extract_us_valuation_positioning(
            {"earningsTimestamp": 1900000000},  # 2030-03-17 UTC
            {"Earnings Date": pd.NaT},
            ref_date=date(2026, 11, 20),
        )
        self.assertEqual(up_nat["next_earnings_date"], "2030-03-17")

        # 6) Farside ETH ETF HTML parser: isolates partial report (unreported ETHA), guards 5d/20d & short rows
        eth_html = """
        <table>
          <tr><th></th><th>ETHA</th><th>FETH</th><th>ETHE</th><th>Total</th></tr>
          <tr><td>16 Nov 2026</td><td>10.0</td><td>5.0</td><td>0.0</td><td>15.0</td></tr>
          <tr><td>17 Nov 2026</td><td>(27.6)</td><td>0.0</td><td>0.0</td><td>(29.1)</td></tr>
          <tr><td>18 Nov 2026</td><td>(17.4)</td><td>0.0</td><td>(2.0)</td><td>(39.8)</td></tr>
          <tr><td>19 Nov 2026</td><td>-</td><td>0.0</td><td>(5.2)</td><td>(5.2)</td></tr>
          <tr><td>20 Nov 2026</td><td>12.3</td><td>4.5</td></tr>
          <tr><td>Total</td><td>11,166.9</td><td>2,435.9</td><td>(5,541.6)</td><td>11,469.7</td></tr>
        </table>"""
        eth_res = rext.parse_farside_etf_html(eth_html, asset="ETH")
        self.assertEqual(eth_res["asset"], "ETH")
        # 19 Nov has missing ETHA (partial); 20 Nov is a malformed short row (latest_partial_report); latest complete is 18 Nov (-39.8)
        self.assertEqual(eth_res["latest_date"], "18 Nov 2026")
        self.assertAlmostEqual(eth_res["latest_total_usd_m"], -39.8)
        self.assertIsNotNone(eth_res["latest_partial_report"])
        self.assertEqual(eth_res["latest_partial_report"]["date"], "20 Nov 2026")
        self.assertIn("MISSING_TOTAL_COLUMN", eth_res["latest_partial_report"]["unreported_funds"])
        nov19_row = next(r for r in eth_res["recent_daily"] if r["date"] == "19 Nov 2026")
        self.assertTrue(nov19_row["is_partial_report"])
        self.assertIn("ETHA", nov19_row["unreported_funds"])
        # Only 3 complete days -> rolling_5d and rolling_20d MUST be None (not 3-day sum!)
        self.assertIsNone(eth_res["rolling_5d_usd_m"])
        self.assertIsNone(eth_res["rolling_20d_usd_m"])
        self.assertAlmostEqual(eth_res["cumulative_total_usd_m"], 11469.7, places=1)

        # Short fallback page without explicit Total row must keep cumulative_total_usd_m = None
        short_html = """
        <table>
          <tr><th>Date</th><th>ETHA</th><th>FETH</th><th>Total</th></tr>
          <tr><td>17 Nov 2026</td><td>10.0</td><td>2.0</td><td>12.0</td></tr>
          <tr><td>18 Nov 2026</td><td>5.0</td><td>1.0</td><td>6.0</td></tr>
        </table>"""
        short_res = rext.parse_farside_etf_html(short_html, asset="ETH")
        self.assertIsNone(short_res["cumulative_total_usd_m"])
        self.assertIsNone(short_res["rolling_20d_usd_m"])

    def test_codex_round2_regressions_stale_curves_dxy_stdout_and_yf_isolation(self):
        from datetime import date
        import threading

        # 1) All-stale curve quotes (from last year) must be rejected against ref_date
        stale_df = pd.DataFrame({"Close": [80.0], "Volume": [100]}, index=pd.to_datetime(["2025-06-01"]))
        with patch.object(rext.yf, "Ticker") as mock_tk:
            mock_tk.return_value.history.return_value = stale_df
            with self.assertRaises(ValueError):
                rext.fetch_overseas_term_structure("CL=F", ref_date=date(2026, 11, 20))

        # 2) Public fetch_cn_daily_term_structure enforces whitelist BEFORE any AkShare call
        with patch.object(rext.ak, "futures_zh_daily_sina") as spy_sina:
            res_skip = rext.fetch_cn_daily_term_structure("IBM")
            self.assertEqual(res_skip["status"], "skipped")
            spy_sina.assert_not_called()

        # 3) TA0 daily curve includes active front TA2612 on 2026-11-20
        ta_called: list[str] = []

        def fake_sina_daily(symbol):
            ta_called.append(symbol)
            return pd.DataFrame({"date": ["2026-11-19"], "close": [5800.0], "volume": [1000]})

        with patch.object(rext.ak, "futures_zh_daily_sina", side_effect=fake_sina_daily):
            ta_res = rext.fetch_cn_daily_term_structure("TA", ref_date=date(2026, 11, 20))
        self.assertIn("TA2612", ta_called)
        self.assertIn("TA2701", ta_called)
        self.assertEqual(ta_res["curve_sampling"], "sampled_active_contracts")

        # 4) Cross-market spread rejects stale re-fetched legs (>5d vs ref_date) uniformly
        with (
            patch.object(rext, "_fetch_latest_yf_close", side_effect=[(82.0, "2026-11-19"), (89.0, "2026-11-19"), (6.65, "2026-11-19")]),
            patch.object(rext, "_fetch_latest_sina_main_close", return_value=(580.0, "2026-11-10")),
        ):
            sp_partial = rext.fetch_cross_market_spread(
                "CL=F",
                known_prices={"SC0": 500.0},
                known_dates={"SC0": "2026-08-20"},
                ref_date=date(2026, 11, 20),
            )
        self.assertEqual(sp_partial["status"], "partial")
        self.assertIn("SC0", sp_partial["stale_legs"])
        self.assertIsNone(sp_partial["sc_usd_bbl"])
        self.assertAlmostEqual(sp_partial["brent_minus_wti_usd_bbl"], 7.0, places=2)

        # All legs stale (6d old: 2026-11-14 vs 2026-11-20) must fail rather than return ok
        with (
            patch.object(rext, "_fetch_latest_yf_close", side_effect=[(5.5, "2026-11-14"), (6.65, "2026-11-14")]),
            patch.object(rext, "_fetch_latest_sina_main_close", return_value=(92000.0, "2026-11-14")),
        ):
            with self.assertRaises(ValueError):
                rext.fetch_cross_market_spread("CU0", ref_date=date(2026, 11, 20))

        # 5) DXY (asset_class='macro') still reaches cftc_fx_cot & rate_differential in collect_research_data
        with (
            patch.object(rd, "_call_get_snapshot", return_value={"symbol": "DX-Y.NYB", "asset_class": "macro"}),
            patch.object(rext, "fetch_cftc_fx_cot", return_value={"contract_code": "098662"}) as spy_dxy_cot,
            patch.object(rext, "fetch_fx_rate_differential", return_value={"us_2s10s_bp": 28.0}) as spy_dxy_rate,
        ):
            dxy_out = rd.collect_research_data("DXY", include_macro=False)
        self.assertEqual(dxy_out["asset_class"], "macro")
        self.assertEqual(dxy_out["fundamentals_and_supply"]["cftc_fx_cot"]["status"], "ok")
        self.assertEqual(dxy_out["fundamentals_and_supply"]["rate_differential"]["status"], "ok")
        spy_dxy_cot.assert_called_once_with("DX-Y.NYB")
        spy_dxy_rate.assert_called_once_with("DX-Y.NYB")

        # 6) Shared stdout filter between rd & rext restores sys.stdout cleanly under overlapping threads
        self.assertIs(rd._run_with_timeout, rext._run_with_timeout)
        orig_out = sys.stdout
        ev1_in = threading.Event()
        ev2_in = threading.Event()
        ev1_go = threading.Event()
        ev2_go = threading.Event()

        def w1():
            ev1_in.set()
            ev1_go.wait(2)
            return "w1"

        def w2():
            ev2_in.set()
            ev2_go.wait(2)
            return "w2"

        t_a = threading.Thread(target=lambda: rd._run_with_timeout(w1, timeout_s=3, silence_stdout=True))
        t_b = threading.Thread(target=lambda: rext._run_with_timeout(w2, timeout_s=3, silence_stdout=True))
        t_a.start()
        ev1_in.wait(2)
        t_b.start()
        ev2_in.wait(2)
        ev1_go.set()
        t_a.join(2)
        ev2_go.set()
        t_b.join(2)
        self.assertIs(sys.stdout, orig_out)

        # 7) Calendar failure in yfinance does NOT wipe out valid info valuation/short-interest metrics
        class FakeTicker:
            earnings_estimate = pd.DataFrame()
            revenue_estimate = pd.DataFrame()
            eps_trend = pd.DataFrame()
            analyst_price_targets = {"mean": 700.0}
            quarterly_income_stmt = pd.DataFrame()
            quarterly_cashflow = pd.DataFrame()
            info = {"marketCap": 8e10, "forwardPE": 27.0, "shortPercentOfFloat": 0.03}

            @property
            def calendar(self):
                raise RuntimeError("Yahoo calendar 500")

        with patch.object(rd.yf, "Ticker", return_value=FakeTicker()):
            yf_out = rd.fetch_yfinance_consensus("PWR")
        vp_sec = yf_out["valuation_and_positioning"]
        self.assertEqual(vp_sec["forward_pe"], 27.0)
        self.assertEqual(vp_sec["short_percent_of_float"], 0.03)
        self.assertIn("Yahoo calendar 500", vp_sec["calendar_error"])
        self.assertEqual(yf_out["status"], "partial")

    def test_codex_round3_regressions_btc_strict_expiry_front_alignment_and_short_farside(self):
        from datetime import date

        # 1) BTC production parser delegates to strict parser: isolates unreported IBIT day & rejects interior gap
        btc_partial_html = """
        <table>
          <tr><th>Date</th><th>IBIT</th><th>FBTC</th><th>GBTC</th><th>Total</th></tr>
          <tr><td>11 Nov 2026</td><td>(100.0)</td><td>0.0</td><td>0.0</td><td>(100.0)</td></tr>
          <tr><td>12 Nov 2026</td><td>(200.0)</td><td>0.0</td><td>0.0</td><td>(200.0)</td></tr>
          <tr><td>13 Nov 2026</td><td>(150.0)</td><td>0.0</td><td>0.0</td><td>(150.0)</td></tr>
          <tr><td>16 Nov 2026</td><td>50.0</td><td>0.0</td><td>0.0</td><td>50.0</td></tr>
          <tr><td>17 Nov 2026</td><td>(250.0)</td><td>0.0</td><td>0.0</td><td>(250.0)</td></tr>
          <tr><td>18 Nov 2026</td><td>(300.0)</td><td>0.0</td><td>0.0</td><td>(300.0)</td></tr>
          <tr><td>19 Nov 2026</td><td>-</td><td>(40.0)</td><td>(10.0)</td><td>(50.0)</td></tr>
          <tr><td>Total</td><td>63,911</td><td>10,037</td><td>(28,235)</td><td>54,121</td></tr>
        </table>"""
        btc_out = rd.parse_farside_btc_etf_html(btc_partial_html, last_n=5)
        self.assertEqual(btc_out["latest_date"], "18 Nov 2026")
        self.assertEqual(btc_out["latest_partial_report"]["date"], "19 Nov 2026")
        # 5 latest complete days = 12..18 Nov (-200 -150 +50 -250 -300 = -850.0), excluding partial 19 Nov (-50)
        self.assertAlmostEqual(btc_out["recent_total_net_flow_musd"], -850.0, places=2)
        self.assertAlmostEqual(btc_out["rolling_5d_usd_m"], -850.0, places=2)

        # 2) Contiguous rolling window must NOT skip over an interior partial day inside last 5 trading days
        gap_html = """
        <table>
          <tr><th>Date</th><th>ETHA</th><th>FETH</th><th>ETHE</th><th>Total</th></tr>
          <tr><td>10 Nov 2026</td><td>10.0</td><td>0.0</td><td>0.0</td><td>10.0</td></tr>
          <tr><td>11 Nov 2026</td><td>10.0</td><td>0.0</td><td>0.0</td><td>10.0</td></tr>
          <tr><td>12 Nov 2026</td><td>10.0</td><td>0.0</td><td>0.0</td><td>10.0</td></tr>
          <tr><td>13 Nov 2026</td><td>-</td><td>0.0</td><td>0.0</td><td>0.0</td></tr>
          <tr><td>16 Nov 2026</td><td>10.0</td><td>0.0</td><td>0.0</td><td>10.0</td></tr>
          <tr><td>17 Nov 2026</td><td>10.0</td><td>0.0</td><td>0.0</td><td>10.0</td></tr>
          <tr><td>18 Nov 2026</td><td>10.0</td><td>0.0</td><td>0.0</td><td>10.0</td></tr>
        </table>"""
        gap_res = rext.parse_farside_etf_html(gap_html, asset="ETH")
        self.assertEqual(gap_res["latest_date"], "18 Nov 2026")
        self.assertIsNone(gap_res["rolling_5d_usd_m"])
        self.assertIn("存在未齐报日", gap_res["coverage_note"])

        # 3) Farside short page two-row header ('Total' on row 0, tickers on row 1 with empty last cell) parses cleanly
        short_two_row_html = """
        <table>
          <tr><th>&nbsp;</th><th></th><th></th><th></th><th>&nbsp;&nbsp;Total</th></tr>
          <tr><th></th><th>ETHA</th><th>FETH</th><th>ETHE</th><th></th></tr>
          <tr><td>17 Nov 2026</td><td>(10.0)</td><td>2.0</td><td>0.0</td><td>(8.0)</td></tr>
          <tr><td>18 Nov 2026</td><td>5.0</td><td>1.0</td><td>(1.0)</td><td>5.0</td></tr>
          <tr><td>Total</td><td>100.0</td><td>50.0</td><td>(20.0)</td><td>130.0</td></tr>
        </table>"""
        short_two = rext.parse_farside_etf_html(short_two_row_html, asset="ETH")
        self.assertEqual(short_two["latest_date"], "18 Nov 2026")
        self.assertAlmostEqual(short_two["latest_total_usd_m"], 5.0)
        self.assertAlmostEqual(short_two["cumulative_total_usd_m"], 130.0)
        self.assertIsNone(short_two["rolling_20d_usd_m"])

        # 4) SC2612 active on Nov 26 (expires Nov 30) & TA2612 active on Dec 11 (10th bday Dec 14)
        self.assertEqual(rext.estimate_contract_last_trading_day("SC", 2026, 12), date(2026, 11, 30))
        self.assertEqual(rext.estimate_contract_last_trading_day("TA", 2026, 12), date(2026, 12, 14))
        seen_sc: list[str] = []
        with patch.object(
            rext.ak,
            "futures_zh_daily_sina",
            side_effect=lambda symbol: (
                seen_sc.append(symbol)
                or pd.DataFrame({"date": ["2026-11-26"], "close": [580.0], "volume": [1000]})
            ),
        ):
            rext.fetch_cn_daily_term_structure("SC", ref_date=date(2026, 11, 26))
        self.assertIn("SC2612", seen_sc)

        # 5) Date alignment must NOT drop active front leg when front is 11-20 & back legs are 11-19
        def fake_ta_misaligned(symbol):
            if symbol == "TA2612":
                return pd.DataFrame({"date": ["2026-11-20"], "close": [5900.0], "volume": [500]})
            return pd.DataFrame({"date": ["2026-11-19"], "close": [5800.0], "volume": [5000]})

        with patch.object(rext.ak, "futures_zh_daily_sina", side_effect=fake_ta_misaligned):
            ta_mis = rext.fetch_cn_daily_term_structure("TA", ref_date=date(2026, 11, 20))
        self.assertEqual(ta_mis["front_contract"], "TA2612")
        self.assertEqual(ta_mis["status"], "partial")
        self.assertFalse(ta_mis["quote_dates_aligned"])
        self.assertIsNone(ta_mis["front_second_spread"])
        self.assertIsNone(ta_mis["annualized_roll_yield_near_pct"])
        self.assertIsNone(ta_mis["annualized_roll_yield_curve_pct"])

        # 6) Explicit ref_date rejects future lookahead bar (2026-11-21 when ref_date=2026-11-20)
        fut_df = pd.DataFrame(
            {"Close": [80.0, 95.0], "Volume": [100, 100]},
            index=pd.to_datetime(["2026-11-20", "2026-11-21"]),
        )
        bars_explicit = rext._extract_fresh_yf_history(fut_df, anchor=date(2026, 11, 20), allow_next_day_tz=False)
        self.assertIn(date(2026, 11, 20), bars_explicit)
        self.assertNotIn(date(2026, 11, 21), bars_explicit)

    def test_codex_round4_regressions_holidays_missing_front_etf_holes_sina_date_and_last_n(self):
        from datetime import date

        # 1) Exact holiday-aware CME/ICE last trading days & China National Day TA2610 survival
        self.assertEqual(rext.estimate_contract_last_trading_day("BZ", 2027, 2), date(2026, 12, 30))
        self.assertEqual(rext.estimate_contract_last_trading_day("KC", 2026, 12), date(2026, 12, 18))
        self.assertEqual(rext.estimate_contract_last_trading_day("CT", 2026, 12), date(2026, 12, 8))
        self.assertEqual(rext.estimate_contract_last_trading_day("CL", 2026, 12), date(2026, 11, 20))

        # On 2026-10-16 (post National Day Oct 1-7), TA2610 still actively trades -> retained as front!
        def fake_ta_oct(symbol):
            return pd.DataFrame({"date": ["2026-10-16"], "close": [5500.0], "volume": [1000]})

        with patch.object(rext.ak, "futures_zh_daily_sina", side_effect=fake_ta_oct):
            ta_oct = rext.fetch_cn_daily_term_structure("TA", ref_date=date(2026, 10, 16))
        self.assertEqual(ta_oct["front_contract"], "TA2610")
        self.assertNotIn("status", ta_oct)  # ok (not partial)

        # 2) Missing active front contract (TA2612 stale -> curve starts at TA2701) MUST mark status='partial'
        def fake_ta_stale_front(symbol):
            if symbol == "TA2612":
                return pd.DataFrame({"date": ["2026-10-01"], "close": [5500.0], "volume": [100]})  # stale
            return pd.DataFrame({"date": ["2026-11-19"], "close": [5800.0], "volume": [5000]})

        with patch.object(rext.ak, "futures_zh_daily_sina", side_effect=fake_ta_stale_front):
            ta_missing = rext.fetch_cn_daily_term_structure("TA", ref_date=date(2026, 11, 20))
        self.assertEqual(ta_missing["status"], "partial")
        self.assertEqual(ta_missing["missing_front_contract"], "TA2612")
        self.assertEqual(ta_missing["front_contract"], "TA2701")

        # Overseas missing active front (CLF27 empty) also marks status='partial'
        def fake_yf_missing_front(ticker):
            class _T:
                def history(self, period="10d"):
                    if ticker == "CLF27.NYM":
                        return pd.DataFrame()
                    return pd.DataFrame({"Close": [80.0], "Volume": [1000]}, index=pd.to_datetime(["2026-11-21"]))
            return _T()

        with patch.object(rext.yf, "Ticker", side_effect=fake_yf_missing_front):
            cl_missing = rext.fetch_overseas_term_structure("CL=F", ref_date=date(2026, 11, 21))
        self.assertEqual(cl_missing["status"], "partial")
        self.assertEqual(cl_missing["missing_front_contract"], "CLF27.NYM")

        # 3) Contiguous ETF rolling 5d & 20d windows must NOT bridge over interior short row OR all-'-' Total=0 row
        base_dates = pd.bdate_range("2026-10-15", periods=25)
        short_rows = []
        zero_rows = []
        for idx, dt_ts in enumerate(base_dates):
            ds = dt_ts.strftime("%d %b %Y")
            if idx == 22:  # 3rd from end -> inside BOTH 5d and 20d windows
                short_rows.append(f"<tr><td>{ds}</td><td>10.0</td><td>0.0</td></tr>")
                zero_rows.append(f"<tr><td>{ds}</td><td>-</td><td>-</td><td>-</td><td>0.0</td></tr>")
            else:
                row_html = f"<tr><td>{ds}</td><td>10.0</td><td>0.0</td><td>0.0</td><td>10.0</td></tr>"
                short_rows.append(row_html)
                zero_rows.append(row_html)
        hdr = "<table><tr><th>Date</th><th>ETHA</th><th>FETH</th><th>ETHE</th><th>Total</th></tr>"
        short_hole_res = rext.parse_farside_etf_html(hdr + "".join(short_rows) + "</table>", asset="ETH")
        zero_hole_res = rext.parse_farside_etf_html(hdr + "".join(zero_rows) + "</table>", asset="ETH")
        self.assertIsNone(short_hole_res["rolling_5d_usd_m"])
        self.assertIsNone(short_hole_res["rolling_20d_usd_m"])
        self.assertIsNone(zero_hole_res["rolling_5d_usd_m"])
        self.assertIsNone(zero_hole_res["rolling_20d_usd_m"])
        self.assertIn("存在未齐报日", zero_hole_res["coverage_note"])

        # 4) Sina cross-market selection picks 2026-11-20 even when >30 newer rows follow it
        future_dates = pd.bdate_range("2026-11-21", periods=40)
        sina_rows = [{"日期": "2026-11-19", "收盘价": 580.0}, {"日期": "2026-11-20", "收盘价": 586.0}] + [
            {"日期": ts.strftime("%Y-%m-%d"), "收盘价": 600.0 + i} for i, ts in enumerate(future_dates)
        ]
        sina_df = pd.DataFrame(sina_rows)
        with patch.object(rext.ak, "futures_main_sina", return_value=sina_df):
            px_sc, dt_sc = rext._fetch_latest_sina_main_close("SC0", ref_date=date(2026, 11, 20))
        self.assertEqual(dt_sc, "2026-11-20")
        self.assertAlmostEqual(px_sc, 586.0)

        # 5) Stale yfinance leg (USDCNY 6d old) recorded in stale_legs (NOT leg_errors) consistently with Sina
        def fake_yf_stale_fx(ticker):
            class _T:
                def history(self, period="1mo"):
                    if ticker == "USDCNY=X":
                        return pd.DataFrame({"Close": [6.65]}, index=pd.to_datetime(["2026-11-14"]))
                    px = 82.0 if ticker == "CL=F" else 89.0
                    return pd.DataFrame({"Close": [px]}, index=pd.to_datetime(["2026-11-20"]))
            return _T()

        with (
            patch.object(rext.yf, "Ticker", side_effect=fake_yf_stale_fx),
            patch.object(rext, "_fetch_latest_sina_main_close", return_value=(586.0, "2026-11-20")),
        ):
            sp_fx_stale = rext.fetch_cross_market_spread("CL=F", ref_date=date(2026, 11, 20))
        self.assertEqual(sp_fx_stale["status"], "partial")
        self.assertIn("USDCNY=X", sp_fx_stale["stale_legs"])
        self.assertNotIn("USDCNY=X", sp_fx_stale.get("leg_errors") or {})

        # 6) BTC compat parser honors last_n > 10 (e.g. last_n=11 on 15 complete days) without 10-row truncation
        rows_15 = "\n".join(
            f"<tr><td>{d:02d} Nov 2026</td><td>10.0</td><td>0.0</td><td>0.0</td><td>10.0</td></tr>"
            for d in range(1, 16)
        )
        html_15 = f"<table><tr><th>Date</th><th>IBIT</th><th>FBTC</th><th>GBTC</th><th>Total</th></tr>{rows_15}</table>"
        btc_11 = rd.parse_farside_btc_etf_html(html_15, last_n=11)
        self.assertEqual(btc_11["recent_days_count"], 11)
        self.assertAlmostEqual(btc_11["recent_total_net_flow_musd"], 110.0, places=2)

    def test_strict_zero_waste_routing_across_asset_classes(self):
        # 1) US stock (NVDA): ZERO commodity/freight/FX calls
        with (
            patch.object(rd, "_call_get_snapshot", return_value={"symbol": "NVDA", "asset_class": "us_stock_or_etf"}),
            patch.object(rd, "fetch_sec_edgar", return_value={"cik": "0001045810"}),
            patch.object(rd, "fetch_yfinance_consensus", return_value={"analyst_price_targets": {}}),
            patch.object(rext, "fetch_freight_indices") as spy_freight,
            patch.object(rext, "fetch_cross_market_spread") as spy_spread,
            patch.object(rext, "fetch_overseas_term_structure") as spy_oterm,
            patch.object(rext, "fetch_cftc_fx_cot") as spy_fx,
        ):
            out = rd.collect_research_data("NVDA", include_macro=False)
            self.assertNotIn("baltic_freight", out["fundamentals_and_supply"])
            self.assertNotIn("cross_market_spread", out["fundamentals_and_supply"])
            spy_freight.assert_not_called()
            spy_spread.assert_not_called()
            spy_oterm.assert_not_called()
            spy_fx.assert_not_called()

        # 2) Domestic-only CN future (LH0 hogs): ZERO freight & ZERO cross-market spread calls
        with (
            patch.object(rd, "_call_get_snapshot", return_value={"symbol": "LH0", "asset_class": "cn_future"}),
            patch.object(rd, "fetch_cn_future_inventory", return_value={"latest_inventory": 100.0}),
            patch.object(rd, "fetch_cn_future_spot_basis", return_value={"spot_price": 15000.0}),
            patch.object(rd, "fetch_cn_future_term_structure", return_value={"structure": "Backwardation"}),
            patch.object(rext, "fetch_freight_indices") as spy_freight,
            patch.object(rext, "fetch_cross_market_spread") as spy_spread,
        ):
            out = rd.collect_research_data("LH0", include_macro=False)
            self.assertNotIn("baltic_freight", out["fundamentals_and_supply"])
            self.assertNotIn("cross_market_spread", out["fundamentals_and_supply"])
            spy_freight.assert_not_called()
            spy_spread.assert_not_called()

        # 3) Copper (CU0): calls cross_market_spread ONLY, ZERO freight calls
        with (
            patch.object(rd, "_call_get_snapshot", return_value={"symbol": "CU0", "asset_class": "cn_future"}),
            patch.object(rd, "fetch_cn_future_inventory", return_value={"latest_inventory": 50000.0}),
            patch.object(rd, "fetch_cn_future_spot_basis", return_value={"spot_price": 92000.0}),
            patch.object(rd, "fetch_cn_future_term_structure", return_value={"structure": "Contango"}),
            patch.object(rext, "fetch_cross_market_spread", return_value={"group": "copper"}) as spy_spread,
            patch.object(rext, "fetch_freight_indices") as spy_freight,
        ):
            out = rd.collect_research_data("CU0", include_macro=False)
            self.assertIn("cross_market_spread", out["fundamentals_and_supply"])
            self.assertNotIn("baltic_freight", out["fundamentals_and_supply"])
            spy_spread.assert_called_once()
            self.assertEqual(spy_spread.call_args[0][0], "CU0")
            spy_freight.assert_not_called()

        # 4) Iron ore (I0): calls freight (BCI/BDI) ONLY, ZERO cross_market_spread calls
        with (
            patch.object(rd, "_call_get_snapshot", return_value={"symbol": "I0", "asset_class": "cn_future"}),
            patch.object(rd, "fetch_cn_future_inventory", return_value={"latest_inventory": 1000.0}),
            patch.object(rd, "fetch_cn_future_spot_basis", return_value={"spot_price": 800.0}),
            patch.object(rd, "fetch_cn_future_term_structure", return_value={"structure": "Backwardation"}),
            patch.object(rext, "fetch_cross_market_spread") as spy_spread,
            patch.object(rext, "fetch_freight_indices", return_value={"requested_keys": ["BCI", "BDI"]}) as spy_freight,
        ):
            out = rd.collect_research_data("I0", include_macro=False)
            self.assertIn("baltic_freight", out["fundamentals_and_supply"])
            self.assertNotIn("cross_market_spread", out["fundamentals_and_supply"])
            spy_freight.assert_called_once_with("I0")
            spy_spread.assert_not_called()

        # 5) FX (USDJPY=X & USDCNY=X): dedicated FX routing, ZERO commodity/freight calls
        with (
            patch.object(rd, "_call_get_snapshot", return_value={"symbol": "USDJPY=X", "asset_class": "fx"}),
            patch.object(rext, "fetch_cftc_fx_cot", return_value={"pair_aligned_lev_money_net": 16855}) as spy_cot,
            patch.object(rext, "fetch_fx_rate_differential", return_value={"base_minus_quote_10y_bp": 230.0}) as spy_rate,
            patch.object(rext, "fetch_freight_indices") as spy_freight,
        ):
            out = rd.collect_research_data("USDJPY=X", include_macro=False)
            self.assertEqual(out["fundamentals_and_supply"]["cftc_fx_cot"]["status"], "ok")
            self.assertEqual(out["fundamentals_and_supply"]["rate_differential"]["status"], "ok")
            spy_cot.assert_called_once_with("USDJPY=X")
            spy_rate.assert_called_once_with("USDJPY=X")
            spy_freight.assert_not_called()

        with (
            patch.object(rd, "_call_get_snapshot", return_value={"symbol": "USDCNY=X", "asset_class": "fx"}),
            patch.object(rext, "fetch_cftc_fx_cot") as spy_cot,
            patch.object(rext, "fetch_fx_rate_differential", return_value={"us_minus_cn_10y_bp": 346.5}) as spy_rate,
        ):
            out = rd.collect_research_data("USDCNY=X", include_macro=False)
            self.assertNotIn("cftc_fx_cot", out["fundamentals_and_supply"])
            self.assertEqual(out["fundamentals_and_supply"]["rate_differential"]["status"], "ok")
            spy_cot.assert_not_called()
            spy_rate.assert_called_once_with("USDCNY=X")

        # 6) Crypto ETH: calls farside_eth_etf, NOT farside_btc_etf
        with (
            patch.object(rd, "_call_get_snapshot", return_value={"symbol": "ETH-USD", "asset_class": "crypto"}),
            patch.object(rd, "fetch_defillama_stablecoins", return_value={"total_circulating_usd": 200e9}),
            patch.object(rd, "fetch_crypto_exchange_oi", return_value={"open_interest_value_usd": 1e9}),
            patch.object(rd, "fetch_farside_btc_etf_flows") as spy_btc,
            patch.object(rext, "fetch_farside_etf_flows", return_value={"asset": "ETH", "latest_total_usd_m": -5.2}) as spy_eth,
        ):
            out = rd.collect_research_data("ETH", include_macro=False)
            self.assertEqual(out["fundamentals_and_supply"]["farside_eth_etf"]["status"], "ok")
            self.assertNotIn("farside_btc_etf", out["fundamentals_and_supply"])
            spy_btc.assert_not_called()
            spy_eth.assert_called_once_with("ETH")


if __name__ == "__main__":
    unittest.main()
