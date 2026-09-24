#!/usr/bin/env python3
"""FIRE 模拟引擎 — P7.4（D3 裁定全集：口径三钉/参数化现金流/取崩栅栏/双语披露）。

口径（三钉）：全实际口径（今日购买力，实际收益=名义−通胀）；提款率=一等参数（默认 4%，
敏感度横扫 3/3.5/4%，>30 年期校准 3–3.5%）；年支出基数=trailing 12 个月（月对齐，含当月）。
现金流出自账本真值；post-tax 支出口径显式披露；房产/公积金/家庭合并 out-of-scope。
取崩栅栏（面板 P0-1）：常数收益引擎对 depletion 系统性单侧乐观——depletion 类问句只做
规则引用 + WR 横扫，禁自定义 depletion 投影（v1.5 压力序列桥另立排期）。
路径驱动：simulate 接收逐年收益路径列表，v1 传常数路径=退化情形；v1.5 换路径生成器。
"""

import datetime
import re

import sim_data as _SD          # 冻结历史表（1928-2025 实际收益；vintage 见该模块头）

DEFAULT_RETURN = 0.04          # 实际年化（保守默认）
DEFAULT_WR = 0.04              # 提款率（4% 规则；一等参数）
SIM_FORMULA_VERSION = "sim-v1.5"  # v1.5=+压力序列桥（数据表更动须 bump；P9 快照三元组用）
OUT_OF_SCOPE = "未计入：房产净值、公积金、家庭合并口径（这些需单独评估）"


def ledger_params(vlt, today):
    """账本真值 → 模拟参数（trailing 12 月窗，月对齐；CNY 口径）。

    单月超 Vlt 客户端单窗 500 行上限时该月被截断。服务端无窗口上限（vlt#1466 已闭
    2026-09-23：当时「~500 短页」实为 ACTIVE-only 默认过滤下的行数，非截断）——
    截断纯为客户端 max_n 政策，全量化修法见 firela-pa#17。截断月记入
    truncated_months：支出系统性低估 → 时间线只能作下界披露（禁静默乐观）。
    """
    ty, tm = today.year, today.month
    expense_cny = income_cny = 0.0
    truncated = []
    for k in range(12):
        t = ty * 12 + (tm - 1) - k
        yy, mm = t // 12, t % 12 + 1
        if (yy, mm) == (ty, tm):
            df, dt = f"{yy:04d}-{mm:02d}-01", today.isoformat()
        else:
            dt = (datetime.date(yy + (mm == 12), mm % 12 + 1, 1) - datetime.timedelta(days=1)).isoformat()
            df = f"{yy:04d}-{mm:02d}-01"
        page = vlt.transactions(df, dt, max_n=2000)   # 全保真月窗（#17 验收项1；重月>2000 时截断披露兜底）
        if page.get("truncated"):
            truncated.append(f"{yy:04d}-{mm:02d}")
        for t_ in page["data"]:
            for p in t_.get("postings", []):
                acct, u = str(p.get("account", "")), float(p.get("units", 0))
                if p.get("currency") != "CNY":
                    continue
                if acct.startswith("Expenses") and u > 0:
                    expense_cny += u
                elif acct.startswith("Income") and u < 0:
                    income_cny += -u
    accounts = [a for a in vlt.accounts().get("items", []) if a.get("type") in ("Assets", "Liabilities")]
    net_cny = 0.0
    for a in accounts:
        bal = vlt.balances(a.get("path", "")).get("balances") or {}
        net_cny += float(bal.get("CNY", 0) or 0)
    return {"annual_expense": expense_cny, "net_assets": net_cny,
            "annual_saving": income_cny - expense_cny, "currency": "CNY",
            "truncated_months": truncated}


def fire_number(params, wr=DEFAULT_WR):
    return params["annual_expense"] / wr


def simulate(params, wr=DEFAULT_WR, r=DEFAULT_RETURN, max_years=60):
    """year-loop：年初资产按实际收益复利 + 年末储蓄；返回达标年数（None=60 年内未达）。"""
    target = fire_number(params, wr)
    a, paths = params["net_assets"], [r] * max_years
    for year, pr in enumerate(paths, 1):                 # 路径驱动（v1 常数路径）
        a = a * (1 + pr) + params["annual_saving"]
        if a >= target:
            return year
    return None


_NO_EXPENSE = ("账本近 12 个月无支出记录——估不出 FIRE 目标（需先有账本数据，或账本未接通）。"
               "\n(EN) no expense records in trailing 12 months; cannot estimate.")


