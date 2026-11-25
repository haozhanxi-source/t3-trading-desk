"""AI web-search & manual verifier for qualitative / policy sentinel triggers.

Machine-observable metrics (price, Chanlun levels, basis, inventory, term structure,
rate differentials, CFTC COT, ETF flows, etc.) are evaluated deterministically by
``daily_sentinel.py``. Non-standard policy/event triggers (e.g. Indonesia RKAB quota,
export bans, regulatory approvals) lack free structured APIs; this module verifies
them automatically via a read-only headless agent CLI with WebSearch/WebFetch and
atomically writes structured verification records back into ``state.json`` so
``daily_sentinel.py`` can evaluate them without false-green risk.
"""
from __future__ import annotations

import json
import os
import tempfile
import threading
from datetime import date, datetime, timezone
from typing import Any, Callable

import daily_sentinel
import research_archive

from . import deep_research
from .cache import CACHE
from .serializers import sanitize

Emit = Callable[[str, dict[str, Any]], None]

VERIFY_TIMEOUT_SECONDS = 240


def _is_valid_http_url(url: Any) -> bool:
    return research_archive.is_valid_http_url(url)


def find_verifiable_triggers(
    state: dict[str, Any],
    *,
    ref_date: date | None = None,
    now_utc: datetime | None = None,
    trigger_ids: list[str] | None = None,
    include_fresh: bool = False,
) -> list[dict[str, Any]]:
    """Return armed non-auto triggers in ``state`` that require AI web or manual verification."""
    triggers = state.get("triggers")
    if not isinstance(triggers, list):
        return []
    wanted = {str(t).strip() for t in trigger_ids if str(t).strip()} if trigger_ids else None
    eval_now = now_utc or (
        None if ref_date is not None else datetime.now(timezone.utc)
    )

    # Evaluate against empty live observations so only stored verifications count
    evaluated_map: dict[str, dict[str, Any]] = {}
    armed = [t for t in triggers if isinstance(t, dict) and daily_sentinel._is_trigger_armed(t)]
    for item in research_archive.evaluate_triggers(
        {"triggers": armed}, {}, ref_date=ref_date, now_utc=eval_now
    ):
        tid = str(item.get("id") or "")
        if tid:
            evaluated_map[tid] = item

    out: list[dict[str, Any]] = []
    for trig in armed:
        tid = str(trig.get("id") or "").strip()
        if not tid:
            continue
        if wanted is not None and tid not in wanted:
            continue
        # Auto machine-observable metrics are NEVER eligible for AI/manual override
        if research_archive.is_trigger_auto_metric(trig):
            continue
        ev = evaluated_map.get(tid) or {}
        if not include_fresh and ev.get("evaluated") is True and not ev.get("verification_stale"):
            continue
        out.append(trig)
    return out


