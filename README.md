# 中低频投研工作台 (T3 Trading Desk · 缠论版)

直接在 T3 Code 内部运行的多市场（美股 / A股 / 加密货币 / 大宗商品）中低频投研系统，采用**缠论多周期结构引擎（周线 + 日线分型、严格笔、中枢、一二三类买卖点、动力学背驰与资金方向一致性判别）** + T3 Code 原生多智能体协作（缠论结构、基本面、新闻宏观、多空辩论、风控决策）。

## 目录结构

- `AGENTS.md`：缠论多智能体投研工作流指令
- `watchlist.json`：跨市场自选观察列表
- `scripts/chanlun.py`：缠论核心引擎（K线包含、分型、严格笔、中枢、MACD力度面积对比、资金一致性 vs 背驰判别、周/日区间套）
- `scripts/market_snapshot.py`：单标的行情、缠论结构、基本面/资金费率与宏观数据抓取
- `scripts/scan_opportunities.py`：跨市场自选股/商品/加密缠论信号扫描
- `tests/test_chanlun.py`：缠论核心引擎与信号分类单元测试
- `reports/`：保存生成的分析报告

## 快速测试

```bash
# 运行单元测试
.venv/bin/python -m unittest discover -s tests -v

# 查看单个标的行情与缠论快照
.venv/bin/python scripts/market_snapshot.py NVDA
.venv/bin/python scripts/market_snapshot.py 黄金
.venv/bin/python scripts/market_snapshot.py BTC
.venv/bin/python scripts/market_snapshot.py 600519

# 扫描跨市场自选观察列表
.venv/bin/python scripts/scan_opportunities.py
```
