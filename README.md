# 中低频投研工作台 (T3 Trading Desk · 缠论版)

直接在 T3 Code 内部运行的多市场（美股 / A股 / 大宗商品 8 大类 / 外汇 FX / 加密货币）中长线投研系统，着眼于**未来 1 个季度至 1 年（1Q ~ 1Y）的宏观与产业头寸机会**（股票以做多为主，商品、外汇与加密多空双向对称），交易结构与买卖点执行**严格锚定在日线级别 K 线缠论结构**（3 年日线样本、多中枢演化、一二三类买卖点、动力学背驰与资金方向一致性判别）。

## 目录结构

- `AGENTS.md`：1Q~1Y 大逻辑 + 第一层 7 维驱动框架 + 第二层多空双向日线缠论过筛与执行工作流指令
- `watchlist.json`：跨市场核心观察列表（分析与寻猎时支持任意跨市场标的实时拉取）
- `universes.json`：大宗商品 8 大类、航运、主流外汇（`fx_majors`）、美股与 A 股一级行业 ETF 定价锚（`us_sectors` / `a_shares_etfs`）及随投研沉淀的主题标的池（共 18 个基础池，支持通过 `--save-universe` 随行情按需生长）
- `scripts/chanlun.py`：日线缠论核心引擎（第 5 课连阴连阳笔修正、第 10 课前三笔笔中枢与九段升级段中枢、1~8 类笔强弱 + MACD 合参及 `momentum_reliable` 失真防护、3出/4出/首中枢三买信号）
- `scripts/market_snapshot.py`：单标的行情（默认 3 年日线，支持 `--full-chanlun` 全量导出、yfinance 与 akshare 国内期货主力连续）、日线缠论结构、基本面/资金费率与宏观数据抓取
- `scripts/scan_opportunities.py`：按自上而下推演出的板块池（`--universe`）或现场梳理的产业链龙头列表（`--symbols`）跑日线缠论过筛，并支持 `--save-universe` 沉淀新板块池
- `scripts/research_data.py`：第三层免费硬数据统一聚合器（SEC EDGAR、yfinance 预期趋势、同花顺财报/预测、东财库存/生意社基差/月差、统计局月度产量、CFTC、EIA、USDA、FRED、DefiLlama/Farside ETF）
- `scripts/research_archive.py` & `research/schema.json`：按标的持续沉淀 `thesis.md` / `state.json` / `evidence.jsonl` 的增量研究档案管理器
- `tests/`：离线单元测试（`test_chanlun.py`、`test_market_and_scan.py`、`test_research_data.py`、`test_research_archive.py`）
- `research/` & `reports/`：单标的增量研究档案与分析报告

## 快速测试

```bash
# 运行单元测试
.venv/bin/python -m unittest discover -s tests -v

# 查看单个标的行情与 3 年日线缠论快照（美股 / 海外商品 / 国内期货 / 外汇 / 加密 / A股）
.venv/bin/python scripts/market_snapshot.py NVDA
.venv/bin/python scripts/market_snapshot.py 黄金
.venv/bin/python scripts/market_snapshot.py 铁矿 --no-macro
.venv/bin/python scripts/market_snapshot.py 美元日元 --no-macro
.venv/bin/python scripts/market_snapshot.py BTC
.venv/bin/python scripts/market_snapshot.py 600519

# 扫描跨市场自选观察列表（默认读 watchlist.json）
.venv/bin/python scripts/scan_opportunities.py

# 查看所有可用标的池并按池批量扫描（支持按信号分层排序）
.venv/bin/python scripts/scan_opportunities.py --list-universes
.venv/bin/python scripts/scan_opportunities.py --universe commodities_ferrous fx_majors us_power_grid --sort tier
.venv/bin/python scripts/scan_opportunities.py --symbols NVDA 螺纹 KC=F 欧元美元 BTC --sort tier

# 当第一层自上而下想到新细分板块时，现场切入验证并按需沉淀为新标的池
.venv/bin/python scripts/scan_opportunities.py --symbols 300308 300502 300394 601138 --save-universe a_shares_optical_ai --description "A股光模块与AI算力硬件龙头" --sort tier

# 第三层：一键聚合免费硬数据（美股 SEC+预期 / 国内期货库存基差月差+统计局 / 海外商品 CFTC+EIA+USDA / 宏观 FRED）
.venv/bin/python scripts/research_data.py PWR --full-chanlun
.venv/bin/python scripts/research_data.py LC0 --no-macro
.venv/bin/python scripts/research_data.py CL=F --no-macro

# 第三层：初始化与增量维护单标的投研档案
.venv/bin/python scripts/research_archive.py init PWR --name "Quanta Services" --subtype "A成长+B工程周期"
.venv/bin/python scripts/research_archive.py list
```
