#!/usr/bin/env python3
"""Session resolver — deterministic ellipsis resolution for the context-free router.

策略（用户裁定 2026-09-18，SPEC v1.1 §多轮上下文策略）：
对话上下文永不进路由器 prompt（单轮契约不变，零 prefill 税、零重训）。
编排层保存最近一轮 {q, t, p}，对省略式追问做槽位替换/模板构造，
还原成独立问句再进路由器。不可解析片段原样直通——路由器预期判 c，
与判例 A（指代措辞一律 c）同侧，fail-safe。

规则优先级：ticker 替换 > 时间词替换 > 类别构造 > 指标构造。
"""

import re

TIME_WORDS = [
    "大前年", "前年", "去年", "今年", "明年",
    "上个季度", "上季度", "这季度", "这个季度", "那季度", "那个季度", "本季度", "下个季度",
    "上个月", "这个月", "本月", "下个月", "上月", "下月",
    "上周", "这周", "本周", "下周",
    "前天", "昨天", "今天", "明天",
]
TIME_RE = re.compile("|".join(sorted(TIME_WORDS, key=len, reverse=True)))
TICKER_RE = re.compile(r"[A-Za-z]{2,6}\.?[A-Za-z]{0,2}")  # prev 问句中的标的槽
FRAGMENT_TOKEN_RE = re.compile(r"^[A-Za-z0-9.:\- ]{1,10}$")

# 完整问句/不该碰的语义标记 → 一律不视为省略片段
COMPLETE_MARKERS = (
    "怎么", "什么", "为什么", "多少", "几", "哪", "如何", "帮我", "吗",
    "分析", "对比", "比较", "建议", "查", "买", "退", "转",
    "更", "最", "一点", "异常", "合理",
)
COMPARISON_RE = re.compile(r"[和跟与][^比]{0,6}比")  # 和去年比/跟它比——比较语义，勿当省略
# 指代/寒暄开头：判例 A 家族，规则不碰（→ 直通判 c）
DEIXIS_PREFIX = ("那", "上次", "最近", "刚才", "前面")
GREETINGS = {"晚安", "早安", "早上好", "你好", "谢谢", "再见", "嗯", "好的", "哦", "ok", "OK"}
SPEND_MARKERS = ("花了", "支出", "消费")
METRIC_MARKERS = ("是多少", "多少", "比例")


def _is_fragment(q: str) -> bool:
    core = q.rstrip("呢？?了吧 ")
    if not core or len(core) > 12:
        return False
    if any(m in q for m in COMPLETE_MARKERS) or COMPARISON_RE.search(q):
        return False
    return True


def _core(q: str) -> str:
    return q.rstrip("呢？?了吧 ").strip()


class SessionResolver:
    """编排层会话解析器。update() 喂【已解析的】上一轮问句+路由结果。"""

    def __init__(self):
        self.prev = None  # {"q": str, "t": str, "p": dict}

    def update(self, q: str, t: str, p: dict | None = None):
        self.prev = {"q": q, "t": t, "p": p or {}}

    def resolve(self, followup: str) -> str:
        return self._resolve(followup) or followup

    def _resolve(self, followup: str) -> str | None:
        if self.prev is None or not _is_fragment(followup):
            return None
        prev_q, prev_t = self.prev["q"], self.prev["t"]
        if prev_t not in ("tx", "pf", "mkt"):
            return None  # 云端轮次骨架不可替换（如理财建议后追「房贷呢」→ 直通判 c）
        core = _core(followup)
        m = TIME_RE.search(core)
        pure_time = m is not None and core == m.group(0)
        if core in GREETINGS:
            return None
        if core.startswith(DEIXIS_PREFIX) and not pure_time:  # 那个季度呢 = 纯时间词，放行
            return None

        # 1) ticker 替换：片段是代码形态 + 上一轮行情类
        if prev_t == "mkt" and FRAGMENT_TOKEN_RE.fullmatch(core) and re.search(r"[A-Za-z]", core):
            tickers = self.prev.get("p", {}).get("s") or []
            if tickers and tickers[0] in prev_q:
                return prev_q.replace(tickers[0], core.upper(), 1)
            slot = TICKER_RE.search(prev_q)  # 兜底：prev 问句最左拉丁串（AAPL的PE高吗 → AAPL）
            if slot:
                return prev_q[: slot.start()] + core.upper() + prev_q[slot.end():]

        # 2) 时间词替换：片段**就是**时间词 → 换/补上一轮时间槽（复合片段走规则 3）
        if pure_time:
            t1 = TIME_RE.search(prev_q)
            if t1:
                return prev_q[: t1.start()] + m.group(0) + prev_q[t1.end():]
            return m.group(0) + prev_q

        # 3) 类别构造：上一轮是消费类问句（花了/支出/消费）→ 时间骨架+新类别
        #    片段自带时间词时优先用片段的（「上个季度吃饭呢」→ 上个季度+吃饭）
        if prev_t == "tx" and any(s in prev_q for s in SPEND_MARKERS) and re.fullmatch(r"[一-鿿]{1,6}", core):
            if m:  # 时间+类别复合片段
                cat = core[m.end():].strip()
                return m.group(0) + cat + "花了多少" if cat else None
            t1 = TIME_RE.search(prev_q)
            return (t1.group(0) if t1 else "") + core + "花了多少"

        # 4) 指标构造：上一轮是指标型（X是多少/比例）→ 我的+新指标+是多少
        if prev_t in ("tx", "pf") and any(s in prev_q for s in METRIC_MARKERS) \
                and not any(s in prev_q for s in SPEND_MARKERS) \
                and re.fullmatch(r"[一-鿿]{1,6}", core):
            return "我的" + core + "是多少"

        return None  # 不可解析 → 直通（预期判 c）


