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
            patch.object(rd, "fetch_ashare_hsgt_flow", return_value={"northbound": [], "all_channels": []}),
        ):
            out = rd.collect_research_data("600519", include_macro=False)

        self.assertEqual(out["asset_class"], "a_share")
        self.assertEqual(out["fundamentals_and_supply"]["ths_financial_abstract"]["status"], "ok")
        self.assertEqual(out["fundamentals_and_supply"]["ths_profit_forecast"]["status"], "error")
        self.assertEqual(out["fundamentals_and_supply"]["hsgt_fund_flow"]["status"], "ok")

    def test_cn_future_blacklisted_inventory_and_term_skip(self):
        mock_snap = {"query": "SC0", "symbol": "SC0", "asset_class": "cn_future"}
        with (
            patch.object(rd, "_call_get_snapshot", return_value=mock_snap),
            patch.object(rd, "fetch_cn_future_spot_basis", return_value={"spot_code": "SC", "spot_price": 520.0}),
        ):
            out = rd.collect_research_data("SC0", include_macro=False)

        self.assertEqual(out["asset_class"], "cn_future")
        self.assertEqual(out["fundamentals_and_supply"]["inventory_em"]["status"], "skipped")
        self.assertEqual(out["fundamentals_and_supply"]["term_structure"]["status"], "skipped")
        self.assertEqual(out["fundamentals_and_supply"]["spot_and_basis"]["status"], "ok")

    def test_commodity_routing_with_wasde_and_isolation(self):
        mock_snap = {"query": "ZC=F", "symbol": "ZC=F", "asset_class": "commodity"}
        with (
            patch.object(rd, "_call_get_snapshot", return_value=mock_snap),
            patch.object(rd, "fetch_cftc_cot", return_value={"open_interest": 1979758}),
            patch.object(rd, "fetch_usda_wasde", return_value={"commodity": "Corn", "report_month": "November 2026"}),
        ):
            out = rd.collect_research_data("ZC=F", include_macro=False)

        self.assertEqual(out["fundamentals_and_supply"]["cftc_cot"]["status"], "ok")
        self.assertEqual(out["fundamentals_and_supply"]["eia"]["status"], "skipped")
        self.assertEqual(out["fundamentals_and_supply"]["usda_wasde"]["status"], "ok")

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


if __name__ == "__main__":
    unittest.main()