def narrate(params, wr=DEFAULT_WR, r=DEFAULT_RETURN):
    """双语叙述（假设披露 + 舍入规范 + WR 横扫 + 长期校准提示 + out-of-scope）。

    truncated_months 非空 → 主结论降为下界表述 + ⚠ 披露（乐观偏差 = 取崩栅栏同族风险）。
    """
    if params.get("annual_expense", 0) <= 0:                # 空账本退化解守卫（fire_number=0 →「1 年」荒谬）
        return _NO_EXPENSE
    years = simulate(params, wr, r)
    fn = fire_number(params, wr)
    sweep = {w: simulate(params, w, r) for w in (0.03, 0.035, 0.04)}
    trunc = params.get("truncated_months") or []
    if trunc:
        main = f"≥ 约 {years} 年（下界）" if years else "60 年内达不到（按当前收支，且支出已低估）"
    else:
        main = f"约 {years} 年" if years else "60 年内达不到（按当前收支）"
    en_main = (f"{years} years" if years else
               "goal not reached within 60 years under current income/spending")
    lines = [
        f"按你的账本：年支出（trailing 12 个月，post-tax 口径）{params['annual_expense']:,.0f} {params['currency']}，"
        f"净资产 {params['net_assets']:,.0f}，年储蓄 {params['annual_saving']:,.0f}。",
    ]
    if trunc:
        lines.append(f"⚠ 追踪窗内 {('、'.join(trunc))} 触达单次查询 500 行上限，年支出被低估"
                     "——以下时间线仅为下界，实际会更长（数据完整性修复前无法给出上界）")
    lines += [
        f"按提款率 {wr * 100:.1f}%（4% 规则源于美国历史序列，非保证），FIRE 目标约 {fn:,.0f}——"
        f"实际口径（今日购买力）、实际年化 {r * 100:.0f}% 假设下：**{main}**。",
        "敏感度（提款率横扫）：3% → 约 {} 年 · 3.5% → 约 {} 年 · 4% → 约 {} 年。".format(
            sweep[0.03] or "—", sweep[0.035] or "—", sweep[0.04] or "—"),
        (f"⚠ 期限 >30 年属超长退休期，实务校准建议提款率 3–3.5%。{OUT_OF_SCOPE}"
         if (years or 99) > 30 else OUT_OF_SCOPE),
        (f"(EN) ≥ at least {en_main} (data-capped lower bound) to financial independence "
         f"at {wr * 100:.1f}% withdrawal, real terms, {r * 100:.0f}% real return assumption."
         if trunc and years else
         f"(EN) ≈ {en_main}{' (data-capped lower bound)' if trunc else ''} to financial independence "
         f"at {wr * 100:.1f}% withdrawal, real terms, {r * 100:.0f}% real return assumption."),
    ]
    return "\n".join(lines)


# ---------- v1.5 压力序列桥（面板终裁 A′：命名序列为主 + 窗口枚举成功率单行） ----------

_HORIZON = 30                    # 默认期限（profile 无当前年龄不可个性化——竞速行开放项延续）
_BLEND_W = 0.5                   # 50/50 年度再平衡（校准闸绑定此配比——改动须过 Trinity 带断言）


def _blend(y):
    """50/50 年度再平衡混合实际收益（等权算术平均近似年初再平衡）。"""
    sp, bd = _SD.REAL[y]
    return _BLEND_W * sp + (1 - _BLEND_W) * bd


def _years_list():
    return sorted(_SD.REAL)


def depletion(params, wr, horizon, path):
    """提款期 year-loop：a = a*(1+r) − 年支出；返回撑过年数（horizon=存活）。

    与 simulate() 同形状的路径驱动（P0-1「新路径生成器而非重写」）。
    path = 逐年组合实际收益列表。
    """
    a = params["net_assets"]
    e = params["annual_expense"]
    for year, r in enumerate(path, 1):
        a = a * (1 + r) - e
        if a < 0:
            return year - 1
    return horizon


def _terminal_wealth(start, horizon, wr):
    """排序用：起始年 start 的期末财富（归一，提款=wr×初始）。"""
    ys = _years_list()
    i = ys.index(start)
    if i + horizon > len(ys):
        return None
    a, e = 1.0, wr
    for k in range(horizon):
        a = a * (1 + _blend(ys[i + k])) - e
    return a


