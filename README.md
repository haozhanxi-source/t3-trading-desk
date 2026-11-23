# 中低频投研工作台 (T3 Trading Desk · 缠论版)

直接在 T3 Code 内部运行的多市场（美股 / A股 / 大宗商品 8 大类 / 外汇 FX / 加密货币）中长线投研系统，着眼于**未来 1 个季度至 1 年（1Q ~ 1Y）的宏观与产业头寸机会**（股票以做多为主，商品、外汇与加密多空双向对称），交易架构遵循**日线定方向 + 30min 缠论结构诞生交易**（大小级别区间套联立，压缩日线噪点浮亏损耗）、**单一板块只做真龙头**（从源头控制相关性）、**商品大 B 结构劈叉照样可空**与**持仓每日必检、逻辑松动果断撤离**四条实战铁律。

## 四层投研与执行架构（第三层与第四层严格分离）

1. **第一层 · 自上而下宏观与 7 维驱动推演**：以中美宏观双核、地缘与资源国政策、技术迭代、商品供需平衡预期等 7 大维度形成至少双维度共振，按需展开产业链并锁定**单一板块真正龙头与引领标的**（严禁盲扫 K 线倒推逻辑，跟风二三线一律不碰不看）。
2. **第二层 · 日线级别缠论定方向与过筛**：在 3 年日线样本上检验严格笔、笔中枢与九段升级段中枢、一/二/三类买卖点与资金方向一致性，确立大方向、主中枢 `[ZD, ZG]` 与空间，输出做多、做空、配对与逻辑待观察池。
3. **第三层 · 纯深度研究 + 独立红蓝对抗（不涉及入场与仓位）**：聚焦 1Q~1Y 基本面预期差 / 供需平衡表 / 利差周期，通过免费硬数据核验真实库存、基差月差、跨市价差、海运费与持仓结构，执行独立反方红蓝辩论，只输出**客观研究裁决**（方向、置信度、`L1~L4` 证据表、关键风险与证伪条件）——**不决定入场点、合约选择或仓位大小**。
4. **第四层 · 交易员 + 风控官 / 持仓管理（5 大职责）**：结合第三层纯研究裁决、第二层日线大方向与 **30min 缠论精确入场**落地执行：
   - **四象限终裁**：放行做多 / 放行做空 / 同板块配对 / 逻辑待观察 / 否决；
   - **最优交易表达与合约选择**：商品显式识别**“短期低库存/大 B 结构（强现实）vs 未来过剩（弱预期）”劈叉（B 结构绝非做空禁区）**，量化展期成本并在近远月、跨市价差、直盘/交叉盘或期权间择优；
   - **日线定方向 + 30min 缠论落地与仓位预算**：在 **30min 缠论结构**中捕捉精确买卖点与紧凑战术止损（减少日线噪点浮亏损耗），配合日线 `ZD/ZG` 结构失效位、盈亏比 `>= 2.5` 与风险预算算仓；
   - **单一板块龙头独占制与事件纪律**：同一板块只留 1 只真正龙头（仅在龙头切换时换仓，或第一第二不明显时最多 2 只共享板块风险额度），叠加跨板块宏大因子检查及移仓 / OPEC+ / WASDE / 议息 / 财报事件规则；
   - **持仓每日强制监控与果断撤离**：观察池按需轻量刷新，**已持有标的每日必检**（30min/日线防线 + 核心基本面/交易结构高频指标），核心逻辑或关键指标一旦松动随时果断撤离并复盘。

## 免费硬数据架构（按品种白名单精准路由，无关品种 0 调用、0 浪费）

`scripts/research_data.py` 严格按标的类型与全球联动属性分流拉取，单源故障自动隔离标陈旧：**查股票绝不调用海运费或商品期限结构，查纯内盘期货（如 `LH0`/`FG0`/`SA0`/`RB0`）绝不调用海外期限曲线、跨市价差或海运指数**。

- **美股**：SEC EDGAR（`10-K/10-Q` GAAP 财务 + RPO 原表链接）+ `yfinance` 一致预期、`eps_trend`、估值/空头比例（`shortPercentOfFloat`）与下次财报日
- **A 股**：同花顺财务摘要 + 机构盈利预测（`2026~2028`）+ **东财 5 年 PE(TTM)/PB 历史估值分位** + 沪深港通资金流
- **大宗商品（海外 + 国内期货 8 大类）**：
  - 国内：东财库存 + 生意社现货基差 + 期限结构与年化展期收益率（常规品种取新浪全挂牌合约，`SC0`/`TA0` 取活跃交割月抽样曲线）+ 国家统计局月度产量
  - 海外：CFTC 分类持仓 + EIA 原油/天然气周报 + USDA WASDE 官方 XML + **海外期货活跃合约抽样期限结构与年化展期收益率（Roll Yield）**
  - 跨市与航运（仅全球联动/海运依赖品种白名单）：**国内外/跨区价差与汇率/税率比价**（原油 `SC↔BZ/CL`、铜 `CU↔HG`、金银 `AU/AG↔GC/SI`、糖棉 `SR/CF↔SB/CT`）+ **波罗的海海运指数**（原油挂 `BDTI/BCTI`、铁矿焦煤挂 `BCI/BDI`、进口粮油糖挂 `BDI/BSI`、集运 `EC0` 挂全套）
