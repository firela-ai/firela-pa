#!/usr/bin/env python3
"""主动审计引擎 — P10（D10 裁定 2026-09-23，单实现：三薄入口共用本模块）。

规则（零 LLM，确定性；阈值=教育性缺省+config 可覆写+telemetry-gated 复审）：
  ① overspend  当月 CNY 支出 vs **含零**前完成月中位数 > audit_overspend_pct%（默认 30）；
               median==0 且当月>0 → 「基线近零」形态（不除零）；<3 基线月=冷启动静默+披露
  ② large      单笔支出 > 12m 单笔中位 × audit_large_mult（默认 10，过支出过滤——投资买入/
               转账非支出）；回看窗=当月+min(3, 距上次审计月数)（首跑 3）；<10 基线笔=冷启动
  ③ milestone  净资产/fire_number 跨 25/50/75/100% 或 sim 年限 ±2（None 三态：None↔finite=
               触发）；快照 wr≠当前 wr → 重基线不触发（防假设变更伪跨越）；首跑只存快照
去重（fired/shown 合一）：rule 有 event ts∈30 天或当月 → 新触发跳过（重显归 last report 存档）。
时间一律 orchestrator._TODAY()（eval 时钟缝可控）；ts=日期串（30 天/月度数学精确）。
存储：第三库 ~/.firela-pa/audit.db（0600/禁 WAL/env FIRELA_AUDIT_DB import 前落位；
per-call con、零模块级缓存——eval 题间 unlink 隔离的前提）。失败仅 stderr（审计永不
打断主路径——降级=顺延下次启动）。
egress：审计文本禁入上云载荷（send_to_relay audit_context 绊线，D10 预裁）。
"""

import json
import os
import sqlite3
import statistics
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import orchestrator as O  # _month_bounds/_win_txns/_tx_expense/_TODAY/Vlt；orchestrator 侧须 lazy import audit（环引守卫）

DB = os.environ.get("FIRELA_AUDIT_DB", str(Path.home() / ".firela-pa" / "audit.db"))

DISCLAIMER = "（AI tool, not a licensed financial advisor——以上为教育性观察，非投资建议。）"
_MILESTONES = (0.25, 0.50, 0.75, 1.00)


def _con():
    Path(DB).parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB, timeout=5)
    con.execute("CREATE TABLE IF NOT EXISTS state (key TEXT PRIMARY KEY, value TEXT)")
    con.execute("CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY AUTOINCREMENT, "
                "rule TEXT, ts TEXT, detail TEXT)")
    return con


def _get_state(key):
    if not Path(DB).exists():
        return None
    con = _con()
    row = con.execute("SELECT value FROM state WHERE key=?", (key,)).fetchone()
    con.close()
    return row[0] if row else None


