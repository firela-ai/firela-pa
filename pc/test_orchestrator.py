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


def _tx_bad(p, cfg, trace=None):
    raise orchestrator.VltBadResponse("列表应答缺 data 键（结构损坏，拒绝报 0 笔）")


def _tx_jsonfail(p, cfg, trace=None):
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
    orchestrator.branch_tx = lambda p, cfg, trace=None: "2026-08-01~2026-08-31 · 3 笔支出，合计 90.50"

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


# ---- P2 件B：dir 三层词典（命中/回落/离线三态 + 双 canary + trim 用例 + miss 遥测） ----
_saved3 = (orchestrator.Vlt, orchestrator._TODAY)
import dir_cache as _dc
try:
    _seed = [
        {"date": "2026-08-01", "narration": "地铁充值",
         "postings": [{"account": "Expenses:Transportation:PublicTransit", "units": 45.0}]},
        {"date": "2026-08-02", "narration": "超市买菜-周采购",
         "postings": [{"account": "Expenses:Food:Groceries", "units": 188.0}]},
        {"date": "2026-08-03", "narration": "星巴克-拿铁",
         "postings": [{"account": "Expenses:Food:Dining", "units": 38.0}]},
        {"date": "2026-08-04", "narration": "肯德基-宅急送",
         "postings": [{"account": "Expenses:Delivery", "units": 59.0}]},
    ]
    orchestrator.Vlt = type("V", (), {"__init__": lambda self, cfg: None,
                                      "transactions": lambda self, a, b: {"data": _seed, "truncated": False}})

    def _set_dir(profiles):
        _dc._state["profiles"], _dc._state["ts"] = profiles, __import__("time").time()
        _dc._matcher = None                      # 单例重建（注入面）

    # 离线/空基线：canary tx-t8（地铁充值→PublicTransit，层3 现役路径）与聚合不受 dir 影响
    _set_dir([])
    out = orchestrator.branch_tx({"period": "上个月", "category": "交通费"}, {})
    check("45.0" in out and "1 笔" in out, f"canary tx-t8 离线态须命中层3（得 {out!r}）")

    # dir 命中态：星巴克别名（层2）+ 快递污染排除（外卖白名单）
    _set_dir([
        {"canonical": "starbucks", "aliases": ["星巴克", " sbux "], "category": "CAFE", "subcategory": None},
        {"canonical": "kfc", "aliases": ["肯德基"], "category": "FAST_FOOD", "subcategory": None},
        {"canonical": "meituan-waimai", "aliases": ["美团外卖"], "category": "OTHER", "subcategory": "delivery"},
        {"canonical": "sf-express", "aliases": ["顺丰"], "category": "OTHER", "subcategory": "delivery"},
    ])
    _m = _dc.matcher({})
    narr, acct = _m.patterns("咖啡")
    check("星巴克" in narr and " sbux" not in narr and "sbux" in narr,
          f"dir 别名须并入且 strip+casefold（得 {narr!r}）")
    out = orchestrator.branch_tx({"period": "上个月", "category": "咖啡"}, {})
    check("38.0" in out, f"咖啡词须经 dir 别名命中星巴克叙述（得 {out!r}）")
    out = orchestrator.branch_tx({"period": "上个月", "category": "餐饮"}, {})
    check("38.0" in out and "59.0" in out, f"餐饮四桶并集须含 CAFE+FAST_FOOD（得 {out!r}）")
    out = orchestrator.branch_tx({"period": "上个月", "category": "外卖"}, {})
    check("59.0" in out, f"外卖须经白名单别名命中（肯德基宅急送在 Delivery 段——层3；美团外卖叙述）（得 {out!r}）")
    # 快递污染排除：顺丰别名不得入外卖 narr 模式
    _narr, _ = _m.patterns("外卖")
    check("顺丰" not in _narr and "sf-express" not in _narr, f"外卖模式须排除快递公司（得 {_narr!r}）")

    # trim 用例（ADR-0060 #1309 客户端义务）：带空白 alias 仍命中
    _set_dir([{"canonical": " luckin ", "aliases": [" 瑞幸咖啡 "], "category": "CAFE", "subcategory": None}])
    _seed.append({"date": "2026-08-05", "narration": "瑞幸咖啡-生椰拿铁",
                  "postings": [{"account": "Expenses:Food:Dining", "units": 18.0}]})
    out = orchestrator.branch_tx({"period": "上个月", "category": "咖啡"}, {})
    check("18.0" in out, f"带空白 alias 须 strip 后仍命中（得 {out!r}）")
    _seed.pop()

    # miss 遥测：类目过滤零结果 → trace.category_hit=miss
    _tr = {}
    orchestrator.branch_tx({"period": "上个月", "category": "话费"}, {}, trace=_tr)
    check(_tr.get("category_hit") == "miss", f"零结果须记 miss（得 {_tr!r}）")
    _set_dir([])
    _tr2 = {}
    orchestrator.branch_tx({"period": "上个月", "category": "交通费"}, {}, trace=_tr2)
    check(_tr2.get("category_hit") == "hit", f"命中须记 hit（得 {_tr2!r}）")
