import { useEffect } from "react";
import {
  AlertTriangle,
  ArrowRight,
  BookmarkCheck,
  BookmarkPlus,
  CheckCircle2,
  ExternalLink,
  Layers,
  Scale,
  ShieldAlert,
  ShieldCheck,
  Sparkles,
  TrendingDown,
  TrendingUp,
  X,
  XCircle,
} from "lucide-react";
import type {
  CompassAvoidSectorItem,
  CompassDirectionCard,
  CompassResultPayload,
} from "../../api/types";

export type Layer1DrawerState =
  | { kind: "card"; card: CompassDirectionCard }
  | { kind: "avoid"; item: CompassAvoidSectorItem; index: number }
  | { kind: "audit" }
  | { kind: "macro_pillars" }
  | null;

interface Layer1DetailDrawerProps {
  drawer: Layer1DrawerState;
  result: CompassResultPayload | null;
  runId?: string;
  onClose: () => void;
  onActivateL2Card: (card: CompassDirectionCard) => void;
  onLaunchAvoidShort?: (item: CompassAvoidSectorItem, index: number) => void;
  isCardLogged?: (cardId: string) => boolean;
  isLoggingCard?: (cardId: string) => boolean;
  onLogCard?: (cardId: string) => void;
}

function safeHttpUrl(raw?: string | null): string | null {
  const s = (raw || "").trim();
  if (!s || s.includes("\\") || s.length > 2000) return null;
  try {
    const parsed = new URL(s);
    if (parsed.protocol !== "http:" && parsed.protocol !== "https:") return null;
    if (parsed.username || parsed.password || !parsed.hostname) return null;
    return parsed.href;
  } catch {
    return null;
  }
}

function directionMeta(direction: CompassDirectionCard["direction"]) {
  if (direction === "short") {
    return {
      label: "做空 SHORT",
      cls: "border-bear/40 bg-bear/12 text-bear",
      Icon: TrendingDown,
    };
  }
  if (direction === "pair") {
    return {
      label: "多空配对 PAIR",
      cls: "border-warning/40 bg-warning/12 text-warning",
      Icon: Scale,
    };
  }
  return {
    label: "做多 LONG",
    cls: "border-bull/40 bg-bull/12 text-bull",
    Icon: TrendingUp,
  };
}

function isHeadwindShortable(item: CompassAvoidSectorItem): boolean {
  if (typeof item.short_proxy_possible === "boolean") return item.short_proxy_possible;
  const text = `${item.sector} ${item.reason}`.toLowerCase();
  return /(美股|美国|ai|算力|芯片|科技|数据中心|纳斯达克|原油|石油|金属|锂|铝|铜|锡|镍|铁矿|黑色|地产|加密|比特币|btc|eth|外汇|汇率|商品|农产品|生猪)/.test(
    text,
  );
}