def named_sequences(wr=DEFAULT_WR, horizon=_HORIZON):
    """机械选取（禁手挑）：按期末财富排名全部起始年 → 最差/较差/中位/最佳 4 条。

    返回 [(起始年, 撑过年数|None 表示窗口不足不计), ...] 四元组标签序。
    """
    ys = _years_list()
    ranked = sorted((s for s in ys if _terminal_wealth(s, horizon, wr) is not None),
                    key=lambda s: _terminal_wealth(s, horizon, wr))
    picks = [ranked[0], ranked[len(ranked) // 4], ranked[len(ranked) // 2], ranked[-1]]
    seen, out = set(), []
    for s in picks:
        if s in seen:
            continue
        seen.add(s)
        path = [_blend(y) for y in ys[ys.index(s):ys.index(s) + horizon]]
        out.append((s, depletion({"net_assets": 1.0, "annual_expense": wr}, wr, horizon, path)))
    return out


def window_success_rate(wr=DEFAULT_WR, horizon=_HORIZON):
    """滚动枚举成功率（FIRECalc 式）：全部完整起始窗存活占比，圆整 5% 封顶 95。

    全确定性（零 RNG）——计数可手工复算 = R3 独立 oracle（面板 eval 异议的消解路径）。
    """
    ys = _years_list()
    total = ok = 0
    for i in range(len(ys) - horizon + 1):
        path = [_blend(y) for y in ys[i:i + horizon]]
        total += 1
        ok += depletion({"net_assets": 1.0, "annual_expense": wr}, wr, horizon, path) >= horizon
    if not total:
        return None
    return min(95, int(round(ok / total * 20)) * 5)   # 圆整 5%，封顶 95（永不 100）


def depletion_reply(params):
    """取崩栅栏（P0-1）：只做规则引用 + WR 横扫，禁投影。"""
    if params.get("annual_expense", 0) <= 0:
        return _NO_EXPENSE
    wr = DEFAULT_WR
    seqs = named_sequences(wr, _HORIZON)
    rate = window_success_rate(wr, _HORIZON)
    lines = ["\n「退休后钱够不够花」由收益顺序（序列风险）决定——按你的净资产与年支出，"
             f"对美国 1928-2025 历史（50/50 股债、实际口径、{_HORIZON} 年提款期）逐段回放："]
    for start, years in seqs:
        lived = "全程存活" if years >= _HORIZON else f"撑 {years} 年后取崩"
        lines.append(f"  {start} 年开局（{lived}）")
    if rate is not None:
        lines.append(f"成功率：约 {rate}% 的历史 30 年窗在 {wr * 100:g}% 提款下存活"
                     "（滚动枚举全部起始窗——非未来概率预测）。")
    lines.append("口径披露：美国历史数据（对中文用户偏乐观）；费前收益；重叠窗非独立样本。")
    lines.append(f"提款率越低越稳——按你的年支出，3% 目标约 {fire_number(params, 0.03):,.0f}、"
                 f"3.5% 约 {fire_number(params, 0.035):,.0f}、4% 约 {fire_number(params, 0.04):,.0f}。")
    if seqs:
        w0 = seqs[0]
        lines.append(f"(EN) Replaying US 1928-2025 history ({_HORIZON}y, 50/50 real): worst start "
                     f"{w0[0]}, survived {w0[1]} of {_HORIZON} years"
                     + (f", ~{rate}% of windows survived at {wr * 100:g}%" if rate is not None else "")
                     + ". US-data, pre-fee caveats apply.")
    if params.get("truncated_months"):
        lines.append("⚠ 数据上限：年支出可能被低估，上述存活年数偏乐观。")
    return "\n".join(lines)


# ---------- D7 拦截层：模拟会话状态（上下文永不进路由器） ----------
# ponytail: STATE 是模块级单例——单用户 PC 形态（server 绑 127.0.0.1）下会话并发不存在；
# 与 _JWT_CACHE 同款权衡。多用户/多会话服务化时升级为 per-session 键控（D7 设计留位）。

class SimState:
    """模块级会话态：参数快照 + 增量修改。TTL=3 个非模拟轮；换话题（新 FIRE 首问）重置。"""

    def __init__(self, params):
        self.params = dict(params)
        self.wr, self.r = DEFAULT_WR, DEFAULT_RETURN
        self.shocked_assets = None          # one-shot 冲击（不写回 params）
        self.ttl = 3

    def effective_params(self):
        p = dict(self.params)
        if self.shocked_assets is not None:
            p["net_assets"] = self.shocked_assets
        return p

    def mutate(self, q):
        """what-if 片段 → 命中返回 (说明, True)；未命中返回 (None, False) 并扣 TTL。"""
        m = re.search(r"(跌了|跌到|跌下|跌去|跌|涨了|涨到|涨)\s*(\d+(?:\.\d+)?)\s*%", q)
        if m:
            x = float(m.group(2)) / 100
            base = self.shocked_assets if self.shocked_assets is not None else self.params["net_assets"]
            verb = m.group(1)
            if "到" in verb:                          # 涨到/跌到 X% = 变至原值的 X%（水平语义，两向对称）
                self.shocked_assets = base * x
            elif verb.startswith("涨"):
                self.shocked_assets = base * (1 + x)
            else:                                     # 跌 X% = 下跌幅度 X%
                self.shocked_assets = base * (1 - x)
            self.ttl = 3
            return f"已按一次性冲击重算：净资产调整为 {self.shocked_assets:,.0f}", True
        m = re.search(r"(\d+(?:\.\d+)?)\s*%\s*(?:的)?\s*(提款|提取|年化|收益|利率|回报)", q) or \
            re.search(r"(?:按|按照)\s*(\d+(?:\.\d+)?)\s*%", q)
        if m and 0 < float(m.group(1)) <= 100:        # 0% 提款=除零、>100% 退化——按未命中
            x = float(m.group(1)) / 100
            if "提款" in q or "提取" in q or (m.re.groups >= 2 and m.group(2) in ("提款", "提取")):
                self.wr = x
                note = f"提款率调整为 {x * 100:.1f}%"
            else:
                self.r = x
                note = f"实际年化假设调整为 {x * 100:.1f}%"
            self.ttl = 3
            return note, True
        self.ttl -= 1
        return None, False


STATE = None


def reset(params):
    global STATE
    STATE = SimState(params)
    return STATE