- **外汇（FX）**：**CFTC 金融期货持仓（TFF `FinFutWk`，自动标注反向报价换算）** + **各国 10Y/中美每日国债利差（FRED + 东财中美国债收益率）**
- **加密货币**：DefiLlama 稳定币总市值 + OKX/Binance 永续 OI + **Farside BTC 与 ETH 现货 ETF 每日净流量** + 资金费率
- **宏观双核**：FRED（`DFII10`/`DGS10`/`T10YIE`/`FEDFUNDS`/`M2SL`/`PCOPPUSDM`）+ 中国 PMI/海关进出口

## 目录结构

- `AGENTS.md`：四层投研与执行完整工作流指令（第一层 7 维驱动 → 第二层日线缠论过筛 → 第三层纯深度研究与红蓝对抗 → 第四层交易表达与风控监控）
- `watchlist.json`：跨市场核心观察列表（分析与寻猎时支持任意跨市场标的实时拉取）
- `universes.json`：大宗商品 8 大类、航运、主流外汇（`fx_majors`）、美股与 A 股一级行业 ETF 定价锚（`us_sectors` / `a_shares_etfs`）及随投研沉淀的主题标的池（共 18 个基础池，支持通过 `--save-universe` 随行情按需生长）
- `scripts/chanlun.py`：多级别缠论核心引擎（支持日线定方向 + 30min/15min/60min 日内精确结构与无损时间戳、第 5 课连阴连阳笔修正、第 10 课前三笔笔中枢与九段升级段中枢、1~8 类笔强弱 + MACD 合参及 `momentum_reliable` 失真防护、3出/4出/首中枢三买信号、`assess_dual_timeframe_alignment` 双级别联立评估）
- `scripts/market_snapshot.py`：单标的行情与双级别缠论快照（默认 3 年日线，支持 `--interval 30m` 6 大类资产日内拉取、`--dual` 日线+30min 区间套联立与 `--full-chanlun` 全量导出）
- `scripts/scan_opportunities.py`：按自上而下推演出的板块池（`--universe`）或现场梳理的产业链龙头列表（`--symbols`）跑日线缠论过筛，并支持 `--save-universe` 沉淀新板块池
- `scripts/research_data.py`：按品种白名单精准路由的多资产免费硬数据聚合器（美股 SEC/yfinance、A 股财报/5 年估值分位、商品库存/基差/内外期限结构与 Roll Yield/跨市比价/波罗的海运价/CFTC/EIA/USDA、外汇 CFTC TFF/10Y 利差、加密稳定币/BTC+ETH ETF、FRED/中国宏观）
- `scripts/research_archive.py` & `research/schema.json`：按标的持续沉淀 `thesis.md`（内置股票 / 大宗商品 / 外汇三类第三层+第四层分离模板）/ `state.json`（含价格、期限结构、运价、价差、利差等多维触发器）/ `evidence.jsonl` 的增量研究与监控档案管理器
- `scripts/trade_planner.py`：第四层交易表达、大 B 结构劈叉权衡、四象限闸门与 30min 风险预算算仓器（内置 46 个国内外期货精确乘数表、支持 `--live` 实盘双级别联动）
- `scripts/portfolio_manager.py` & `portfolio/schema.json`：第四层单一板块龙头独占制持仓风控账本（单 SQLite 事务 `portfolio.db` + 原子镜像 JSON、龙头切换、双寡头共享额度、严禁浮亏摊平、减仓 `trim`、移动止损释风险；默认 `portfolio/` 运行态文件已 `.gitignore` 保护，如使用自定义 `--portfolio-dir` 请置于仓库外或补充忽略规则）
- `scripts/daily_sentinel.py`：第四层持仓每日哨兵（自动扫描所有 `holding` 持仓，每日双轨核查日线/30min 缠论与核心逻辑高频指标，输出 🔴 果断撤离 / 🟡 松动警戒 / 🟢 稳固持有）
- `tests/`：离线单元测试（`test_chanlun.py`、`test_market_and_scan.py`、`test_research_data.py`、`test_research_archive.py`、`test_layer4_planner_portfolio.py`、`test_daily_sentinel.py`）
- `research/`、`portfolio/` & `reports/`：单标的增量研究档案、本地持仓风控模板与分析报告

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

# 第三层：按品种白名单精准路由聚合免费硬数据（无关源 0 调用）
.venv/bin/python scripts/research_data.py PWR --full-chanlun
.venv/bin/python scripts/research_data.py LC0 --no-macro
.venv/bin/python scripts/research_data.py CL=F --no-macro
.venv/bin/python scripts/research_data.py USDJPY=X --no-macro

# 第三层 / 第四层：初始化与增量维护单标的投研档案（自动匹配股票 / 商品 / 外汇模板）
.venv/bin/python scripts/research_archive.py init PWR --name "Quanta Services" --subtype "A成长+B工程周期"
.venv/bin/python scripts/research_archive.py init USDJPY=X --name "美元日元" --subtype "G10利差"
.venv/bin/python scripts/research_archive.py list

# 第四层：双级别（日线定方向 + 30min 诞生交易）快照、交易表达算仓、龙头账本与持仓每日哨兵
.venv/bin/python scripts/market_snapshot.py CU0 --dual --no-macro
.venv/bin/python scripts/market_snapshot.py PWR --interval 30m
.venv/bin/python scripts/trade_planner.py --symbol CU0 --direction short --live --target 85000 --equity 1000000 --risk-pct 1.0
.venv/bin/python scripts/portfolio_manager.py init --equity 1000000 --base-currency CNY
.venv/bin/python scripts/portfolio_manager.py summary
.venv/bin/python scripts/daily_sentinel.py --format markdown --include-watchlist
```
