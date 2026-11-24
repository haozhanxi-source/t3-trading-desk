/**
 * Canonical Chinese display names for symbols across universes, commodities,
 * equities, FX, and crypto so users never have to decode raw ticker codes
 * like BZ=F, HO=F, SC0, or 601899.SS on screen.
 */

const SYMBOL_ZH_NAMES: Record<string, string> = {
  // 能源原油与天然气
  "CL=F": "WTI原油",
  "BZ=F": "布伦特原油",
  "NG=F": "美国天然气",
  "HO=F": "美国取暖油",
  "RB=F": "美国汽油",
  SC0: "上海原油",
  FU0: "燃料油",
  PG0: "液化石油气(LPG)",

  // 贵金属
  "GC=F": "COMEX黄金",
  "SI=F": "COMEX白银",
  "PL=F": "NYMEX铂金",
  "PA=F": "NYMEX钯金",
  AU0: "沪金",
  AG0: "沪银",

  // 有色与基本金属
  "HG=F": "COMEX铜",
  "ALI=F": "COMEX铝",
  CU0: "沪铜",
  AL0: "沪铝",
  ZN0: "沪锌",
  NI0: "沪镍",
  SN0: "沪锡",
  PB0: "沪铅",

  // 黑色金属
  I0: "铁矿石",
  RB0: "螺纹钢",
  HC0: "热卷",
  J0: "焦炭",
  JM0: "焦煤",
  SF0: "硅铁",
  SM0: "锰硅",
  "HRC=F": "美国热卷",

  // 谷物与油脂
  "ZC=F": "CBOT玉米",
  "ZS=F": "CBOT大豆",
  "ZW=F": "CBOT小麦",
  "ZM=F": "CBOT豆粕",
  "ZL=F": "CBOT豆油",
  M0: "豆粕",
  Y0: "豆油",
  P0: "棕榈油",
  OI0: "菜籽油",
  C0: "连玉米",

  // 软商品
  "KC=F": "ICE咖啡",
  "CC=F": "ICE可可",
  "SB=F": "ICE原糖",
  "CT=F": "ICE美棉",
  "OJ=F": "ICE橙汁",
  SR0: "郑白糖",
  CF0: "郑棉花",

  // 畜牧与生猪
  "LE=F": "CME活牛",
  "HE=F": "CME瘦肉猪",
  "GF=F": "CME育肥牛",
  LH0: "大商所生猪",

  // 化工与新能源金属
  TA0: "PTA",
  MA0: "甲醇",
  V0: "PVC",
  SA0: "纯碱",
  FG0: "玻璃",
  RU0: "天然橡胶",
  LC0: "碳酸锂",
  SI0: "工业硅",
  PS0: "多晶硅",

  // 航运期货
  EC0: "集运欧线",

  // 金属矿业与资源股
  COPX: "全球铜矿ETF",
  GDX: "黄金矿业ETF",
  XME: "美国金属矿业ETF",
  FCX: "自由港迈克墨伦(铜)",
  SCCO: "南方铜业",
  AA: "美国铝业",
  NEM: "纽蒙特矿业(金)",
  MP: "MP材料(稀土)",
  "512400.SS": "有色金属ETF",
  "601899.SS": "紫金矿业",
  "603993.SS": "洛阳钼业",
  "601600.SS": "中国铝业",

  // 美股宽基与行业 ETF
  SPY: "标普500 ETF",
  QQQ: "纳指100 ETF",
  IWM: "罗素2000 ETF",
  XLK: "美股科技ETF",
  SMH: "半导体ETF",
  XLU: "公用事业ETF",
  XLE: "能源ETF",
  XLI: "工业ETF",
  XLF: "金融ETF",
  XLV: "医疗保健ETF",
  ITA: "航空军工ETF",
  XLB: "原材料ETF",
  XLP: "必需消费ETF",
  XLY: "可选消费ETF",

  // 美股电力与电网基建
  CEG: "星座能源(核电)",
  VST: "Vistra能源",
  GEV: "GE Vernova(电力设备)",
  ETN: "伊顿(电气设备)",
  VRT: "维谛技术(供电/液冷)",
  PWR: "广达服务(电网工程)",

  // 美股半导体与 AI 算力
  NVDA: "英伟达",
  AVGO: "博通",
  TSM: "台积电",
  MU: "美光科技",
  MRVL: "迈威尔科技",
  AMD: "超威半导体(AMD)",

  // 美股科技现金流巨头
  AAPL: "苹果",
  MSFT: "微软",
  GOOGL: "谷歌",
  AMZN: "亚马逊",
  META: "Meta",

  // A 股宽基、ETF 与主题龙头
  "510300.SS": "沪深300 ETF",
  "588000.SS": "科创50 ETF",
  "510880.SS": "红利ETF",
  "512480.SS": "半导体ETF(A股)",
  "515220.SS": "煤炭ETF",
  "512660.SS": "军工ETF",
  "512010.SS": "医药ETF",
  "159928.SZ": "消费ETF",
  "512880.SS": "证券ETF",
  "300750.SZ": "宁德时代",
  "002050.SZ": "三花智控",
  "600519.SS": "贵州茅台",

  // 加密货币
  "BTC/USDT": "比特币",
  "ETH/USDT": "以太坊",
  "SOL/USDT": "Solana",
  "BTC-USD": "比特币",
  "ETH-USD": "以太坊",
  "SOL-USD": "Solana",
  BTCUSDT: "比特币",
  ETHUSDT: "以太坊",
  SOLUSDT: "Solana",
  BTC: "比特币",
  ETH: "以太坊",
  SOL: "Solana",

  // 外汇与美元指数
  "DX-Y.NYB": "美元指数",
  DXY: "美元指数",
  "EURUSD=X": "欧元/美元",
  "USDJPY=X": "美元/日元",
  "GBPUSD=X": "英镑/美元",
  "AUDUSD=X": "澳元/美元",
  "USDCNY=X": "美元/人民币",
  "USDCAD=X": "美元/加元",
};

export function getSymbolName(symbol: string | null | undefined): string | null {
  if (!symbol) return null;
  const raw = symbol.trim();
  if (!raw) return null;
  const upper = raw.toUpperCase();

  if (SYMBOL_ZH_NAMES[upper]) return SYMBOL_ZH_NAMES[upper];

  // Match bare 6-digit A-share codes against .SS / .SZ entries
  if (/^\d{6}$/.test(upper)) {
    return SYMBOL_ZH_NAMES[`${upper}.SS`] ?? SYMBOL_ZH_NAMES[`${upper}.SZ`] ?? null;
  }

  // Match bare 6-letter FX pairs against =X entries
  if (/^[A-Z]{6}$/.test(upper)) {
    return SYMBOL_ZH_NAMES[`${upper}=X`] ?? null;
  }

  return null;
}

export function formatSymbolWithCode(
  symbol: string | null | undefined,
  preferredName?: string | null,
): string {
  const code = (symbol || "").trim();
  if (!code) return "";
  const candidate = (preferredName || "").trim();
  const name = candidate && candidate.toUpperCase() !== code.toUpperCase() ? candidate : getSymbolName(code);
  return name ? `${name} (${code})` : code;
}
