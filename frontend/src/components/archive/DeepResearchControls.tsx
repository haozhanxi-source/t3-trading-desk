import { useEffect, useState, type FormEvent } from "react";
import { Bot, CheckCircle2, Loader2, ShieldCheck, Square, Swords, X, XCircle } from "lucide-react";
import type {
  DeepResearchEngine,
  DeepResearchMode,
  ResearchEnginesResponse,
} from "../../api/types";
import { ModalDialog } from "../ui/ModalDialog";

export interface DeepResearchLogLine {
  ts: string;
  stage?: string;
  message: string;
}

export interface DeepResearchRunState {
  taskId: string;
  symbol: string;
  market: string | null;
  mode: DeepResearchMode;
  engine: DeepResearchEngine;
  status: "queued" | "running" | "completed" | "failed" | "cancelled";
  stage: string;
  done: number;
  total: number;
  startedAtMs: number;
  finishedAtMs?: number;
  logs: DeepResearchLogLine[];
  resultSummary?: string;
  error?: string;
}

const STAGES: { key: string; label: string; stepIndex: number }[] = [
  { key: "collect_data", label: "采集硬数据与缠论", stepIndex: 0 },
  { key: "build_brief", label: "封装 0-编造简报", stepIndex: 1 },
  { key: "agent_research", label: "AI 深研与红蓝对抗", stepIndex: 2 },
  { key: "parse_and_validate", label: "结构与证据校验", stepIndex: 3 },
  { key: "persist_archive", label: "原子写回档案", stepIndex: 4 },
];

function stageToIndex(stage: string, done: number, status: DeepResearchRunState["status"]): number {
  if (status === "completed") return STAGES.length;
  if (stage === "collect_data") return 0;
  if (stage === "build_brief") return 1;
  if (stage === "agent_research" || stage === "adversarial_debate" || stage === "agent_synthesis") return 2;
  if (stage === "parse_and_validate") return 3;
  if (stage === "persist_archive") return 4;
  return Math.max(0, Math.min(STAGES.length - 1, done));
}

function formatElapsed(ms: number): string {
  const sec = Math.max(0, Math.floor(ms / 1000));
  const m = Math.floor(sec / 60);
  const s = sec % 60;
  return m > 0 ? `${m}m ${String(s).padStart(2, "0")}s` : `${s}s`;
}