export function Layer1DetailDrawer({
  drawer,
  result,
  onClose,
  onActivateL2Card,
  onLaunchAvoidShort,
  isCardLogged,
  isLoggingCard,
  onLogCard,
}: Layer1DetailDrawerProps) {
  useEffect(() => {
    if (!drawer) return;
    const onKeyDown = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [drawer, onClose]);

  if (!drawer) return null;

  const titleId = "l1-detail-drawer-title";

  return (
    <>
      {/* Mobile-only light backdrop; desktop stays non-modal so switching cards updates in place */}
      <div
        className="fixed inset-0 z-40 bg-black/30 lg:hidden"
        aria-hidden="true"
        onClick={onClose}
      />

      <aside
        role="complementary"
        aria-labelledby={titleId}
        className="detail-drawer-sheet fixed inset-y-0 right-0 z-50 flex w-full max-w-[460px] flex-col border-l border-hairline bg-surface shadow-2xl"
      >
        {/* Sticky Drawer Header */}
        <div className="flex items-start justify-between gap-3 border-b border-hairline-soft bg-sunken/80 px-4 py-3">
          <div className="min-w-0 flex-1">
            {drawer.kind === "card" ? (
              <>
                <div className="flex flex-wrap items-center gap-1.5">
                  {(() => {
                    const dm = directionMeta(drawer.card.direction);
                    const Icon = dm.Icon;
                    return (
                      <span
                        className={`inline-flex items-center gap-1 rounded border px-1.5 py-0.5 text-[10px] font-semibold ${dm.cls}`}
                      >
                        <Icon className="h-3 w-3" aria-hidden="true" />
                        {dm.label}
                      </span>
                    );
                  })()}
                  <span className="rounded bg-subtle px-1.5 py-0.5 font-mono text-[10px] text-muted">
                    {drawer.card.horizon || "1Q~1Y"}
                  </span>
                  <span className="rounded bg-subtle px-1.5 py-0.5 text-[10px] text-muted">
                    置信度: {drawer.card.conviction === "high" ? "高" : drawer.card.conviction === "medium" ? "中" : "低"}
                  </span>
                </div>
                <h3 id={titleId} className="mt-1.5 text-[15px] font-semibold leading-snug text-ink">
                  {drawer.card.title}
                </h3>
              </>
            ) : drawer.kind === "avoid" ? (
              <>
                <div className="inline-flex items-center gap-1 rounded border border-bear/35 bg-bear/12 px-1.5 py-0.5 text-[10px] font-semibold text-bear">
                  <XCircle className="h-3 w-3" aria-hidden="true" />
                  规避 / 逆风板块详情
                </div>
                <h3 id={titleId} className="mt-1.5 text-[15px] font-semibold leading-snug text-ink">
                  {drawer.item.sector}
                </h3>
              </>
            ) : drawer.kind === "audit" ? (
              <>
                <div className="inline-flex items-center gap-1 rounded border border-warning/40 bg-warning/12 px-1.5 py-0.5 text-[10px] font-semibold text-warning">
                  <ShieldAlert className="h-3 w-3" aria-hidden="true" />
                  宪法护栏与同源因子审计
                </div>
                <h3 id={titleId} className="mt-1.5 text-[15px] font-semibold text-ink">
                  L1 规则拦截与风险降档详情
                </h3>
              </>
            ) : (
              <>
                <div className="inline-flex items-center gap-1 rounded border border-accent/35 bg-accent/12 px-1.5 py-0.5 text-[10px] font-semibold text-accent">
                  <Layers className="h-3 w-3" aria-hidden="true" />
                  本轮宏观四维推演底座
                </div>
                <h3 id={titleId} className="mt-1.5 text-[15px] font-semibold text-ink">
                  宏观定调与风险提示全文
                </h3>
              </>
            )}
          </div>
          <button
            type="button"
            className="btn btn-ghost btn-sm !min-h-[28px] !px-2"
            onClick={onClose}
            aria-label="关闭详情抽屉"
          >
            <X className="h-4 w-4" aria-hidden="true" />
          </button>
        </div>

        {/* Scrollable Drawer Content */}
        <div className="flex-1 space-y-4 overflow-y-auto px-4 py-4 text-[12.5px] leading-relaxed">
          {drawer.kind === "card" ? (
            <>
              {/* Constitutional Flags Alert */}
              {drawer.card.constitutional_flags && drawer.card.constitutional_flags.length > 0 ? (
                <div className="rounded-md border border-warning/40 bg-warning/10 p-3">
                  <div className="flex items-center gap-1.5 text-[11px] font-semibold text-warning">
                    <ShieldAlert className="h-3.5 w-3.5" aria-hidden="true" />
                    宪法护栏降档 / 警示说明
                  </div>
                  <ul className="mt-1.5 space-y-1 text-[11.5px] text-ink-2">
                    {drawer.card.constitutional_flags.map((flag, i) => (
                      <li key={i} className="flex items-start gap-1.5">
                        <span className="mt-1 h-1.5 w-1.5 shrink-0 rounded-full bg-warning" />
                        <span>
                          <strong className="text-ink">{flag.article || flag.code || flag.action}:</strong> {flag.message}
                        </span>
                      </li>
                    ))}
                  </ul>
                </div>
              ) : null}

              {/* Portfolio Overlap Warning */}
              {drawer.card.portfolio_overlap && drawer.card.portfolio_overlap.length > 0 ? (
                <div className="rounded-md border border-accent/35 bg-accent/8 p-3 text-[11.5px] text-ink-2">
                  <div className="font-semibold text-accent">现有持仓重合提示（单一板块龙头独占制）</div>
                  <p className="mt-1">已持有相关标的/板块：{drawer.card.portfolio_overlap.join("、")}</p>
                </div>
              ) : null}

              {/* Priced-in vs Unpriced Gap */}
              <div className="grid grid-cols-1 gap-2.5 sm:grid-cols-2">
                <div className="rounded-md border border-hairline-soft bg-sunken/70 p-3">
                  <div className="text-[10.5px] font-semibold uppercase tracking-wide text-muted">
                    市场已定价（共识预期）
                  </div>
                  <p className="mt-1 text-[12px] text-ink-2">
                    {drawer.card.priced_in_already || "未显式拆解"}
                  </p>
                </div>
                <div className="rounded-md border border-bull/30 bg-bull/6 p-3">
                  <div className="text-[10.5px] font-semibold uppercase tracking-wide text-bull">
                    真预期差（尚未定价）
                  </div>
                  <p className="mt-1 text-[12px] font-medium text-ink">
                    {drawer.card.unpriced_gap || drawer.card.card_headline_gap || drawer.card.thesis}
                  </p>
                </div>
              </div>

              {/* Dual Drivers Resonance */}
              <div className="space-y-2 rounded-md border border-hairline-soft bg-surface p-3">
                <div className="text-[11px] font-semibold text-ink">双维共振驱动拆解</div>
                <div className="space-y-1.5 text-[12px]">
                  <div>
                    <span className="mr-1.5 inline-block rounded bg-accent/12 px-1.5 py-px text-[10px] font-semibold text-accent">
                      主驱动 · {drawer.card.primary_driver?.dimension || "未标注"}
                    </span>
                    <span className="text-ink-2">{drawer.card.primary_driver?.summary || "—"}</span>
                  </div>
                  <div>
                    <span className="mr-1.5 inline-block rounded bg-subtle px-1.5 py-px text-[10px] font-semibold text-ink-2">
                      次驱动 · {drawer.card.secondary_driver?.dimension || "未标注"}
                    </span>
                    <span className="text-ink-2">{drawer.card.secondary_driver?.summary || "—"}</span>
                  </div>
                </div>
              </div>

              {/* Full Thesis & Value Chain Focus */}
              <div className="space-y-2">
                <div>
                  <div className="text-[11px] font-semibold text-ink">核心投资逻辑（1Q ~ 1Y）</div>
                  <p className="mt-1 text-ink-2">{drawer.card.thesis}</p>
                </div>
                {drawer.card.value_chain_focus ? (
                  <div className="rounded-md border border-hairline-soft bg-sunken/60 p-3">
                    <div className="text-[11px] font-semibold text-ink">产业链最受益环节（去锚定选龙头指南）</div>
                    <p className="mt-1 text-ink-2">{drawer.card.value_chain_focus}</p>
                  </div>
                ) : null}
              </div>

              {/* Catalyst & Hard Invalidation */}
              <div className="grid grid-cols-1 gap-2.5">
                {drawer.card.key_catalyst ? (
                  <div className="rounded-md border border-hairline-soft bg-surface p-3">
                    <div className="inline-flex items-center gap-1 text-[11px] font-semibold text-accent">
                      <Sparkles className="h-3.5 w-3.5" aria-hidden="true" />
                      未来催化节点
                    </div>
                    <p className="mt-1 text-ink-2">{drawer.card.key_catalyst}</p>
                  </div>
                ) : null}
                <div className="rounded-md border border-bear/35 bg-bear/8 p-3">
                  <div className="inline-flex items-center gap-1 text-[11px] font-semibold text-bear">
                    <AlertTriangle className="h-3.5 w-3.5" aria-hidden="true" />
                    核心证伪红线（触发即退出）
                  </div>
                  <p className="mt-1 text-ink">
                    {drawer.card.invalidation || drawer.card.invalidation_short || "未提供明确证伪阈值"}
                  </p>
                </div>
              </div>

              {/* L1-L4 Evidence Table */}
              {drawer.card.evidence && drawer.card.evidence.length > 0 ? (
                <div className="space-y-2">
                  <div className="text-[11px] font-semibold text-ink">
                    证据清单（L1–L4 · 共 {drawer.card.evidence.length} 条）
                  </div>
                  <div className="space-y-2">
                    {drawer.card.evidence.map((ev, idx) => {
                      const href = safeHttpUrl(ev.url);
                      return (
                        <div
                          key={idx}
                          className="rounded-md border border-hairline-soft bg-sunken/50 p-2.5 text-[11.5px]"
                        >
                          <div className="flex flex-wrap items-center justify-between gap-1.5">
                            <div className="flex items-center gap-1.5">
                              <span className="rounded bg-accent/14 px-1.5 py-px font-mono text-[10px] font-semibold text-accent">
                                {ev.level || "L2"}
                              </span>
                              <span className="font-medium text-ink">{ev.source || "一手/外部源"}</span>
                            </div>
                            <div className="flex items-center gap-1.5 text-[10px] text-muted">
                              {ev.verification_status === "verified" ? (
                                <span className="inline-flex items-center gap-0.5 text-bull">
                                  <CheckCircle2 className="h-3 w-3" aria-hidden="true" />
                                  已核验
                                </span>
                              ) : null}
                              {ev.date ? <span className="font-mono">{ev.date}</span> : null}
                              {href ? (
                                <a
                                  href={href}
                                  target="_blank"
                                  rel="noreferrer"
                                  className="inline-flex items-center gap-0.5 text-accent hover:underline"
                                >
                                  原文
                                  <ExternalLink className="h-3 w-3" aria-hidden="true" />
                                </a>
                              ) : null}
                            </div>
                          </div>
                          <p className="mt-1 text-ink-2">{ev.fact}</p>
                        </div>
                      );
                    })}
                  </div>
                </div>
              ) : null}
            </>
          ) : drawer.kind === "avoid" ? (
            <>
              <div className="rounded-md border border-bear/35 bg-bear/8 p-3">
                <div className="text-[11px] font-semibold text-bear">逆风 / 规避原因</div>
                <p className="mt-1 text-ink">{drawer.item.reason}</p>
              </div>
              {drawer.item.derived_strategy ? (
                <div className="rounded-md border border-hairline-soft bg-sunken/70 p-3">
                  <div className="text-[11px] font-semibold text-ink">衍生交易策略与表达建议</div>
                  <p className="mt-1 text-ink-2">{drawer.item.derived_strategy}</p>
                </div>
              ) : null}
              {drawer.item.short_proxy_hint ? (
                <div className="rounded-md border border-warning/35 bg-warning/8 p-3">
                  <div className="text-[11px] font-semibold text-warning">合规空头 / 跨资产代理提示</div>
                  <p className="mt-1 text-ink-2">{drawer.item.short_proxy_hint}</p>
                </div>
              ) : null}
              <p className="text-[11px] text-muted">
                铁律：A 股严格禁止直接做空及配对空头腿；若本逆风板块属于 A 股，仅作规避/止盈提醒或转译为商品期货/海外代理空头表达。
              </p>
            </>
          ) : drawer.kind === "audit" ? (
            <>
              <div className="grid grid-cols-3 gap-2 text-center">
                <div className="rounded-md border border-hairline-soft bg-sunken/70 p-2.5">
                  <div className="font-mono text-lg font-semibold text-bear">
                    {result?.constitutional_audit?.rejected_reasons?.length ?? 0}
                  </div>
                  <div className="text-[10.5px] text-muted">拦截剔除卡</div>
                </div>
                <div className="rounded-md border border-hairline-soft bg-sunken/70 p-2.5">
                  <div className="font-mono text-lg font-semibold text-warning">
                    {result?.constitutional_audit?.conviction_caps_applied ?? 0}
                  </div>
                  <div className="text-[10.5px] text-muted">置信度封顶</div>
                </div>
                <div className="rounded-md border border-hairline-soft bg-sunken/70 p-2.5">
                  <div className="font-mono text-lg font-semibold text-accent">
                    {result?.constitutional_audit?.flagged_cards_count ?? 0}
                  </div>
                  <div className="text-[10.5px] text-muted">标旗警示卡</div>
                </div>
              </div>

              {result?.factor_concentration?.concentrated ? (
                <div className="rounded-md border border-warning/40 bg-warning/10 p-3">
                  <div className="text-[11.5px] font-semibold text-warning">
                    同源 macro 因子高度集中提醒（防伪分散）
                  </div>
                  <ul className="mt-1.5 space-y-1 text-[11.5px] text-ink-2">
                    {(result.factor_concentration.warnings ?? []).map((w, i) => (
                      <li key={i}>• {w}</li>
                    ))}
                  </ul>
                </div>
              ) : null}

              {(result?.constitutional_audit?.rejected_reasons ?? []).length > 0 ? (
                <div className="space-y-2">
                  <div className="text-[11px] font-semibold text-ink">被宪法硬拦截的卡片明细</div>
                  <div className="space-y-1.5">
                    {(result?.constitutional_audit?.rejected_reasons ?? []).map((reason, i) => (
                      <div
                        key={i}
                        className="rounded-md border border-bear/30 bg-bear/6 px-3 py-2 text-[11.5px] text-ink-2"
                      >
                        {reason}
                      </div>
                    ))}
                  </div>
                </div>
              ) : (
                <div className="flex items-center gap-2 rounded-md border border-bull/30 bg-bull/8 p-3 text-[12px] text-bull">
                  <ShieldCheck className="h-4 w-4" aria-hidden="true" />
                  本轮全部方向卡片通过宪法规则核验，无拦截剔除项。
                </div>
              )}
            </>
          ) : (
            /* macro_pillars */
            <>
              {result?.regime_summary ? (
                <div className="rounded-md border border-hairline-soft bg-sunken/70 p-3">
                  <div className="text-[11px] font-semibold text-ink">全局宏观 Regime 研判</div>
                  <p className="mt-1 text-ink-2">{result.regime_summary}</p>
                </div>
              ) : null}

              <div className="space-y-2.5">
                {[
                  { key: "us_macro", label: "美债与流动性周期", text: result?.macro_signals?.us_macro },
                  { key: "china_macro", label: "中国财政与信用脉冲", text: result?.macro_signals?.china_macro },
                  {
                    key: "geopolitics_supply",
                    label: "地缘、资源国政策与供给约束",
                    text: result?.macro_signals?.geopolitics_supply,
                  },
                  { key: "tech_industry", label: "技术代际与产业变革", text: result?.macro_signals?.tech_industry },
                ].map((pillar) => (
                  <div key={pillar.key} className="rounded-md border border-hairline-soft bg-surface p-3">
                    <div className="text-[11px] font-semibold text-accent">{pillar.label}</div>
                    <p className="mt-1 text-ink-2">{pillar.text || "本轮无额外说明"}</p>
                  </div>
                ))}
              </div>

              {(result?.risk_warnings ?? []).length > 0 ? (
                <div className="rounded-md border border-warning/35 bg-warning/8 p-3">
                  <div className="text-[11px] font-semibold text-warning">本轮关键风险警示</div>
                  <ul className="mt-1.5 space-y-1 text-[11.5px] text-ink-2">
                    {(result?.risk_warnings ?? []).map((w, i) => (
                      <li key={i}>• {w}</li>
                    ))}
                  </ul>
                </div>
              ) : null}
            </>
          )}
        </div>

        {/* Sticky Footer CTAs */}
        {drawer.kind === "card" ? (
          <div className="flex flex-wrap items-center justify-between gap-2 border-t border-hairline-soft bg-sunken/80 px-4 py-3">
            {onLogCard ? (
              <button
                type="button"
                className="btn btn-sm"
                disabled={Boolean(isLoggingCard?.(drawer.card.id))}
                onClick={() => onLogCard(drawer.card.id)}
              >
                {isCardLogged?.(drawer.card.id) ? (
                  <>
                    <BookmarkCheck className="h-3.5 w-3.5 text-bull" aria-hidden="true" />
                    <span>已入 Track C 影子账本</span>
                  </>
                ) : (
                  <>
                    <BookmarkPlus className="h-3.5 w-3.5" aria-hidden="true" />
                    <span>记入 Track C 影子追踪</span>
                  </>
                )}
              </button>
            ) : (
              <span />
            )}
            <button
              type="button"
              className="btn btn-primary btn-sm"
              onClick={() => {
                onActivateL2Card(drawer.card);
                onClose();
              }}
            >
              <span>去 L2 轨道选真龙头</span>
              <ArrowRight className="h-3.5 w-3.5" aria-hidden="true" />
            </button>
          </div>
        ) : drawer.kind === "avoid" && isHeadwindShortable(drawer.item) && onLaunchAvoidShort ? (
          <div className="flex items-center justify-end gap-2 border-t border-hairline-soft bg-sunken/80 px-4 py-3">
            <button
              type="button"
              className="btn btn-primary btn-sm"
              onClick={() => {
                onLaunchAvoidShort(drawer.item, drawer.index);
                onClose();
              }}
            >
              <span>以此逆风逻辑启动 L2 空头轨道</span>
              <ArrowRight className="h-3.5 w-3.5" aria-hidden="true" />
            </button>
          </div>
        ) : null}
      </aside>
    </>
  );
}