# ---- 自检（本仓纪律：非平凡逻辑留一个可运行检查） ----
def _demo():
    r = SessionResolver()

    # 时间词替换（换槽 / 补槽）
    r.update("我这个月总共花了多少钱", "tx")
    assert r.resolve("上个月呢？") == "我上个月总共花了多少钱"
    # 缩写时间词不得掉进类别构造（实测回归：「上月」缺词表时构造出「这个月上月花了多少」，
    # 月份被 prev 的「这个月」锚死、「上月」当类别查空）
    r.update("这个月餐饮花了多少", "tx")
    assert r.resolve("上月呢") == "上月餐饮花了多少"
    r.update("我的净资产是多少", "pf")
    assert r.resolve("上个月呢") == "上个月我的净资产是多少"

    # ticker 替换
    r.update("QQQ现在多少钱", "mkt")
    assert r.resolve("vti呢") == "VTI现在多少钱"

    # 类别构造（有时间词 / 无时间词）
    r.update("本月餐饮花了多少", "tx")
    assert r.resolve("交通呢") == "本月交通花了多少"
    r.update("我在交通上花了多少", "tx")
    assert r.resolve("吃饭呢") == "吃饭花了多少"

    # 指标构造
    r.update("我的净资产是多少", "pf")
    assert r.resolve("总资产呢") == "我的总资产是多少"
    r.update("我的股债比例是多少", "pf")
    assert r.resolve("房产占比呢") == "我的房产占比是多少"  # 「比」不得误杀占比类片段
    r.update("我的储蓄率是多少", "tx")
    assert r.resolve("支出率呢") == "我的支出率是多少"

    # 时间词表覆盖这个季度；那字头纯时间词放行（OCR round1）
    r.update("这个季度话费花了多少", "tx")
    assert r.resolve("上个季度呢") == "上个季度话费花了多少"
    r.update("这个季度话费花了多少", "tx")
    assert r.resolve("那个季度呢") == "那个季度话费花了多少"
    # 复合片段（时间+类别）不丢类别槽（OCR round1）
    r.update("我这个月总共花了多少钱", "tx")
    assert r.resolve("上个季度吃饭呢") == "上个季度吃饭花了多少"
    # ticker 槽优先 prev p.s；兜底取最左拉丁串（OCR round1）
    r.update("AAPL的PE高吗", "mkt", {"s": ["AAPL"]})
    assert r.resolve("MSFT呢") == "MSFT的PE高吗"

    # 直通：完整问句不碰
    r.update("我这个月花了多少", "tx")
    full = "上个月我在吃饭上花了多少"
    assert r.resolve(full) == full
    assert r.resolve("什么是复利") == "什么是复利"
    assert r.resolve("现在几点了") == "现在几点了"

    # 直通：云端轮次后的片段 / 比较类 / 中文标的（诚实缺口）/ 指代 / 寒暄
    r.update("我工资卡里有38万该怎么理财", "c")
    assert r.resolve("房贷呢") == "房贷呢"
    r.update("我这个月花了多少", "tx")
    assert r.resolve("和去年比呢") == "和去年比呢"  # 比较语义
    r.update("贵州茅台今天涨了多少", "mkt")
    assert r.resolve("五粮液呢") == "五粮液呢"  # 中文标的不在规则内
    assert r.resolve("那笔工资到账没") == "那笔工资到账没"  # 指代（吗→完整句标记）
    assert r.resolve("那次大额消费呢") == "那次大额消费呢"  # 指代开头不构造
    assert r.resolve("晚安") == "晚安"  # 寒暄不构造


if __name__ == "__main__":
    _demo()
    print("session_resolver: all asserts passed" if __debug__ else "WARN: -O 剥掉了 asserts，本次未验证")