export function DeepResearchProgressCard({
  run,
  cancelling,
  onCancel,
  onDismiss,
}: {
  run: DeepResearchRunState;
  cancelling: boolean;
  onCancel: () => void;
  onDismiss: () => void;
}) {
  const isActive = run.status === "queued" || run.status === "running";
  const [nowMs, setNowMs] = useState(() => Date.now());

  useEffect(() => {
    if (!isActive) return;
    const id = window.setInterval(() => setNowMs(Date.now()), 1000);
    return () => window.clearInterval(id);
  }, [isActive]);

  const elapsedMs = (run.finishedAtMs ?? nowMs) - run.startedAtMs;
  const activeIdx = stageToIndex(run.stage, run.done, run.status);
  const modeLabel = run.mode === "adversarial" ? "跨模型红蓝对抗" : "标准 AI 深研";
  const recentLogs = run.logs.slice(-8);

  const borderTone =
    run.status === "completed"
      ? "border-bull/45 bg-bull/8"
      : run.status === "failed" || run.status === "cancelled"
        ? "border-critical/45 bg-critical/8"
        : "border-accent/45 bg-accent/8";

  return (
    <section
      className={`card flex flex-col gap-3 border p-3.5 ${borderTone}`}
      aria-live="polite"
      aria-label="AI 深度研究进度"
    >
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div className="flex flex-wrap items-center gap-2 text-xs">
          {isActive ? (
            <Loader2 className="h-4 w-4 animate-spin text-accent" aria-hidden="true" />
          ) : run.status === "completed" ? (
            <CheckCircle2 className="h-4 w-4 text-bull" aria-hidden="true" />
          ) : (
            <XCircle className="h-4 w-4 text-critical" aria-hidden="true" />
          )}
          <span className="font-semibold text-ink">
            {isActive
              ? `AI 深度研究进行中 · ${modeLabel}`
              : run.status === "completed"
                ? `AI 深度研究已完成并写回档案 · ${modeLabel}`
                : run.status === "cancelled"
                  ? "AI 深度研究已终止"
                  : "AI 深度研究失败"}
          </span>
          <span className="rounded border border-hairline-soft bg-sunken px-1.5 py-0.5 font-mono text-[10px] text-muted">
            {run.engine.toUpperCase()} · {formatElapsed(elapsedMs)}
          </span>
        </div>

        <div className="flex items-center gap-1.5">
          {isActive ? (
            <button
              type="button"
              className="btn btn-sm border-critical/40 text-critical hover:bg-critical/10"
              disabled={cancelling}
              onClick={onCancel}
            >
              <Square className="h-3 w-3 fill-current" aria-hidden="true" />
              <span>{cancelling ? "正在终止…" : "终止研究"}</span>
            </button>
          ) : (
            <button
              type="button"
              className="btn btn-ghost btn-sm px-1.5 text-muted hover:text-ink"
              title="关闭进度提示"
              onClick={onDismiss}
            >
              <X className="h-3.5 w-3.5" aria-hidden="true" />
            </button>
          )}
        </div>
      </div>

      {/* 5-step progress rail */}
      <div className="grid grid-cols-2 gap-1.5 sm:grid-cols-5">
        {STAGES.map((st, idx) => {
          const completed = idx < activeIdx || run.status === "completed";
          const current = isActive && idx === activeIdx;
          return (
            <div
              key={st.key}
              className={`rounded border px-2 py-1.5 text-[11px] transition-colors ${
                completed
                  ? "border-bull/35 bg-bull/10 text-ink"
                  : current
                    ? "border-accent/60 bg-accent/15 font-semibold text-ink"
                    : "border-hairline-soft bg-sunken/70 text-muted"
              }`}
            >
              <div className="flex items-center gap-1">
                <span className="font-mono text-[10px]">{idx + 1}.</span>
                <span className="truncate">{st.label}</span>
              </div>
            </div>
          );
        })}
      </div>

      {run.resultSummary ? (
        <div className="rounded border border-bull/30 bg-surface/80 px-2.5 py-1.5 text-xs text-ink">
          {run.resultSummary}
        </div>
      ) : null}

      {run.error ? (
        <div className="rounded border border-critical/40 bg-surface/80 px-2.5 py-1.5 text-xs text-critical">
          {run.error}
        </div>
      ) : null}

      {recentLogs.length > 0 ? (
        <div className="max-h-32 overflow-y-auto rounded border border-hairline-soft bg-sunken/80 px-2.5 py-2 font-mono text-[11px] leading-relaxed text-ink-2">
          {recentLogs.map((item, i) => (
            <div key={`${item.ts}-${i}`} className="flex gap-2">
              <span className="shrink-0 text-muted">{item.ts}</span>
              <span className="break-all">{item.message}</span>
            </div>
          ))}
        </div>
      ) : null}
    </section>
  );
}

function resolveDynamicFocusPlaceholder(opts: {
  symbol: string;
  market: string | null;
  assetClass?: string | null;
  layer1Primary?: string | null;
}): string {
  const rawTheme = (opts.layer1Primary || "").trim();
  const isGenericTheme =
    !rawTheme ||
    rawTheme === "临时自选" ||
    rawTheme.toLowerCase() === "adhoc" ||
    rawTheme.toLowerCase() === "custom" ||
    rawTheme.includes("待补充");
  const cleanTheme = isGenericTheme
    ? ""
    : rawTheme.length > 26
      ? `${rawTheme.slice(0, 26)}…`
      : rawTheme;

  const ac = (opts.assetClass || "").trim().toLowerCase();
  const mkt = (opts.market || "").trim().toUpperCase();
  const sym = (opts.symbol || "").trim().toUpperCase();

  let focusHint: string;
  if (ac.includes("us_stock") || ac === "us_equity" || mkt === "US_STOCKS" || mkt === "US") {
    focusHint = "未来 1~3 年盈利预期差、订单/资本开支与回购节奏，以及日线方向与 30min 缠论买点";
  } else if (
    ac.includes("a_share") ||
    ac.includes("cn_stock") ||
    mkt === "A_SHARES" ||
    mkt === "CN_STOCKS" ||
    /\.(SS|SZ|BJ)$/.test(sym) ||
    /^\d{6}$/.test(sym)
  ) {
    focusHint = "未来 1~3 年业绩拐点与 5 年 PE/PB 历史分位、产业政策催化，以及 30min 缠论买点防线";
  } else if (ac === "fx" || mkt === "FX" || sym.endsWith("=X") || sym === "DXY" || sym === "DX-Y.NYB") {
    focusHint = "各国 10Y/2Y 国债利差、央行路径与干预红线、CFTC TFF 杠杆基金持仓，以及 30min 缠论结构";
  } else if (
    ac === "crypto" ||
    mkt === "CRYPTO" ||
    /^(BTC|ETH|SOL)/.test(sym) ||
    sym.includes("/USDT") ||
    sym.endsWith("-USD")
  ) {
    focusHint = "稳定币净流入、现货 ETF 资金流、永续资金费率与 OI 拥挤度，以及 30min 缠论防线";
  } else if (
    ac.includes("future") ||
    ac.includes("commodity") ||
    mkt === "CN_FUTURES" ||
    mkt === "US_FUTURES" ||
    mkt === "COMMODITIES" ||
    sym.endsWith("=F") ||
    /^[A-Z]{1,2}0$/.test(sym)
  ) {
    focusHint = "未来 1Q~1Y 供需平衡表（投产与需求）、显性库存与期限结构/基差变化，以及 30min 缠论买卖点";
  } else {
    focusHint = "未来 1Q~1Y 核心预期差、L1/L2 一手硬数据核验，以及日线定方向与 30min 缠论触发防线";
  }

  return cleanTheme
    ? `例如：围绕「${cleanTheme}」主线，重点核查${focusHint}…`
    : `例如：重点核查${focusHint}…`;
}