def _put_state(key, value):
    con = _con()
    con.execute("INSERT INTO state (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))
    con.commit()
    con.close()
    os.chmod(DB, 0o600)


def _recent_event(rule, today):
    """30 天内或当月已有 event（fired/shown 合一）→ True。"""
    if not Path(DB).exists():
        return False
    con = _con()
    rows = [r[0] for r in con.execute("SELECT ts FROM events WHERE rule=?", (rule,))]
    con.close()
    from datetime import date
    for ts in rows:
        try:
            d = date.fromisoformat(ts)
        except ValueError:
            continue
        if (today - d).days < 30 or (d.year, d.month) == (today.year, today.month):
            return True
    return False


def _mark(rule, today, detail=""):
    con = _con()
    con.execute("INSERT INTO events (rule, ts, detail) VALUES (?, ?, ?)",
                (rule, today.isoformat(), detail))
    con.commit()
    con.close()
    os.chmod(DB, 0o600)


def _cfg_f(cfg, key, default, lo):
    """config 阈值（非法值回缺省+stderr，永不崩）。"""
    v = cfg.get(key, default)
    try:
        v = float(v)
        if v <= lo:
            raise ValueError
    except (TypeError, ValueError):
        print(f"[审计] 配置 {key}={v!r} 非法，回缺省 {default}", file=sys.stderr)
        return float(default)
    return v


def _month_pull(vlt, y, m, today):
    """单月 (CNY 支出额, 单笔支出额列表, truncated, 行数)。"""
    rows, trunc = O._win_txns(vlt, *O._month_bounds(y, m, today))
    amt, singles = 0.0, []
    for t in rows:
        a = sum(float(p.get("units", 0) or 0) for p in t.get("postings", [])
                if str(p.get("account", "")).startswith("Expenses") and p.get("currency") == "CNY")
        if a > 0:
            amt += a
            singles.append(a)
    return amt, singles, trunc, len(rows)


def run_audit(vlt, today, cfg):
    """跑一批审计 → (findings[dict], notes[str])。调用方负责渲染与降级。"""
    import sim
    import memory
    findings, notes = [], []
    last_ts = _get_state("last_audit_ts")
    last_audit_month = None
    if last_ts:
        from datetime import date
        try:
            d = date.fromisoformat(last_ts)
            last_audit_month = d.year * 12 + d.month - 1
        except ValueError:
            pass

    # ---- 月窗数据（12+当月，并发 8——branch_pf 先例）----
    y0, m0 = today.year, today.month
    months = [(y0 * 12 + (m0 - 1) - k) for k in range(13)]        # 当月 + 前 12 完成月
    with ThreadPoolExecutor(max_workers=8) as ex:
        pulled = list(ex.map(lambda tm: _month_pull(vlt, tm // 12, tm % 12 + 1, today), months))
    cur_amt, cur_singles, _, _ = pulled[0]
    base = pulled[1:]                                             # 前 12 完成月（含零月）
    base_amts = [b[0] for b in base]
    base_trunc = [months[i + 1] for i, b in enumerate(base) if b[2]]
    months_with_rows = sum(1 for b in base if b[3] > 0)           # 冷启动判定：有交易的基线月数

    # ---- ① overspend（当月 only：轨迹规则回看无行动价值——D10 收口解释）----
    if months_with_rows >= 3:
        med = statistics.median(base_amts)
        if base_trunc:
            notes.append(f"基线含截断月（{'、'.join(f'{tm // 12:04d}-{tm % 12 + 1:02d}' for tm in base_trunc)}）——超支规则本轮压制")
        elif med == 0:
            if cur_amt > 0:
                f1 = {"rule": "overspend",
                      "text": f"本月（{today:%Y-%m}）已支出 {cur_amt:,.2f} CNY，而近 {len(base_amts)} 个月支出中位数为 0"
                              "——基线近零下任何支出都值得看一眼（若月度预算本就为 0，忽略此条）。"}
                if not _recent_event("overspend", today):
                    findings.append(f1)
                    _mark("overspend", today, f"{cur_amt}")
        elif cur_amt > med * (1 + _cfg_f(cfg, "audit_overspend_pct", 30, 0) / 100):
            f1 = {"rule": "overspend",
                  "text": f"本月（{today:%Y-%m}）支出 {cur_amt:,.2f} CNY，高于近 {len(base_amts)} 个月中位数 "
                          f"{med:,.2f} 超过 {int(_cfg_f(cfg, 'audit_overspend_pct', 30, 0))}%"
                          "——教育层面：先复盘大额类目再谈压缩。"}
            if not _recent_event("overspend", today):
                findings.append(f1)
                _mark("overspend", today, f"{cur_amt}")
    else:
        notes.append("支出基线不足 3 个完成月——超支规则次月生效")

    # ---- ② large（点事件：回看窗）----
    win_n = 3 if last_audit_month is None else max(1, min(3, (y0 * 12 + m0 - 1) - last_audit_month))
    win_months = months[:win_n]
    win_trunc = [months[i] for i in range(win_n) if pulled[i][2]]
    singles_all = [s for b in base for s in b[1]]
    if len(singles_all) < 10:
        notes.append("单笔基线不足 10 笔——大额规则暂静默")
    elif win_trunc:
        notes.append(f"回看窗含截断月——大额规则本轮压制")
    else:
        med_s = statistics.median(singles_all)
        mult = _cfg_f(cfg, "audit_large_mult", 10, 1)
        seen = set()
        for i in range(win_n):
            for s in pulled[i][1]:
                if s > med_s * mult and s not in seen:
                    seen.add(s)
                    if not _recent_event("large", today):
                        tm = months[i]
                        findings.append({"rule": "large",
                                         "text": f"{tm // 12:04d}-{tm % 12 + 1:02d} 有一笔 {s:,.2f} CNY 支出，"
                                                 f"高于近 12 个月单笔中位数 {med_s:,.2f} 的 {mult:g} 倍"
                                                 "——确认是否在计划内。"})
                        _mark("large", today, f"{s}")
    # ---- ③ milestone ----
    try:
        params = sim.ledger_params(vlt, today)
        prof = memory.get_profile()
        wr = prof.get("wr", sim.DEFAULT_WR)
        r = prof.get("real_return", sim.DEFAULT_RETURN)
        if "wr" not in prof:
            notes.append(f"里程碑按缺省提款率 {sim.DEFAULT_WR * 100:.0f}% 计算（无假设档案）")
        fn = sim.fire_number(params, wr)
        years = sim.simulate(params, wr, r)
        snap = {"as_of": today.isoformat(), "net_assets": params["net_assets"],
                "annual_expense": params["annual_expense"], "fire_number": fn,
                "wr": wr, "r": r, "sim_years": years,
                "truncated_months": params.get("truncated_months", []),
                "formula_version": sim.SIM_FORMULA_VERSION}
        prev_raw = _get_state("sim_snapshot")
        if prev_raw is None:
            notes.append("已建立 FIRE 基线快照——里程碑规则自下次审计生效")
        else:
            try:
                prev = json.loads(prev_raw)
            except ValueError:
                prev = None
            if prev:
                if abs(prev.get("wr", wr) - wr) > 1e-9:
                    notes.append("提款率假设已变更——里程碑基线重置（本次不判跨越）")
                else:
                    prev_prog = prev.get("net_assets", 0) / prev["fire_number"] if prev.get("fire_number") else 0
                    cur_prog = params["net_assets"] / fn if fn else 0
                    for ms in _MILESTONES:
                        if prev_prog < ms <= cur_prog and not _recent_event("milestone", today):
                            findings.append({"rule": "milestone",
                                             "text": f"FIRE 目标进度跨过 {int(ms * 100)}%（净资产 {params['net_assets']:,.2f} / "
                                                     f"目标 {fn:,.2f}）——教育层面：里程碑后常见动作是复核提款率假设。"})
                            _mark("milestone", today, f"{ms}")
                            break
                    py, cy = prev.get("sim_years"), years
                    if (py is None) != (cy is None) or (py is not None and cy is not None and abs(cy - py) >= 2):
                        if not _recent_event("milestone", today):
                            txt = (f"FIRE 达标年限从 {'60 年内不可达' if py is None else f'约 {py} 年'} 变为 "
                                   f"{'60 年内不可达' if cy is None else f'约 {cy} 年'}")
                            findings.append({"rule": "milestone",
                                             "text": txt + "——变化超过 2 年，值得看一眼收支或假设哪边动了。"})
                            _mark("milestone", today, "years")
        _put_state("sim_snapshot", json.dumps(snap, ensure_ascii=False))
    except Exception as e:                                        # 里程碑失败不拖垮整批（其余规则照出）
        print(f"[审计] 里程碑规则失败（{type(e).__name__}: {e}）", file=sys.stderr)
        notes.append("里程碑规则本轮跳过（账本参数不可得）")

    _put_state("last_audit_ts", today.isoformat())
    return findings, notes


def render(findings, notes, today):
    """报告渲染（教育语气+完整数字+disclaimer；空/冷启动=诚实报告）。"""
    lines = [f"主动审计（{today:%Y-%m-%d}，覆盖近月账本）："]
    if findings:
        lines += [f"  ⚠ {f['text']}" for f in findings]
    else:
        lines.append("  本轮无新触发。")
    lines += [f"  · {n}" for n in notes]
    lines.append(f"  {DISCLAIMER}")
    return "\n".join(lines)


def audit_report(vlt, today, cfg):
    """运行+渲染+存档（供三入口共用；findings 为空也存——重显走存档）。"""
    findings, notes = run_audit(vlt, today, cfg)
    text = render(findings, notes, today)
    _put_state("last_report", json.dumps({"text": text, "as_of": today.isoformat()},
                                         ensure_ascii=False))
    return text, findings


def stored_report(today):
    """30 天内存档重显（注「已提示」）——防 PAGE 刷新烧静默；过期返 None。"""
    raw = _get_state("last_report")
    if not raw:
        return None
    try:
        rep = json.loads(raw)
        from datetime import date
        d = date.fromisoformat(rep["as_of"])
    except (ValueError, KeyError):
        return None
    if (today - d).days >= 30:
        return None
    return rep["text"] + "\n（此报告此前已提示；30 天内同规则不重复触发。）"


def startup_audit(cfg):
    """REPL 启动补审（仅 REPL——argv/serve/voice 分支已 return）。失败=stderr+顺延。"""
    try:
        text, findings = audit_report(O.Vlt(cfg), O._TODAY(), cfg)
        if findings:
            print("\n" + text + "\n")
        import telemetry
        telemetry.record("audit", "（启动补审）", "audit", 0, answer_len=len(text), source="system")
    except Exception as e:
        print(f"[审计] 启动补审未完成（{type(e).__name__}: {e}）——顺延下次启动", file=sys.stderr)
