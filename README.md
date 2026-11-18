# 中低频投研工作台 (T3 Trading Desk)

直接在 T3 Code 内部运行的多市场（美股 / A股 / 加密货币 / 大宗商品）中低频投研系统，采用轻量数据脚本 + T3 Code 原生多智能体协作（技术面、基本面、新闻宏观、多空辩论、风控决策）。

## 目录结构

- `AGENTS.md`：多智能体投研工作流指令
- `watchlist.json`：跨市场自选观察列表
- `scripts/market_snapshot.py`：单标的行情、技术指标、基本面/资金费率与宏观数据抓取
- `scripts/scan_opportunities.py`：跨市场自选股/商品/加密中低频信号扫描
- `reports/`：保存生成的分析报告

## 快速测试

```bash
# 查看单个标的数据快照
.venv/bin/python scripts/market_snapshot.py NVDA
.venv/bin/python scripts/market_snapshot.py 黄金
.venv/bin/python scripts/market_snapshot.py BTC
.venv/bin/python scripts/market_snapshot.py 600519

# 扫描跨市场自选观察列表
.venv/bin/python scripts/scan_opportunities.py
```
