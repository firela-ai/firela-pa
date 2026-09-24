#!/usr/bin/env python3
"""数字对账门 — 架构裁决报告近期项 #2b / issue #3。

「不编造红线」的机器腿：l/c 生成输出里，凡对**个人财务数据作断言**的数字
（个人数据关键词邻近 × 数值 ∉ 问句自带 ∪ 知识白名单）→ 整答换诚实兜底。
纯概念示例数字（无个人断言）放行——HARVEST 首轮分布 l 的 57% 是概念咨询，
全面禁数字会杀合法示例（「比如 100 元年利率 3%」）。

与 redact.py 的关系：那边是身份形态检测器（≥11 位长数字串），金额是短数字，
提取器不共用；NFKC 折叠语义共享（redact._normalize 同款）。
范围 v0：l 与 c 生成腿。tx/pf/mkt 是模板腿（数字来自 API 构造保证）；
段2 解读腿尚不存在，模板门（G5）属后续件。
天花板（ponytail）：指数/点位编造未覆盖（加「指数」关键词会误杀「指数基金年化 8%」
类概念答）；白名单内小额编造（「余额 30 元」）漏过；无单位裸中文数字（「三」）
不进提取器。中文数字已进（2026-09-23 r2 §4-2 共享原语，有界文法：token 须以
十/百/千/万/亿 收尾——「三十万」✓、「万一」✗ 防误杀）；R1 判分器与本门共用
_nums/_ZH_NUM_RE 单实现，禁建第二份。
扩表走 telemetry 误杀/漏放记录。
"""

import re
import unicodedata

# 个人财务数据断言关键词（与数字邻近 = 在「报数」）
_PERSONAL = ("余额", "支出", "花", "收入", "存款", "资产", "负债", "净资",
             "持仓", "储蓄率", "账单", "工资", "欠", "结余", "报销", "薪", "股价")

# 知识白名单：FIRE/理财通识常数（panel「带时间戳知识数字」v0 集；4%=法则、25=倍年支出、
# 3/5/10=常见示例利率、20/30/50=储蓄率/仓位常见档）
_KNOWN = {4, 25, 3, 5, 10, 20, 30, 50}

_NUM_RE = re.compile(r"[0-9０-９][0-9０-９,，]*(?:\.[0-9０-９]+)?(?:万|亿)?")
# 中文数字（有界）：数字字起头或单位字起头，后随数字/单位字族；合法性由 _zh_to_value 判。
# 负向后顾排除「阿拉伯数字+万/亿」的裸单位字（「300万」已由 _NUM_RE 整体提取，裸「万」≠1e4——OCR R1）
_ZH_NUM_RE = re.compile(r"(?<![0-9０-９])[零一二两三四五六七八九十百千万亿][零一二两三四五六七八九十百千万亿]*")
_ZH_DIGITS = {"零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
              "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
_ZH_UNITS = {"十": 10, "百": 100, "千": 1000}
_WINDOW = 10                                     # 关键词↔数字邻近字符距

FALLBACK = ("（已拦截：这个回答里的数字我无法从你的问句或账本核实，怕误导就不说了。"
            "可以换个问法，或让我接通账本工具后再问。）")


def _norm_num(s):
    """匹配串 → 数值（剥千分位、万/亿 进位）；解析失败返回 None。"""
    s = s.replace(",", "").replace("，", "")
    mult = 1
    if s.endswith("万"):
        mult, s = 10_000, s[:-1]
    elif s.endswith("亿"):
        mult, s = 100_000_000, s[:-1]
    try:
        return float(s) * mult
    except ValueError:
        return None


def _zh_to_value(tok):
    """中文数字 token → 数值；有界文法，非法返回 None（调用方跳过）。

    规则：必须以 十/百/千/万/亿 收尾（排除「万一」「十三岁」类散文）；
    单位字前无数字按隐含「一」（十万=100000、百万=1000000）。
    层级（OCR R1）：万放大当前小节、亿把小节累计进总量的亿位——「三亿五千万」
    = 3e8 + 5000×1e4 = 3.05e8，不再把已有总量二次乘单位。
    """
    if not tok or tok[-1] not in "十百千万亿" or len(tok) > 9:
        return None
    total = section = cur = 0
    for ch in tok:
        if ch in _ZH_DIGITS:
            cur = _ZH_DIGITS[ch]
        elif ch in _ZH_UNITS:
            section += (cur or 1) * _ZH_UNITS[ch]
            cur = 0
        elif ch == "万":
            section = (section + cur) * 10_000
            cur = 0
        elif ch == "亿":
            total += (section + cur) * 100_000_000
            section = cur = 0
        else:
            return None
    v = total + section + cur
    return v if v > 0 else None


def _nums(text):
    """文本 → 数值集合（NFKC 折半角，与 redact._normalize 同语义；含中文数字支）。"""
    out = set()
    flat = unicodedata.normalize("NFKC", text)
    for m in _NUM_RE.finditer(flat):
        v = _norm_num(m.group(0))
        if v is not None:
            out.add(v)
    for m in _ZH_NUM_RE.finditer(flat):
        v = _zh_to_value(m.group(0))
        if v is not None:
            out.add(v)
    return out


def _violations(flat, allowed):
    """答案扁平文本里的违例数字（个人关键词邻近 × 数值 ∉ 溯源源）；两族提取器共用逻辑。

    归属匹配带相对容差（1e-6）——万/亿 进位的浮点噪声（「9.71万」=97100.00000000001
    ≠ round(97103.38,-2)=97100.0）是解析事故非语义差异，精确集合匹配会误杀（P8 实测）。
    """
    for finder in (_NUM_RE, _ZH_NUM_RE):
        for m in finder.finditer(flat):
            ctx = flat[max(0, m.start() - _WINDOW):m.end() + _WINDOW]
            if not any(k in ctx for k in _PERSONAL):
                continue                             # 概念示例数字，放行
            v = _norm_num(m.group(0)) if finder is _NUM_RE else _zh_to_value(m.group(0))
            if v is not None and not any(abs(v - a) <= 1e-6 * max(1.0, abs(a)) for a in allowed):
                yield m.group(0), v


def allowed_set(question, extra=None):
    """溯源允许集（gate 与 R1 判分器单源共用）：问句数字 ∪ 白名单
    （∪ 上下文数字与舍入锚，extra 在场时——P8；round(a,-k), k∈1..4：
    「约9.7万」=round(97103.38,-3)=97000 合法，云端自然派生形态，面板 D4 mod2。
    派生比率仍属文档化残余，A5 裁判管辖；仅 extra 在场时放宽）。"""
    allowed = _nums(question) | _KNOWN
    if extra:
        anchors = _nums(extra)
        allowed |= anchors | {round(a, -k) for a in anchors for k in (1, 2, 3, 4) if a >= 1}
    return allowed


def gate(question, answer, extra=None):
    """通过 → 原 answer；个人数据断言含不可溯源数字 → 兜底文案。

    extra（P8 带数据 c 腿）：上云数据上下文 JSON 串——见 allowed_set。
    """
    allowed = allowed_set(question, extra)
    flat = unicodedata.normalize("NFKC", answer)
    for _tok, _v in _violations(flat, allowed):
        return FALLBACK
    return answer