finally:
    orchestrator.Vlt, orchestrator._TODAY = _saved3
    _dc._state["profiles"], _dc._state["ts"] = [], 0.0
    _dc._matcher = None

if FAILS:
    print(f"FAIL ×{len(FAILS)}")
    for f in FAILS:
        print(" -", f)
    sys.exit(1)

# ---- #23：sim 钩子收窄——概念题走路由、个人时机题进 sim ----
import types

_router_calls = []
_saved4 = (orchestrator.call_router, orchestrator.branch_l, orchestrator.telemetry.record,
           orchestrator.memory, sys.modules.get("sim"))
try:
    orchestrator.call_router = lambda q: (_router_calls.append(q), ("l", {}, 0, True))[1]
    orchestrator.branch_l = lambda q, cfg, stats=None: "L-MARKER：Lean FIRE 与 Fat FIRE 的区别是目标支出水平。"
    orchestrator.telemetry.record = lambda *a, **k: None
    orchestrator.memory = types.ModuleType("memory")
    orchestrator.memory.get_profile = lambda: {}

    _fake = types.ModuleType("sim")
    _fake.STATE = None
    _fake.SIM_FORMULA_VERSION = "test-v0"
    _fake.ledger_params = lambda vlt, today: {}
    _fake.narrate = lambda ep, wr=None, r=None: "SIM-NARRATE-MARKER"

    def _reset(p):
        _fake.STATE = types.SimpleNamespace(
            ttl=99, wr=0.04, r=0.03,
            effective_params=lambda: {"years": 25}, mutate=lambda q: ("", False))
    _fake.reset = _reset
    sys.modules["sim"] = _fake

    _CFG23 = {"vlt_base_url": "http://vlt", "vlt_access_token": "t"}   # Vlt(cfg) 构造在 ledger_params 实参先求值

    # 概念/讨论题（gold=l 形态）：不进 sim，走受训路由器
    for q in ("财务自由是什么意思啊", "FIRE中的Lean FIRE和Fat FIRE区别是啥？",
              "财务自由会让你失去工作动力吗"):
        _router_calls.clear()
        out = orchestrator.handle(q, _R(), _CFG23, entry="test")
        check("L-MARKER" in out, f"#23 概念题须走路由不进 sim（{q} → {out!r}）")
        check(len(_router_calls) == 1, f"#23 概念题须恰好进路由一次（{q}）")

    # 个人时机题（时态动词+目标词）：进 sim，不进路由
    for q in ("我什么时候能退休", "我什么时候能财务自由", "按现在这样还要多少年能退休"):
        _router_calls.clear()
        out = orchestrator.handle(q, _R(), _CFG23, entry="test")
        check("SIM-NARRATE-MARKER" in out, f"#23 个人时机题须进 sim（{q} → {out!r}）")
        check(not _router_calls, f"#23 个人时机题不得进路由（{q}）")
finally:
    (orchestrator.call_router, orchestrator.branch_l, orchestrator.telemetry.record,
     orchestrator.memory) = _saved4[:4]
    if _saved4[4] is None:
        sys.modules.pop("sim", None)
    else:
        sys.modules["sim"] = _saved4[4]

if FAILS:
    print(f"FAIL ×{len(FAILS)}")
    for f in FAILS:
        print(" -", f)
    sys.exit(1)
print("test_orchestrator: 全部通过")