def build_trigger_verify_prompt(
    *,
    brief_path: str,
    symbol: str,
    market: str,
    name: str,
    asset_class: str,
    today_str: str,
    triggers: list[dict[str, Any]],
) -> str:
    """Build a focused, zero-fabrication prompt for web-verifying qualitative/policy triggers."""
    return (
        "你是 T3 Trading Desk 哨兵触发器核验专员。请针对标的研究档案中无法由免费行情 API 自动抓取的"
        "政策/事件/非标监控触发器，使用 WebSearch / WebFetch 联网查证最新权威公开信息，并输出严格 JSON。\n\n"
        f"标的：{name or symbol}（代码 `{symbol}`，市场 `{market}`，资产类别 `{asset_class}`）\n"
        f"当前日期（UTC）：{today_str}\n"
        f"待核验简报文件（请先使用 Read 工具读取）：{brief_path}\n"
        f"待核验触发器数量：{len(triggers)} 条\n\n"
        "【硬性核验纪律】\n"
        "1. 严格零编造：禁止凭记忆猜政策数值或虚构公告；必须通过 WebSearch/WebFetch 检索最新权威信源。\n"
        "2. 证据分级：优先 L1（政府/监管/交易所/央行/公司官方公告）与 L2（权威通讯社 Reuters/Bloomberg/官方行业协会），"
        "其次 L3（主流投行/研报），再次 L4（一般新闻媒体）。\n"
        "3. 审慎判定 status：\n"
        "   - `fired`：有确凿公开信源证明阈值/事件条件**已经满足/生效**（宁可早预警不漏报，但须有真实依据）；\n"
        "   - `clear`：已查证最新公开进展，明确**尚未触发**该条件（例如最新官方配额未低于阈值、尚未出台禁令）；\n"
        "   - `uncertain`：联网未找到足够清晰或可信的最新公开信息，无法判断是否触发（绝不强行给 clear）。\n"
        "4. 若触发器为数值比较（如 `< 250`）且查到具体数字，请把数字填入 `observed`（JSON number），并在 `observed_text` 中写明单位与口径；"
        "若尚未官宣具体数字但明确未触发，可令 `observed: null` 并在 `observed_text` 说明现状。\n"
        "5. 只输出一个 ```json ... ``` 代码块，严格符合如下 Schema（不要附带额外解释文本）：\n"
        "```json\n"
        "{\n"
        '  "results": [\n'
        "    {\n"
        '      "id": "必须与待核验触发器的 id 完全一致",\n'
        '      "status": "clear | fired | uncertain",\n'
        '      "observed": null,\n'
        '      "observed_text": "简明现状或带单位的观测值（如：2027 RKAB 尚未官宣低于2.5亿湿吨，目前官方讨论区间2.7~2.9亿湿吨）",\n'
        '      "summary": "1~2句清晰核验结论，说明为何判定为 clear/fired/uncertain",\n'
        '      "evidence_tier": "L1 | L2 | L3 | L4",\n'
        '      "source_title": "来源机构或报道标题",\n'
        '      "source_url": "https://...（无有效URL则填空字符串）"\n'
        "    }\n"
        "  ]\n"
        "}\n"
        "```\n"
    )


