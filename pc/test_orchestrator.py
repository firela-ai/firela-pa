#!/usr/bin/env python3
"""后端 200-损坏载荷 fail-safe 测试 — #11/#12 验收（panel-r2 §3-5）。

攻击 = 后端返回 200 但 body 损坏/缺键（必须报错不崩溃；vlt 缺键拒绝假零）；
反例 = 良构空数据（data: [] = 真零，必须正常作答）。跑法：python3 pc/test_orchestrator.py
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import orchestrator

FAILS = []


def check(cond, why):
    if not cond:
        FAILS.append(why)


# ---- relay 腿：200-非 JSON / 缺 choices → 错误文案，不抛（#11）----
CFG = {"relay_model": "m", "relay_base_url": "http://relay", "relay_api_key": "k",
       "known_names": []}
Q = "资产配置的一般原则是什么"

orig_post = orchestrator._post


def _post_raises(*a, **k):
    raise json.JSONDecodeError("Expecting value", "<html>502</html>", 0)


def _post_no_choices(*a, **k):
    return {"id": "x"}


def _post_ok(*a, **k):
    return {"choices": [{"message": {"content": "分散化与费用控制是两大支柱。"}}]}


for stub, why in ((_post_raises, "relay 200-非 JSON"),
                  (_post_no_choices, "relay 200-缺 choices")):
    orchestrator._post = stub
    out, _u = orchestrator.send_to_relay(Q, 0, CFG)     # #9 元组契约
    check(isinstance(out, str) and "relay 应答损坏" in out, f"{why}：须报错文案不抛（得 {out!r}）")
orchestrator._post = _post_ok
out, usage = orchestrator.send_to_relay(Q, 0, CFG)      # #9 元组契约：text + usage
check("分散化" in out, "relay 良构应答须正常返回")
check(usage is None or isinstance(usage, int), f"usage 须 int|None（得 {usage!r}）")

# ---- vlt 腿：缺键拒绝假零、真零正常（#12）----
VC = {"vlt_base_url": "http://vlt", "vlt_access_token": "t"}
v = orchestrator.Vlt(VC)


def _call(path, **kw):
    return {}                      # 缺 data / items 键


orig_call = orchestrator.Vlt._call
orchestrator.Vlt._call = lambda self, path, **kw: _call(path, **kw)
try:
    v.transactions("2026-01-01", "2026-01-31")
    FAILS.append("vlt 缺 data 键：须抛 VltBadResponse（拒绝假零）")
except orchestrator.VltBadResponse:
    pass
try:
    v.accounts()
    FAILS.append("vlt accounts 缺 items 键：须抛 VltBadResponse")
except orchestrator.VltBadResponse:
    pass

orchestrator.Vlt._call = lambda self, path, **kw: {"data": []}   # 真零（良构空数据）
r = v.transactions("2026-01-01", "2026-01-31")
check(r == {"data": [], "truncated": False}, f"vlt data:[] 真零须正常返回（得 {r!r}）")

# ---- handle()：VltBadResponse / ValueError 转错误文案，不炸整轮（#11/#12）----
class _R:
    def resolve(self, q):
        return q

    def update(self, *a):
        pass


orig_router, orig_tx, orig_rec = (orchestrator.call_router, orchestrator.branch_tx,
                                  orchestrator.telemetry.record)
orchestrator.call_router = lambda q: ("tx", {}, 0, True)
orchestrator.telemetry.record = lambda *a, **k: None


def _tx_bad(p, cfg):
    raise orchestrator.VltBadResponse("列表应答缺 data 键（结构损坏，拒绝报 0 笔）")


def _tx_jsonfail(p, cfg):
    raise json.JSONDecodeError("Expecting value", "", 0)


for stub, needle, why in ((_tx_bad, "账本应答异常", "handle×VltBadResponse"),
                          (_tx_jsonfail, "应答损坏", "handle×JSONDecodeError")):
    orchestrator.branch_tx = stub
    out = orchestrator.handle("这个月花了多少", _R(), {}, entry="test")
    check(needle in out, f"{why}：须转错误文案（得 {out!r}）")
orchestrator.call_router, orchestrator.branch_tx, orchestrator.telemetry.record = (
    orig_router, orig_tx, orig_rec)
orchestrator._post = orig_post
orchestrator.Vlt._call = orig_call

# ---- P6a 观测缝：trace 填充 / pre-gate raw_out / source 透传 / _TODAY 时钟缝 ----
import datetime

captured = {}
_saved = (orchestrator.call_router, orchestrator.branch_tx, orchestrator.branch_l,
          orchestrator.telemetry.record, orchestrator.normalize_period, orchestrator._TODAY)
try:
    orchestrator.telemetry.record = lambda *a, **k: captured.update(k)
    orchestrator.branch_tx = lambda p, cfg: "2026-08-01~2026-08-31 · 3 笔支出，合计 90.50"

    # tx 腿：trace 填充 + 模板腿无 raw_out + source 透传
    orchestrator.call_router = lambda q: ("tx", {"period": "2026-08"}, 0, True)
    tr = {}
    orchestrator.handle("2026 年 8 月外卖花了多少", _R(), {}, entry="test", trace=tr, source="synthetic")
    check(tr.get("t") == "tx" and tr.get("parsed") is True and "外卖" in tr.get("resolved", ""),
          f"trace 须含 resolved/t/parsed（得 {tr!r}）")
    check("raw_out" not in tr, "模板腿（tx）不应有 pre-gate raw_out（verify 不包模板腿）")
    check(captured.get("source") == "synthetic",
          f"source 须经 handle 透传到 telemetry（得 {captured.get('source')!r}）")

    # l 腿：raw_out 在场（pre-gate 捕获）+ 缺省 source 回落 human
    orchestrator.call_router = lambda q: ("l", {}, 0, True)
    orchestrator.branch_l = lambda q, cfg, stats=None: "复利是收益再投资产生的滚动增长。"
    captured.clear()
    tr2 = {}
    orchestrator.handle("什么是复利", _R(), {}, entry="test", trace=tr2)
    check(tr2.get("raw_out") == "复利是收益再投资产生的滚动增长。",
          f"l 腿 trace 须含 pre-gate raw_out（得 {tr2.get('raw_out')!r}）")
    check(captured.get("source", "human") == "human", "缺省 source 须回落 human")

    # _TODAY 时钟缝：改时钟 → 真 branch_tx 的周期计算随动（桩 Vlt 供数据面）
    seen = {}
    orchestrator.branch_tx = _saved[1]                     # 恢复真 branch_tx（桩不调 normalize_period）
    orig_vlt_call = orchestrator.Vlt._call
    orchestrator.Vlt._call = lambda self, path, **kw: {"data": []}
    orchestrator.normalize_period = (
        lambda per, today: seen.update(per=per, today=today) or ("2026-08-01", "2026-08-31"))
    orchestrator.call_router = lambda q: ("tx", {}, 0, True)
    orchestrator._TODAY = lambda: datetime.date(2026, 9, 15)
    try:
        orchestrator.handle("这个月花了多少", _R(),
                            {"vlt_base_url": "http://vlt", "vlt_access_token": "t"}, entry="test")
    finally:
        orchestrator.Vlt._call = orig_vlt_call
    check(seen.get("today") == datetime.date(2026, 9, 15),
          f"_TODAY 缝须注入 branch_tx 周期计算（得 {seen.get('today')!r}）")
finally:
    (orchestrator.call_router, orchestrator.branch_tx, orchestrator.branch_l,
     orchestrator.telemetry.record, orchestrator.normalize_period, orchestrator._TODAY) = _saved

if FAILS:
    print(f"FAIL ×{len(FAILS)}")
    for f in FAILS:
        print(" -", f)
    sys.exit(1)

# ---- p 槽噪声归一（agent eval 真基线 2026-09-23 发现：聚合词/口语类别/5d 变体） ----
_saved2 = (orchestrator.Vlt, orchestrator._TODAY)
try:
    orchestrator.Vlt = type("V", (), {"__init__": lambda self, cfg: None,
                                      "transactions": lambda self, a, b: {"data": [
                                          {"date": "2026-08-01", "narration": "美团外卖-午餐",
                                           "postings": [{"account": "Expenses:Delivery", "units": 90.5}]}],
                                          "truncated": False}})
    out = orchestrator.branch_tx({"period": "上个月", "category": "总支出"}, {})
    check("90.5" in out and "1 笔" in out, f"聚合词类别须归一为全类（得 {out!r}）")
    out = orchestrator.branch_tx({"period": "上个月", "category": "交通费"}, {})
    check("0 笔" in out, "交通费同义词须过滤生效（种子里无交通）")
    import re as _re
    check(_re.fullmatch(r"\d+d", "5d") and not _re.fullmatch(r"\d+d", "qq"),
          "5d 变体正则须只匹配数字+d")
finally:
    orchestrator.Vlt, orchestrator._TODAY = _saved2

if FAILS:
    print(f"FAIL ×{len(FAILS)}")
    for f in FAILS:
        print(" -", f)
    sys.exit(1)
print("test_orchestrator: 全部通过")
