#!/usr/bin/env python3
"""Redaction pipeline — docs/pc-p2-design.md 涂黑规则 v2（三路对抗评审后定稿）.

单遍管线，不用独立 regex（评审 H-1：1900–1999 出生者证件号 100% 内嵌电话形态
子串，独立 regex 互相打碎产生碎片泄漏）：
  候选数字串提取（分隔符/全角/中文音译）→ 归一 → 分类阶梯（长优先互斥）
  → 整段替换原始 span。
成文原则：涂黑目标 = 账本里不存在的身份 join key；账本（经令牌）本就可见的
信息（金额/订单号/时间戳/账户路径）一律放行——涂了零隐私收益纯损语义。
拒绝语义（§4）：仅内部不变量破坏抛 RedactError → 调用方拒绝上云（bug 金丝雀）。
"""

import re
import unicodedata

PLACEHOLDER = {"ID": "[ID]", "CARD": "[CARD]", "TEL": "[TEL]", "ADDR": "[ADDR]", "NAME": "[NAME]",
               "EMAIL": "[EMAIL]"}

# 分隔符形态学（评审 H-2：whisper 伪影与现实口语的主敌——空格/连字符/全角/顿号）
_SEPS = r"[\s\-—–.·,，、]"
_CAND_RE = re.compile(rf"[0-9０-９](?:{_SEPS}*[0-9０-９]){{10,}}")
# 中文数字音译连续段 ≥7（「幺三八」=大陆报号标准读法；金额带进位词不命中——评审 H-2）
_ZH_DIGIT_RE = re.compile(r"[幺一二两三四五六七八九零〇]{7,}")
_ZH_TO_DIGIT = str.maketrans("幺一二两三四五六七八九零〇", "1122345678900")

# 反例卫语（评审 M-4）：账本可见即放行
_CURRENCY = set("元块万亿分¥$")
_ORDER_WORDS = ("订单", "单号", "流水", "时间", "编号")
_DATE_SHAPE_RE = re.compile(r"^(?:19|20)\d{2}(?:0[1-9]|1[0-2])(?:0[1-9]|[12]\d|3[01])")

# 住址（评审 M-5）：省/市/区县链形态直接涂；短形态须领属卫语（商家地址在 dir 库，放行）
_ADDR_RE = re.compile(
    r"(?:[一-鿿]{1,3}省)?[一-鿿]{0,4}(?:市)?[一-鿿]{0,4}(?:区|县)?"
    r"[一-鿿]{0,8}(?:路|街|道|巷|弄)[0-9一二三四五六七八九十百]{0,10}号?"
    r"(?:[0-9一二三四五六七八九十百]{0,4}(?:楼|单元|室))?")
_ADDR_SHORT_RE = re.compile(
    r"[一-鿿]{1,8}(?:路|街|道|巷|弄)[0-9一二三四五六七八九十百]{0,10}号"
    r"(?:[0-9一二三四五六七八九十百]{0,4}(?:楼|单元|室))?")
_ADDR_CHAIN_RE = re.compile(r"省|市|区|县")
_POSSESSION = ("我家", "我住", "住在", "地址")

_ID_WEIGHTS = (7, 9, 10, 5, 8, 4, 2, 1, 6, 3, 7, 9, 10, 5, 8, 4, 2)
_ID_CHECK_MAP = "10X98765432"

# ---- #7 扩面（2026-09-24）：email + 非 CN 身份格式 ----
# 形状锚定臂（严格连字/分隔形态）：与订单号 doctrine 结构性解耦——CN 账本订单号为连续数字，
# 连续串身份输入按「歧义偏向放行」记接受遗漏（不降 _CAND_RE 连续 ≥11 位阈值）。
_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
_SSN_RE = re.compile(r"\b\d{3}-\d{2}-\d{4}\b")                      # 美国 SSN 严格连字形态
_US_TEL_RE = re.compile(r"(?:\b\d{3}[-.]\d{3}[-.]|\(\d{3}\)\s*\d{3}[-.])\d{4}\b")  # 美国电话三形态（\b 只锚数字起头支）
_MYNUMBER_W = (6, 5, 4, 3, 2, 7, 6, 5, 4, 3, 2)                    # 日本マイナンバー官方权重


def _mynumber_valid(d12: str) -> bool:
    """12 位 My Number mod-11 校验（余 0/1 → 校验位 0；否则 11−余）。"""
    q = sum(int(d) * w for d, w in zip(d12[:11], _MYNUMBER_W)) % 11
    return int(d12[11]) == (0 if q <= 1 else 11 - q)


class RedactError(Exception):
    """管线内部不变量破坏——调用方必须拒绝上云（设计 §4 bug 金丝雀）。"""


def id_check_char(prefix17: str) -> str:
    s = sum(int(d) * w for d, w in zip(prefix17, _ID_WEIGHTS))
    return _ID_CHECK_MAP[s % 11]


def _id_valid(digits18: str) -> bool:
    return digits18[17].upper() == id_check_char(digits18[:17])