def normalize_verification_payload(
    raw_payload: dict[str, Any],
    expected_triggers: list[dict[str, Any]],
    *,
    method: str = "ai_web",
    verified_at_utc: str | None = None,
    ttl_days: int = research_archive.DEFAULT_VERIFICATION_TTL_DAYS,
) -> dict[str, dict[str, Any]]:
    """Validate and sanitize LLM verification output into ``{trigger_id: verification_dict}``."""
    now_str = verified_at_utc or research_archive.utc_now_iso_seconds()
    expected_by_id = {str(t.get("id") or "").strip(): t for t in expected_triggers if t.get("id")}
    raw_results = raw_payload.get("results")
    if not isinstance(raw_results, list):
        raise ValueError("AI 核验输出缺少有效的 results 数组")

    verifications: dict[str, dict[str, Any]] = {}
    for item in raw_results:
        if not isinstance(item, dict):
            continue
        tid = str(item.get("id") or "").strip()
        if not tid or tid not in expected_by_id:
            continue
        trig = expected_by_id[tid]
        status = str(item.get("status") or "uncertain").strip().lower()
        if status not in research_archive.VALID_VERIFICATION_STATUSES:
            status = "uncertain"

        obs = item.get("observed")
        if isinstance(obs, float) and (obs != obs or obs in (float("inf"), float("-inf"))):
            obs = None

        tier = str(item.get("evidence_tier") or "L4").strip().upper()
        if tier not in research_archive.VALID_EVIDENCE_LEVELS:
            tier = "L4"

        src_url = str(item.get("source_url") or "").strip()
        if not _is_valid_http_url(src_url):
            src_url = ""
        src_title = str(item.get("source_title") or "").strip()
        obs_txt = str(item.get("observed_text") or "").strip()
        summary = str(item.get("summary") or obs_txt or "").strip()

        # Fail-closed checks aligned with research_archive.evaluate_triggers:
        # 1) Numeric breach always forces fired; uncomparable observed value blocks clear.
        op = trig.get("operator")
        thresh = trig.get("threshold")
        if obs is not None and op in (">", ">=", "<", "<=", "==", "contains"):
            ok_cmp, cmp_fired, _ = research_archive._compare_trigger_value(op, obs, thresh)
            if ok_cmp and cmp_fired:
                status = "fired"
            elif not ok_cmp and status == "clear":
                status = "uncertain"

        # 2) Unsubstantiated clear without summary/provenance downgrades to uncertain.
        if status == "clear":
            if not summary:
                status = "uncertain"
            elif method == "ai_web" and not (src_url or src_title):
                status = "uncertain"
            elif method == "ai_web" and research_archive.is_numeric_trigger(op, thresh) and obs is None and tier not in {"L1", "L2"}:
                status = "uncertain"

        trig_ttl = trig.get("verification_ttl_days") or ttl_days
        try:
            clean_ttl = max(1, int(trig_ttl))
        except (TypeError, ValueError):
            clean_ttl = research_archive.DEFAULT_VERIFICATION_TTL_DAYS

        ver_obj: dict[str, Any] = {
            "method": method if method in research_archive.VALID_VERIFICATION_METHODS else "ai_web",
            "status": status,
            "verified_at_utc": now_str,
            "ttl_days": clean_ttl,
            "evidence_tier": tier,
        }
        if obs is not None:
            ver_obj["observed"] = obs
        if obs_txt:
            ver_obj["observed_text"] = obs_txt[:300]
        if summary:
            ver_obj["summary"] = summary[:400]
        if src_title:
            ver_obj["source_title"] = src_title[:200]
        if src_url:
            ver_obj["source_url"] = src_url[:500]
        verifications[tid] = ver_obj

    return sanitize(verifications)


def _discover_target_archives(
    symbol: str | None,
    market: str | None,
    *,
    research_root: str | None = None,
    portfolio_dir: str | None = None,
) -> tuple[list[dict[str, Any]], list[str]]:
    """Resolve explicit target or discover all active holding archives (aligned with daily_sentinel)."""
    if symbol and symbol.strip():
        loaded = research_archive.load_archive(
            symbol.strip(), research_root=research_root, market_override=market
        )
        return [loaded], []

    open_positions, closed_positions, discovery_errors = daily_sentinel._load_all_positions(
        portfolio_dir=portfolio_dir
    )
    open_syms: set[str] = set()
    closed_only_syms: set[str] = set()
    candidates: list[tuple[str, str | None]] = []

    for pos in open_positions:
        raw_s = str(pos.get("symbol") or "").strip()
        raw_arch = str(pos.get("archive_symbol") or "").strip()
        raw_under = str(pos.get("underlying_symbol") or "").strip()
        mkt = str(pos.get("market") or "").strip() or None
        for tok in (raw_arch, raw_s, raw_under):
            if tok:
                ns, _ = daily_sentinel._safe_normalize_symbol(tok)
                open_syms.add(ns)
                open_syms.add(tok.upper())
        # Prefer explicit archive_symbol mapping (e.g. traded NI2701 -> archive NI0), then symbol/underlying
        lookup = raw_arch or raw_s or raw_under
        if lookup:
            candidates.append((lookup, mkt))
            if raw_s and raw_s != lookup:
                candidates.append((raw_s, mkt))

    for cpos in closed_positions:
        for tok in (str(cpos.get("archive_symbol") or "").strip(), str(cpos.get("symbol") or "").strip()):
            if not tok:
                continue
            ns, _ = daily_sentinel._safe_normalize_symbol(tok)
            if ns not in open_syms and tok.upper() not in open_syms:
                closed_only_syms.add(ns)
                closed_only_syms.add(tok.upper())

    for arc_meta in research_archive.list_archives(research_root=research_root):
        if arc_meta.get("error"):
            discovery_errors.append(
                f"research_archive ({arc_meta.get('archive_dir', '?')}): {arc_meta['error']}"
            )
            continue
        if str(arc_meta.get("status") or "").strip().lower() != "holding":
            continue
        sym = str(arc_meta.get("symbol") or "").strip()
        mkt = str(arc_meta.get("market") or "").strip() or None
        if not sym:
            continue
        ns, _ = daily_sentinel._safe_normalize_symbol(sym)
        if ns in closed_only_syms or sym.upper() in closed_only_syms:
            continue
        candidates.append((sym, mkt))

    out: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for sym, mkt in candidates:
        loaded = None
        try:
            loaded = research_archive.load_archive(
                sym, research_root=research_root, market_override=mkt
            )
        except FileNotFoundError:
            if mkt is not None:
                try:
                    loaded = research_archive.load_archive(sym, research_root=research_root)
                except FileNotFoundError:
                    continue
                except Exception as exc:  # noqa: BLE001
                    discovery_errors.append(f"research_archive ({sym}): 档案损坏无法读取 ({type(exc).__name__}: {exc})")
                    continue
            else:
                continue
        except Exception as exc:  # noqa: BLE001
            discovery_errors.append(f"research_archive ({sym}): 档案损坏无法读取 ({type(exc).__name__}: {exc})")
            continue
        if not isinstance(loaded, dict):
            continue
        key = (str(loaded.get("market") or ""), str(loaded.get("symbol") or ""))
        if key in seen:
            continue
        seen.add(key)
        out.append(loaded)
    return out, discovery_errors