export function DeepResearchDialog({
  open,
  symbol,
  name,
  market,
  assetClass,
  layer1Primary,
  isHolding,
  enginesInfo,
  onClose,
  onLaunch,
}: {
  open: boolean;
  symbol: string;
  name?: string;
  market: string | null;
  assetClass?: string | null;
  layer1Primary?: string | null;
  isHolding: boolean;
  enginesInfo: ResearchEnginesResponse | null;
  onClose: () => void;
  onLaunch: (opts: {
    mode: DeepResearchMode;
    engine: DeepResearchEngine;
    focus_prompt: string;
  }) => Promise<void>;
}) {
  const defaultEngine: DeepResearchEngine = enginesInfo?.default_engine ?? "claude";
  const [mode, setMode] = useState<DeepResearchMode>("standard");
  const [engine, setEngine] = useState<DeepResearchEngine>(defaultEngine);
  const [focusPrompt, setFocusPrompt] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    if (!open) return;
    setMode("standard");
    setEngine(enginesInfo?.default_engine ?? "claude");
    setFocusPrompt("");
    setErr(null);
  }, [open, enginesInfo?.default_engine]);

  const anyAvailable = enginesInfo ? enginesInfo.any_available : true;

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    if (!anyAvailable || busy) return;
    setBusy(true);
    setErr(null);
    try {
      await onLaunch({
        mode,
        engine,
        focus_prompt: focusPrompt.trim(),
      });
      onClose();
    } catch (ex) {
      setErr(ex instanceof Error ? ex.message : String(ex));
    } finally {
      setBusy(false);
    }
  };

  const titleLabel = name && name !== symbol ? `${name} (${symbol})` : symbol;
  const focusPlaceholder = resolveDynamicFocusPlaceholder({
    symbol,
    market,
    assetClass,
    layer1Primary,
  });

  return (
    <ModalDialog
      open={open}
      onClose={onClose}
      title={`启动 AI 深度研究 · ${titleLabel}`}
      subtitle={`市场分区 ${market || "自动识别"} · 自动完成第三层纯研究、红蓝对抗与第四层缠论执行框架并写回档案`}
    >
      <form onSubmit={submit} className="flex flex-col gap-4 p-5">
        {!anyAvailable ? (
          <div role="alert" className="rounded-md border border-critical/50 bg-critical/10 px-3 py-2 text-xs text-ink">
            宿主机未检测到可用的 Claude Code 或 Codex CLI，请先在服务器完成对应 CLI 安装与登录。
          </div>
        ) : null}

        <div className="flex flex-col gap-2">
          <span className="text-xs font-semibold text-ink">研究深度模式</span>
          <div className="grid grid-cols-1 gap-2.5 sm:grid-cols-2">
            <button
              type="button"
              onClick={() => setMode("standard")}
              className={`flex cursor-pointer flex-col gap-1 rounded-lg border p-3 text-left transition-colors ${
                mode === "standard"
                  ? "border-accent bg-accent/10 text-ink"
                  : "border-hairline bg-sunken/50 text-ink-2 hover:border-hairline-strong"
              }`}
            >
              <div className="flex items-center gap-1.5 text-xs font-semibold">
                <Bot className="h-3.5 w-3.5 text-accent" aria-hidden="true" />
                <span>标准深度研究（推荐）</span>
              </div>
              <p className="m-0 text-[11px] leading-relaxed text-muted">
                硬数据底座 + 实时联网验真 + 第三层纯研究与反方自辩 + 第四层 30m 缠论防线与触发器（约 1.5~3 分钟）。
              </p>
            </button>

            <button
              type="button"
              onClick={() => setMode("adversarial")}
              className={`flex cursor-pointer flex-col gap-1 rounded-lg border p-3 text-left transition-colors ${
                mode === "adversarial"
                  ? "border-accent bg-accent/10 text-ink"
                  : "border-hairline bg-sunken/50 text-ink-2 hover:border-hairline-strong"
              }`}
            >
              <div className="flex items-center gap-1.5 text-xs font-semibold">
                <Swords className="h-3.5 w-3.5 text-warning" aria-hidden="true" />
                <span>
                  {enginesInfo?.adversarial_cross_model ? "跨模型红蓝对抗（深度）" : "双轮独立红蓝对抗（深度）"}
                </span>
              </div>
              <p className="m-0 text-[11px] leading-relaxed text-muted">
                主研初稿 → 独立反方魔鬼代言人逐条攻防并量化反向情景 → 主研吸收质询终裁定稿（约 3~5 分钟）。
              </p>
            </button>
          </div>
        </div>

        <div className="flex flex-col gap-2">
          <span className="text-xs font-semibold text-ink">主研 AI 引擎</span>
          <div className="grid grid-cols-1 gap-2 sm:grid-cols-2">
            {(enginesInfo?.engines ?? [
              { id: "claude" as const, label: "Claude Code", description: "默认主力引擎", available: true },
              { id: "codex" as const, label: "Codex", description: "备选引擎", available: true },
            ]).map((eng) => {
              const selected = engine === eng.id;
              return (
                <button
                  key={eng.id}
                  type="button"
                  disabled={!eng.available}
                  onClick={() => setEngine(eng.id)}
                  className={`flex items-start justify-between gap-2 rounded-md border px-3 py-2 text-left text-xs transition-colors ${
                    !eng.available
                      ? "cursor-not-allowed border-hairline-soft bg-sunken/30 text-muted opacity-60"
                      : selected
                        ? "cursor-pointer border-accent bg-accent/10 text-ink"
                        : "cursor-pointer border-hairline bg-sunken/50 text-ink-2 hover:border-hairline-strong"
                  }`}
                >
                  <div>
                    <div className="font-semibold">{eng.label}</div>
                    <div className="mt-0.5 text-[11px] text-muted">{eng.description}</div>
                  </div>
                  <span
                    className={`mt-0.5 rounded px-1.5 py-0.5 font-mono text-[10px] ${
                      eng.available ? "bg-bull/15 text-bull" : "bg-sunken text-muted"
                    }`}
                  >
                    {eng.available ? "就绪" : "未安装"}
                  </span>
                </button>
              );
            })}
          </div>
        </div>

        <label className="field">
          <span>本次研究侧重点 / 额外指令（选填）</span>
          <textarea
            className="input"
            rows={2}
            maxLength={1200}
            placeholder={focusPlaceholder}
            value={focusPrompt}
            onChange={(e) => setFocusPrompt(e.target.value)}
          />
        </label>

        <div className="flex items-start gap-2 rounded-md border border-hairline-soft bg-sunken/60 px-3 py-2 text-[11px] leading-relaxed text-muted">
          <ShieldCheck className="mt-0.5 h-3.5 w-3.5 shrink-0 text-accent" aria-hidden="true" />
          <div>
            研究在只读隔离沙箱内执行，完成后由后端原子写回：研报正文、核心预期差、方向裁决、L1~L4 分级证据、哨兵自动化撤离触发器与未来催化日历；底部历史修订日志永久保留。
            {isHolding ? " 当前标的处于「持有中」状态，系统会自动保护该持仓状态不被降级覆盖。" : ""}
          </div>
        </div>

        {err ? (
          <div role="alert" className="rounded-md border border-critical/50 bg-critical/10 px-3 py-2 text-xs text-ink">
            {err}
          </div>
        ) : null}

        <div className="flex justify-end gap-2 border-t border-hairline pt-3">
          <button type="button" className="btn" onClick={onClose} disabled={busy}>
            取消
          </button>
          <button type="submit" className="btn btn-primary" disabled={busy || !anyAvailable}>
            <Bot className="h-3.5 w-3.5" aria-hidden="true" />
            <span>{busy ? "正在启动…" : "开始后台研究"}</span>
          </button>
        </div>
      </form>
    </ModalDialog>
  );
}