def _normalize(raw: str) -> str:
    """候选 span → 纯数字串（NFKC 全角折半 + 剥非数字）。"""
    return re.sub(r"\D", "", unicodedata.normalize("NFKC", raw))


def _ledger_visible(digits: str, before: str, after: str) -> bool:
    """反例卫语：歧义偏向放行（身份类 vs 账本可见类 → 放行）。"""
    if after[:1] in _CURRENCY or before[-1:] in _CURRENCY:
        return True                                   # 金额：1380000138000分 / ¥138…
    if any(w in before[-6:] for w in _ORDER_WORDS):   # 订单号/流水/时间戳
        return True
    if _DATE_SHAPE_RE.match(digits):                  # 20260918… 日期形态
        return True
    if before[-1:] == ":" or after[:1] == ":":        # Assets:Bank:CMB:6222… 账户路径
        return True
    return False


def _classify_digits(digits: str, before: str, after: str):
    """纯数字串分类（长优先互斥）；返回 None = 放行。"""
    if _ledger_visible(digits, before, after):
        return None
    if len(digits) == 18:
        return "ID" if _id_valid(digits) else "CARD"  # 校验失败仍是 13–19 窗口敏感串，整段涂
    if 13 <= len(digits) <= 19:
        return "CARD"
    if len(digits) == 11 and re.match(r"^1[3-9]", digits):
        return "TEL"
    if len(digits) == 12 and _mynumber_valid(digits):              # #7：checksum 锚定才涂（纯 12 位流水照旧放行）
        return "ID"
    return None


def redact(text: str, known_names=()) -> tuple[str, dict]:
    """返回 (涂黑后文本, 统计)。known_names 命中 → [NAME]（设计 §3.1）。"""
    stats = {k: 0 for k in PLACEHOLDER}
    spans = []

    em_spans = [m.span() for m in _EMAIL_RE.finditer(text)]       # #7：email 身份 pre-pass（数字通道前）
    for s, e in em_spans:
        spans.append((s, e, "EMAIL"))
        stats["EMAIL"] += 1

    def _in_email(s, e):                                          # email 内嵌长数字与数字通道重叠 → 金丝雀误炸，跳过
        return any(s < ee and es < e for es, ee in em_spans)

    for m in _SSN_RE.finditer(text):                              # #7：形状锚定臂（不经 _classify——严格身份形态）
        if not _in_email(*m.span()):
            spans.append((*m.span(), "ID"))
            stats["ID"] += 1
    for m in _US_TEL_RE.finditer(text):
        if not _in_email(*m.span()):
            spans.append((*m.span(), "TEL"))
            stats["TEL"] += 1

    for m in _CAND_RE.finditer(text):
        if _in_email(*m.span()):
            continue
        start, end = m.span()
        digits = _normalize(m.group(0))
        kind = None
        # X/x 尾证件号（评审 M-6：字面 \d{18} 整条漏检）——17 数字 + X = 18 位 ID 形态
        if len(digits) == 17 and end < len(text) and text[end] in "Xx":
            full = digits + text[end].upper()
            if not _ledger_visible(digits, text[:start], text[end + 1:]):
                kind = "ID" if _id_valid(full) else "CARD"
                end += 1
        if kind is None:
            kind = _classify_digits(digits, text[:start], text[end:])
        if kind:
            spans.append((start, end, kind))
            stats[kind] += 1

    for m in _ZH_DIGIT_RE.finditer(text):
        start, end = m.span()
        if _in_email(start, end):
            continue
        kind = _classify_digits(m.group(0).translate(_ZH_TO_DIGIT),
                                text[:start], text[end:])
        if kind:
            spans.append((start, end, kind))
            stats[kind] += 1

    if any(a[0] < b[1] and b[0] < a[1]
           for i, a in enumerate(spans) for b in spans[i + 1:]):
        raise RedactError("overlapping candidate spans")   # 提取器不变量（bug 金丝雀）

    out = text
    for start, end, kind in sorted(spans, reverse=True):
        out = out[:start] + PLACEHOLDER[kind] + out[end:]

    def _addr_full(m):
        nonlocal stats
        if _ADDR_CHAIN_RE.search(m.group(0)):
            stats["ADDR"] += 1
            return PLACEHOLDER["ADDR"]
        return m.group(0)

    out = _ADDR_RE.sub(_addr_full, out)

    m = _ADDR_SHORT_RE.search(out)                     # 短形态 → 领属卫语（我家/我住/地址）
    # 贪婪中文前缀可能吞掉领属词（我家在浦东张扬路…），卫语查匹配点前 6 字或匹配头 6 字
    if m and (any(p in out[max(0, m.start() - 6):m.start()] for p in _POSSESSION)
              or any(p in m.group(0)[:6] for p in _POSSESSION)):
        out = out[:m.start()] + PLACEHOLDER["ADDR"] + out[m.end():]
        stats["ADDR"] += 1

    for name in known_names:                           # 姓名（设计 §3.1 known_names）
        if name and name in out:
            stats["NAME"] += out.count(name)
            out = out.replace(name, PLACEHOLDER["NAME"])

    return out, stats
