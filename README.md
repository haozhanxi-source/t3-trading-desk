# 中低频投研工作台 (T3 Trading Desk · 缠论版)

直接在 T3 Code 内部运行的多市场（美股 / A股 / 加密货币 / 大宗商品）中长线投研系统，着眼于**未来 1 个季度至 1 年（1Q ~ 1Y）的宏观与产业头寸机会**，交易结构与买卖点执行**严格锚定在日线级别 K 线缠论结构**（3 年日线样本、多中枢演化、一二三类买卖点、动力学背驰与资金方向一致性判别）。

## 目录结构

- `AGENTS.md`：1Q~1Y 大逻辑 + 日线缠论执行工作流指令
- `watchlist.json`：跨市场核心观察列表（分析与寻猎时支持任意跨市场标的实时拉取）
- `scripts/chanlun.py`：日线缠论核心引擎（K线包含、分型、严格笔、多中枢识别、MACD力度面积对比、资金一致性 vs 背驰判别）
- `scripts/market_snapshot.py`：单标的行情（默认 3 年日线）、日线缠论结构、基本面/资金费率与宏观数据抓取
- `scripts/scan_opportunities.py`：跨市场自选股/商品/加密日线缠论信号扫描
- `tests/test_chanlun.py`：缠论核心引擎与信号分类单元测试
- `reports/`：保存生成的分析报告

## 快速测试

```bash
# 运行单元测试
.venv/bin/python -m unittest discover -s tests -v

# 查看单个标的行情与 3 年日线缠论快照
.venv/bin/python scripts/market_snapshot.py NVDA
.venv/bin/python scripts/market_snapshot.py 黄金
.venv/bin/python scripts/market_snapshot.py BTC
.venv/bin/python scripts/market_snapshot.py 600519

# 扫描跨市场自选观察列表
.venv/bin/python scripts/scan_opportunities.py
```
