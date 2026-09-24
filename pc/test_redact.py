#!/usr/bin/env python3
"""涂黑器攻击样本测试 — docs/pc-p2-design.md 验收 2。

Fixture = 三路对抗评审产出（15 条攻击 + 3 条姓名 + 反例必须原样通过）。
跑法：python3 pc/test_redact.py
"""

import sys

from redact import redact, id_check_char

# 手算校验位独立验证（不信任被测模块的 id_check_char——一次手工核对）
assert id_check_char("11010519491231002") == "X", "checksum map must match GB11643 hand calc"

FAILS = []


def _no_digit_fragments(out):
    """零碎片要求（H-1）：剥分隔符后不得残留 ≥4 位连续数字。"""
    import re
    return re.search(r"\d{4,}", re.sub(r"[\s\-—–.·,，、]", "", out)) is None


def hits(payload, expect_kind, expect_fragment, known_names=()):
    out, stats = redact(payload, known_names)
    ok = (stats.get(expect_kind, 0) >= 1 and expect_fragment not in out
          and _no_digit_fragments(out))
    if not ok:
        FAILS.append(("MISS", expect_kind, payload, out))
    return out


def passes(payload, known_names=()):
    """反例：必须原样通过（过度涂黑=损语义=验收失败）。"""
    out, _ = redact(payload, known_names)
    if out != payload:
        FAILS.append(("OVER-REDACT", "", payload, out))


if __name__ == "__main__":   # main-guard：使 _no_digit_fragments 等助手可被 eval_agent 复用（P6a）
    # ---- H-1：证件号内嵌电话形态子串（1900–1999 出生者 100% 命中）----
    # 校验位手算：11010519491231002 → X（上面已独立断言）
    hits("我的身份证号是11010519491231002X，名下有几笔贷款", "ID", "110105")
    # 攻击样本原串（尾位 1，校验不过）——仍须整段涂（13–19 窗口 → CARD），零数字碎片
    out, stats = redact("我的身份证号是110105194912310021，名下有几笔贷款")
    if stats.get("CARD", 0) < 1 or not _no_digit_fragments(out):
        FAILS.append(("MISS", "CARD/fragment", "110105194912310021", out))

    # ---- H-2：分隔符 / 全角 / 中文音译形态学（whisper 伪影）----
    hits("帮我查 138 0013 8000 这个号注册的账户消费", "TEL", "13800138000")
    hits("我手机是138-0013-8000帮我查话费", "TEL", "13800138000")
    hits("我卡号是6222-0210-0000-0000看看今天扣款没", "CARD", "6222021000000000")
    hits("卡号622 0210 0000 0000", "CARD", "622021000000000")
    hits("卡号6222　0210　0000　0000", "CARD", "6222021000000000")
    hits("我的手机号幺三八零零幺三八零零零，帮我分析下话费支出", "TEL", "幺三八")
    hits("手机１３８００１３８０００是谁的", "TEL", "13800138000")

    # ---- M-4：反例——账本可见即放行（涂了零隐私收益纯损语义）----
    passes("帮我查订单号 3719283746501928 那笔退款到账没")
    passes("看下我 2026091814301 那笔转账")
    passes("手续费一共1380000138000分")
    passes("Assets:Bank:CMB:6222021000000000 这个账户和现金的比例合理吗")
    passes("工资卡里有38万该怎么理财")            # 金额+银行（v2 明文允许）
    passes("招行卡还剩多少")

    # ---- M-5：住址——短形态须领属卫语，商家地址放行 ----
    hits("我家在浦东张扬路五百号，帮我算下房贷", "ADDR", "张扬路")
    passes("帮我查人民路3号那家面馆的消费")       # 无卫语=商家地址（dir 库可见）
    hits("我住在南山区科技园南路15号，算下房贷", "ADDR", "科技园南路")

    # ---- M-6：X 尾证件号（字面 \d{18} 会整条漏检）----
    _valid_id = "11010119900307861" + id_check_char("11010119900307861")
    hits(f"身份证号{_valid_id}查下我能贷多少", "ID", "110101")

    # ---- H-3：姓名（known_names 路径——设计 §3.1 默认 onboarding）----
    hits("我叫王小芳，帮我看看净资产38万该怎么配置", "NAME", "王小芳", known_names=["王小芳"])
    hits("帮我把给我妈张桂花的代缴水电费都找出来", "NAME", "张桂花", known_names=["张桂花"])
    hits("我儿子张小宝的教育金该买什么", "NAME", "张小宝", known_names=["张小宝"])
    # 姓名清单外 = 已文档化缺口（不在此断言拦截——评审接受的残差）

    # ---- #7 扩面（2026-09-24）：email + SSN + 美国电话 + My Number ----
    hits("我的邮箱 zhang.san@example.com 帮我看看资产", "EMAIL", "zhang.san@example.com")
    hits("发账单到 a123456789012@mai1.cn 这个邮箱", "EMAIL", "a123456789012")  # 内嵌 12 位数字不炸重叠金丝雀
    hits("我的 SSN 是 123-45-6789 帮我查信用", "ID", "123-45-6789")
    hits("电话 (415) 555-2671 有人冒用我的身份", "TEL", "415")
    hits("call me at 415.555.2671 about my accounts", "TEL", "415.555.2671")
    hits("我的マイナンバー是 123456789018 请核对", "ID", "123456789018")      # 官方示例号（mod-11 校验过）
    # 反例（订单号 doctrine 不破）：连续 12 位非校验和=流水放行；校验失败的 12 位放行；连字日期不中 SSN
    passes("订单 374829103826 已发货")
    passes("流水 123456789011 已入账")                                            # My Number 校验不过（末位应 8）
    passes("账期 2026-09-15 结算")
    passes("编号 20260915120030 通过")                                            # 14 位时间戳形态

    if FAILS:
        for kind, want, payload, out in FAILS:
            print(f"[{kind}] expect={want}\n  in : {payload}\n  out: {out}")
        sys.exit(1)
    print(f"test_redact: all attack fixtures passed "
          f"(15 attack + 6 reverse + name/ID hand-checks + #7 扩面 6+4)")
