import type { UniversePool } from "../api/types";

export type UniverseGroupKey = "commodities" | "us_equities" | "a_shares" | "fx_crypto" | "custom";

export interface UniverseMetaDisplay {
  label: string;
  group: UniverseGroupKey;
  order: number;
}

export const UNIVERSE_GROUPS: { key: UniverseGroupKey; title: string }[] = [
  { key: "commodities", title: "大宗商品与航运" },
  { key: "us_equities", title: "美股与全球资源股" },
  { key: "a_shares", title: "A 股板块" },
  { key: "fx_crypto", title: "外汇与加密" },
  { key: "custom", title: "自定义沉淀池" },
];

const UNIVERSE_DISPLAY_MAP: Record<string, UniverseMetaDisplay> = {
  commodities_energy: { label: "能源原油与天然气", group: "commodities", order: 1 },
  commodities_precious: { label: "贵金属", group: "commodities", order: 2 },
  commodities_base: { label: "有色与基本金属", group: "commodities", order: 3 },
  commodities_ferrous: { label: "黑色金属", group: "commodities", order: 4 },
  commodities_grains_oilseeds: { label: "谷物与油脂", group: "commodities", order: 5 },
  commodities_softs: { label: "软商品", group: "commodities", order: 6 },
  commodities_livestock: { label: "畜牧与生猪", group: "commodities", order: 7 },
  commodities_chem_newenergy: { label: "化工与新能源金属", group: "commodities", order: 8 },
  shipping: { label: "航运期货", group: "commodities", order: 9 },

  us_sectors: { label: "美股宽基与行业 ETF", group: "us_equities", order: 1 },
  us_power_grid: { label: "美股电力与电网基建", group: "us_equities", order: 2 },
  us_semis_ai: { label: "美股半导体与 AI 算力", group: "us_equities", order: 3 },
  us_megacap_cashflow: { label: "美股科技现金流巨头", group: "us_equities", order: 4 },
  metals_equities: { label: "金属矿业与资源股", group: "us_equities", order: 5 },

  a_shares_etfs: { label: "A 股宽基与行业 ETF", group: "a_shares", order: 1 },
  a_shares_themes: { label: "A 股核心主题龙头", group: "a_shares", order: 2 },

  fx_majors: { label: "主流外汇与美元指数", group: "fx_crypto", order: 1 },
  crypto_majors: { label: "主流加密货币", group: "fx_crypto", order: 2 },
};

function deriveLabelFromDescription(name: string, description?: string | null): string {
  const raw = (description || "").trim();
  if (!raw) return name;
  const head = raw.split(/[（(：:—\-|]/)[0]?.trim() || raw;
  return head.length > 16 ? `${head.slice(0, 16)}…` : head;
}

export function getUniverseLabel(name: string, description?: string | null): string {
  if (!name) return "";
  if (name.toLowerCase() === "adhoc" || name.toLowerCase() === "custom") return "临时自选";
  const preset = UNIVERSE_DISPLAY_MAP[name];
  if (preset) return preset.label;
  return deriveLabelFromDescription(name, description);
}

export interface GroupedUniverseItem {
  key: string;
  label: string;
  description: string;
  symbols: string[];
  order: number;
}

export interface UniverseGroupSection {
  groupKey: UniverseGroupKey;
  title: string;
  items: GroupedUniverseItem[];
}

export function groupUniverses(universes: Record<string, UniversePool>): UniverseGroupSection[] {
  const buckets = new Map<UniverseGroupKey, GroupedUniverseItem[]>();
  for (const g of UNIVERSE_GROUPS) {
    buckets.set(g.key, []);
  }

  for (const [key, pool] of Object.entries(universes)) {
    const preset = UNIVERSE_DISPLAY_MAP[key];
    const group: UniverseGroupKey = preset?.group ?? "custom";
    const description = pool?.description || "";
    const symbols = Array.isArray(pool?.symbols) ? pool.symbols : [];
    buckets.get(group)?.push({
      key,
      label: getUniverseLabel(key, description),
      description,
      symbols,
      order: preset?.order ?? 999,
    });
  }

  const sections: UniverseGroupSection[] = [];
  for (const g of UNIVERSE_GROUPS) {
    const items = buckets.get(g.key) ?? [];
    if (items.length === 0) continue;
    items.sort((a, b) => a.order - b.order || a.key.localeCompare(b.key));
    sections.push({ groupKey: g.key, title: g.title, items });
  }
  return sections;
}

export function formatTaskKind(kind: string | null | undefined): string {
  switch (kind) {
    case "scan":
      return "板块缠论扫描";
    case "research_refresh":
    case "research-refresh":
      return "硬数据刷新";
    case "sentinel":
      return "哨兵巡检";
    case "macro_compass":
      return "Layer 1 战略推演";
    default:
      return kind || "任务";
  }
}

export function formatTaskStatus(status: string | null | undefined): string {
  switch (status) {
    case "completed":
      return "已完成";
    case "running":
      return "执行中";
    case "queued":
      return "排队中";
    case "failed":
      return "执行失败";
    case "cancelled":
      return "已终止";
    default:
      return status || "未知";
  }
}