def run_trigger_verification(
    params: dict[str, Any],
    emit: Emit,
    *,
    cancel_event: threading.Event | None = None,
    task_id: str | None = None,
) -> dict[str, Any]:
    """TaskManager runner: web-verify non-standard/policy sentinel triggers and persist back to state.json."""
    del task_id
    symbol = str(params.get("symbol") or "").strip() or None
    market = str(params.get("market") or "").strip() or None
    raw_tids = params.get("trigger_ids")
    trigger_ids = [str(x).strip() for x in raw_tids if str(x).strip()] if isinstance(raw_tids, list) else None
    force = bool(params.get("force", False))
    research_root = params.get("research_root")
    portfolio_dir = params.get("portfolio_dir")
    try:
        ttl_days = max(1, int(params.get("ttl_days") or research_archive.DEFAULT_VERIFICATION_TTL_DAYS))
    except (TypeError, ValueError):
        ttl_days = research_archive.DEFAULT_VERIFICATION_TTL_DAYS

    req_engine = str(params.get("engine") or "").strip().lower()
    engine_info = deep_research.detect_available_engines()
    if req_engine:
        if req_engine not in deep_research.VALID_ENGINES:
            raise ValueError(f"不支持的核验引擎 {req_engine!r}")
        if not deep_research.resolve_engine_binary(req_engine):
            raise RuntimeError(f"本机未检测到可用引擎 `{req_engine}`")
        engine = req_engine
    else:
        default_engine = engine_info.get("default_engine")
        if not default_engine:
            raise RuntimeError("本机未检测到可用 AI 核验引擎（Claude Code 或 Codex CLI）")
        engine = str(default_engine)

    def check_cancelled() -> None:
        if cancel_event is not None and cancel_event.is_set():
            raise deep_research.DeepResearchCancelledError("用户已取消触发器核验任务")

    check_cancelled()
    emit("step", {"stage": "discover", "message": "扫描待核验的政策/非标触发器…"})
    archives, discovery_errors = _discover_target_archives(
        symbol, market, research_root=research_root, portfolio_dir=portfolio_dir
    )
    if discovery_errors and not archives:
        raise RuntimeError(f"持仓发现失败：{'；'.join(discovery_errors)}")

    work_items: list[tuple[dict[str, Any], list[dict[str, Any]]]] = []
    skipped: list[dict[str, Any]] = []
    for arc in archives:
        state = arc.get("state") or {}
        candidates = find_verifiable_triggers(
            state, trigger_ids=trigger_ids, include_fresh=force
        )
        if candidates:
            work_items.append((arc, candidates))
        else:
            skipped.append({
                "symbol": arc.get("symbol"),
                "market": arc.get("market"),
                "reason": "无待核验的政策/非标触发器（均已自动机审或在核验有效期内）",
            })

    total = max(1, len(work_items))
    emit("progress", {"done": 0, "total": total})
    if not work_items:
        emit("log", {"stage": "done", "message": "所有持仓触发器均已完成核验，无需联网重查。"})
        emit("progress", {"done": total, "total": total})
        return sanitize({
            "engine": engine,
            "verified_targets": [],
            "updated_count": 0,
            "fired_count": 0,
            "clear_count": 0,
            "uncertain_count": 0,
            "conflict_count": 0,
            "skipped_conflicts": [],
            "missing_count": 0,
            "discovery_errors": discovery_errors,
            "skipped": skipped,
        })

    today_str = datetime.now(timezone.utc).date().isoformat()
    verified_targets: list[dict[str, Any]] = []
    all_conflicts: list[dict[str, Any]] = []
    updated_count = 0
    fired_count = 0
    clear_count = 0
    uncertain_count = 0
    missing_count = 0

    try:
        for idx, (arc, trigs) in enumerate(work_items, start=1):
            check_cancelled()
            sym = str(arc.get("symbol") or "")
            mkt = str(arc.get("market") or "")
            state = arc.get("state") or {}
            inst = state.get("instrument") or {}
            name = str(inst.get("name") or sym)
            asset_class = str(inst.get("asset_class") or "")

            # Snapshot definitions & full prior verification dicts BEFORE launching async CLI
            expected_fingerprints: dict[str, dict[str, Any]] = {}
            expected_prev_verifications: dict[str, dict[str, Any] | None] = {}
            for t in trigs:
                tid = str(t.get("id") or "").strip()
                if not tid:
                    continue
                expected_fingerprints[tid] = research_archive.trigger_definition_fingerprint(t)
                prev_v = t.get("verification") if isinstance(t.get("verification"), dict) else None
                expected_prev_verifications[tid] = dict(prev_v) if prev_v is not None else None

            emit(
                "step",
                {
                    "stage": "verify",
                    "symbol": sym,
                    "message": f"正在使用 {engine.upper()} 联网核验 {name}（{sym}）的 {len(trigs)} 条政策/非标触发器…",
                },
            )

            brief = {
                "symbol": sym,
                "market": mkt,
                "name": name,
                "asset_class": asset_class,
                "direction_bias": state.get("direction_bias"),
                "thesis_summary": state.get("thesis_summary"),
                "layer1_drivers": state.get("layer1_drivers"),
                "triggers_to_verify": [
                    {
                        "id": t.get("id"),
                        "kind": t.get("kind"),
                        "metric": t.get("metric"),
                        "operator": t.get("operator"),
                        "threshold": t.get("threshold"),
                        "action": t.get("action"),
                        "description": t.get("description") or "",
                        "previous_verification": t.get("verification"),
                    }
                    for t in trigs
                ],
            }

            with tempfile.TemporaryDirectory(prefix="t3_trigger_verify_") as tmpdir:
                brief_path = os.path.join(tmpdir, "verify_brief.json")
                with open(brief_path, "w", encoding="utf-8") as fh:
                    json.dump(brief, fh, ensure_ascii=False, indent=2)

                prompt = build_trigger_verify_prompt(
                    brief_path=brief_path,
                    symbol=sym,
                    market=mkt,
                    name=name,
                    asset_class=asset_class,
                    today_str=today_str,
                    triggers=trigs,
                )

                raw_text, meta = deep_research._run_agent_cli(
                    engine=engine,
                    prompt=prompt,
                    cwd=tmpdir,
                    effort="low",
                    emit=emit,
                    stage="verify",
                    timeout_seconds=VERIFY_TIMEOUT_SECONDS,
                    cancel_event=cancel_event,
                )

            check_cancelled()
            parsed = deep_research.extract_json_payload(raw_text)
            verifications = normalize_verification_payload(
                parsed, trigs, method="ai_web", ttl_days=ttl_days
            )
            if not verifications:
                raise RuntimeError(f"AI 核验未返回任何匹配 {sym} 触发器 ID 的结果")

            missing_ids = [tid for tid in expected_fingerprints if tid not in verifications]
            missing_count += len(missing_ids)

            persisted = research_archive.record_trigger_verifications(
                sym,
                verifications,
                research_root=research_root,
                market_override=mkt or None,
                expected_fingerprints=expected_fingerprints,
                expected_prev_verifications=expected_prev_verifications,
                reject_auto_metrics=True,
                cancel_check=(lambda: bool(cancel_event is not None and cancel_event.is_set())),
            )
            updated_ids = persisted["updated_ids"]
            conflicts = persisted.get("skipped_conflicts") or []
            for c in conflicts:
                all_conflicts.append({"symbol": sym, "market": mkt, **c})

            # Immediately invalidate Sentinel cache on every committed target so partial failures/cancels never leave stale greens!
            if updated_ids:
                CACHE.invalidate_prefix("sentinel")

            updated_count += len(updated_ids)
            log_parts: list[str] = []
            for tid in updated_ids:
                v = verifications[tid]
                st = v["status"]
                if st == "fired":
                    fired_count += 1
                elif st == "clear":
                    clear_count += 1
                else:
                    uncertain_count += 1
                log_parts.append(f"{tid}={st}({v.get('observed_text') or v.get('summary') or '已核验'})")
            for c in conflicts:
                log_parts.append(f"{c.get('id')}=跳过冲突({c.get('reason')})")

            if updated_ids:
                try:
                    research_archive.append_log(
                        sym,
                        f"AI 联网核验触发器（{engine.upper()}）：{'；'.join(log_parts)}",
                        research_root=research_root,
                        market_override=mkt or None,
                    )
                except Exception:
                    pass

            verified_targets.append({
                "symbol": sym,
                "market": mkt,
                "name": name,
                "updated_ids": updated_ids,
                "skipped_conflicts": conflicts,
                "missing_ids": missing_ids,
                "verifications": {k: verifications[k] for k in updated_ids},
                "engine_meta": meta,
            })
            emit(
                "log",
                {
                    "stage": "verify",
                    "symbol": sym,
                    "committed_symbols": [t["symbol"] for t in verified_targets if t["updated_ids"]],
                    "message": f"{name}（{sym}）核验完成：{'；'.join(log_parts)}",
                },
            )
            emit("progress", {"done": idx, "total": total})
    finally:
        if updated_count > 0:
            CACHE.invalidate_prefix("sentinel")

    emit(
        "step",
        {
            "stage": "done",
            "message": (
                f"触发器联网核验完成：更新 {updated_count} 条"
                f"（命中 {fired_count} / 未触发 {clear_count} / 待确认 {uncertain_count}"
                f"{f' / 冲突跳过 {len(all_conflicts)}' if all_conflicts else ''}"
                f"{f' / 未返回 {missing_count}' if missing_count else ''}）"
            ),
        },
    )
    return sanitize({
        "engine": engine,
        "verified_targets": verified_targets,
        "updated_count": updated_count,
        "fired_count": fired_count,
        "clear_count": clear_count,
        "uncertain_count": uncertain_count,
        "conflict_count": len(all_conflicts),
        "skipped_conflicts": all_conflicts,
        "missing_count": missing_count,
        "discovery_errors": discovery_errors,
        "skipped": skipped,
    })
