#!/usr/bin/env python3
"""后端 200-损坏载荷 fail-safe 测试 — #11/#12 验收（panel-r2 §3-5）。

攻击 = 后端返回 200 但 body 损坏/缺键（必须报错不崩溃；vlt 缺键拒绝假零）；
反例 = 良构空数据（data: [] = 真零，必须正常作答）。跑法：python3 pc/test_orchestrator.py
"""

import json
import sys
import tempfile
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

    # ---- #26：词族扩容 + 变体包含归一 --------------------------------------------
    # 词表 enum 桶合法性：全部 ∈ vlt PayeeProfileCategory 23 值（防拼错 enum 名 dir 分组静默落空）
    _VLT_ENUMS = {"RESTAURANT", "CAFE", "FAST_FOOD", "BAR", "SUPERMARKET", "CONVENIENCE_STORE",
                  "SHOPPING_MALL", "ONLINE_SHOPPING", "TAXI", "RIDE_SHARING", "PUBLIC_TRANSPORT",
                  "PARKING", "GAS_STATION", "UTILITIES", "TELECOM", "STREAMING", "HEALTHCARE",
                  "EDUCATION", "ENTERTAINMENT", "SPORTS", "TRAVEL", "HOTEL", "OTHER"}
    bad = {e for _, (enums, _) in _dc.LOCAL_CAT_WORDS.items() for e in enums if e not in _VLT_ENUMS}
    check(not bad, f"词表 enum 桶须 ∈ vlt PayeeProfileCategory（越界 {bad}）")
    # 新词族段词就位（account 段 = 命中面的现役路径）
    for w, seg in [("订阅", "subscription"), ("购物", "shopping"), ("娱乐", "entertainment"),
                   ("油费", "gas"), ("水电", "utilities"), ("便利店", "convenience"), ("房租", "rent")]:
        _, _a = _m.patterns(w)
        check(seg in _a, f"新词 {w} 的 account 段须含 {seg}（得 {_a!r}）")
    # 空枚举互斥：房租/rent 不得并入外卖白名单别名
    _narr_r, _ = _m.patterns("rent")
    check("meituan" not in _narr_r and "美团" not in _narr_r, f"rent 空枚举不得继承外卖白名单（得 {_narr_r!r}）")
    # 变体包含归一：未知词含已知词/段词 → 沿用该行 spec
    _n, _a = _m.patterns("话费账单")
    check("话费" in _n and "phone" in _a, f"话费账单须归一到话费行（narr={_n!r} acct={_a!r}）")
    _, _a = _m.patterns("水电燃气")
    check("utilities" in _a, f"水电燃气须归一到水电行（得 {_a!r}）")
    _, _a = _m.patterns("rent杂费")
    check("rent" in _a, f"rent杂费须归一到 rent 行（得 {_a!r}）")
    _, _a = _m.patterns("entertainment")           # 英文段包含：entertainment ⊃ Entertainment 段
    check("entertainment" in _a, f"entertainment 须经段词归一到娱乐行（得 {_a!r}）")
    _, _a = _m.patterns("educational")             # 词首命中允许派生后缀
    check("education" in _a, f"educational 须归一到教育行（得 {_a!r}）")
    # ASCII 词首边界：词中 rent 不得误中（current/parent → verbatim 回落）
    for _w in ("current", "parent"):
        _n, _ = _m.patterns(_w)
        check(_n == [_w], f"{_w} 词中含 rent 须 verbatim 不误归一（得 {_n!r}）")
    # 歧义不猜 + 真未知走旧 verbatim 路径（超集保证不回归）
    _n, _ = _m.patterns("水电娱乐")                # 含两个不同 spec 的词 → 不猜
    check(_n == ["水电娱乐"], f"多 spec 歧义词须 verbatim 回落（得 {_n!r}）")
    _n, _a = _m.patterns("宠物")
    check(_n == ["宠物"] and "宠物" in _a, f"真未知词须旧版逐字保留（narr={_n!r}）")
    # 端到端：变体词经归一命中账本段（星巴克行在 Expenses:Food:Dining；订阅须 Subscriptions 段交易）
    _seed.append({"date": "2026-08-06", "narration": "ChatGPT Plus 订阅",
                  "postings": [{"account": "Expenses:Subscriptions:General", "units": 150.0}]})
    try:
        _set_dir([])                              # 离线态：纯词表面（隔离 dir 别名）
        out = orchestrator.branch_tx({"period": "上个月", "category": "外出就餐"}, {})
        check("38.0" in out, f"外出就餐须归一后就餐行命中 Dining 段（得 {out!r}）")
        out = orchestrator.branch_tx({"period": "上个月", "category": "订阅服务"}, {})
        check("150.0" in out, f"订阅服务须归一命中 Subscriptions 段+订阅叙述（得 {out!r}）")
    finally:
        _seed.pop()
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

    # 个人时机题（时态动词+目标词，含插动词变体「能实现」——test_eval_agent sim 首问实测）：进 sim，不进路由
    for q in ("我什么时候能退休", "我什么时候能财务自由", "按现在这样还要多少年能退休",
              "我什么时候能实现财务自由", "多久能达到财务自由"):
        _router_calls.clear()
        out = orchestrator.handle(q, _R(), _CFG23, entry="test")
        check("SIM-NARRATE-MARKER" in out, f"#23 个人时机题须进 sim（{q} → {out!r}）")
        check(not _router_calls, f"#23 个人时机题不得进路由（{q}）")

    # dogfood R1：CLI/REPL 缺省（无 trace 实参）category_hit 观测不得断线
    _cap = {}
    _bt = orchestrator.branch_tx              # 桩前捕获（_saved4 未含 branch_tx——曾误恢复成 branch_l）
    try:
        orchestrator.telemetry.record = lambda *a, **k: _cap.update(k)
        orchestrator.branch_tx = lambda p, cfg, trace=None: trace.update(category_hit="miss") or "0 笔"
        orchestrator.call_router = lambda q: ("tx", {"category": "咖啡"}, 0, True)
        orchestrator.handle("上个月咖啡花了多少", _R(), {}, entry="test")   # 无 trace 实参
        check(_cap.get("category_hit") == "miss",
              f"缺省 trace 路径 category_hit 须落遥测（得 {_cap.get('category_hit')!r}）")
    finally:
        (orchestrator.call_router, orchestrator.branch_tx,
         orchestrator.telemetry.record) = _saved4[0], _bt, _saved4[2]
finally:
    (orchestrator.call_router, orchestrator.branch_l, orchestrator.telemetry.record,
     orchestrator.memory) = _saved4[:4]
    if _saved4[4] is None:
        sys.modules.pop("sim", None)
    else:
        sys.modules["sim"] = _saved4[4]

# ---- #24：REPL exit/quit 须在 handle 前断出（不进路由、不落遥测） ----
import builtins

_handled = []
_saved5 = (builtins.input, orchestrator.handle, orchestrator.load_config,
           orchestrator.CONFIG_PATH, sys.argv, sys.modules.get("audit"))
try:
    orchestrator.handle = lambda q, *a, **k: _handled.append(q) or "MARKER"
    orchestrator.load_config = lambda: {}
    orchestrator.CONFIG_PATH = Path(__file__)          # exists() 过闸即可（load_config 已打桩）
    sys.argv = ["orchestrator.py"]

    _audit = types.ModuleType("audit")
    _audit.startup_audit = lambda cfg: None
    sys.modules["audit"] = _audit

    for cmd in ("exit", "quit"):
        _handled.clear()
        builtins.input = lambda *a, _c=cmd: _c
        orchestrator.main()
        check(not _handled, f"#24 REPL 输入 {cmd!r} 须直接退出、不进 handle")

    _handled.clear()
    _feed = iter(["这个月花了多少", "exit"])
    builtins.input = lambda *a: next(_feed)
    orchestrator.main()
    check(_handled == ["这个月花了多少"],
          f"#24 普通问句后 exit 须正常退出且仅处理一问（{_handled!r}）")
finally:
    (builtins.input, orchestrator.handle, orchestrator.load_config,
     orchestrator.CONFIG_PATH, sys.argv) = _saved5[:5]
    if _saved5[5] is None:
        sys.modules.pop("audit", None)
    else:
        sys.modules["audit"] = _saved5[5]

if FAILS:
    print(f"FAIL ×{len(FAILS)}")
    for f in FAILS:
        print(" -", f)
    sys.exit(1)

# ---- #27：p 槽噪声归一（余额→pf / 转账诚实 / meta 剥离 / topn 守卫扩展）----
for cat, want in (("总支出", None), ("支出结构", None), ("支出排行", None),
                  ("交易记录", None), ("交易查询", None), ("购物明细", "购物"),
                  ("话费扣款记录", "话费扣款"), ("外卖", "外卖"), ("转账", "转账"), ("", "")):
    got = orchestrator.norm_category(cat)
    check(got == want, f"#27 norm_category({cat!r}) 须得 {want!r}（得 {got!r}）")
for q, want in (("看下支出结构", ("topn", {})), ("上季度支出排行", ("topn", {"period": "上个季度"})),
                ("记账软件排行榜前十名都有哪些啊", None), ("我的支出结构有什么可以优化的", None)):
    got = orchestrator._metric_hook(q)
    check(got == want, f"#27 _metric_hook({q!r}) 须得 {want!r}（得 {got!r}）")

_saved5 = (orchestrator.call_router, orchestrator.branch_pf, orchestrator.branch_tx,
           orchestrator.telemetry.record)
try:
    # handle：余额形 reroute → pf（trace/遥测分支随动）
    orchestrator.telemetry.record = lambda *a, **k: None
    orchestrator.call_router = lambda q: ("tx", {"category": "余额"}, 0, True)
    orchestrator.branch_pf = lambda cfg: "PF-MARKER：资产全景"
    _tr = {}
    out = orchestrator.handle("查一下我微信钱包零钱还有多少", _R(), {}, entry="test", trace=_tr)
    check("PF-MARKER" in out and _tr.get("t") == "pf",
          f"#27 余额形须 reroute pf（得 t={_tr.get('t')!r}, out={out!r}）")

    # #28：信用/花呗账单形（负债余额形状）同辖 reroute pf——禁 0 笔假答案
    for _cat, _q in (("信用卡待还", "帮我check下信用卡本月待还amount"),
                     ("花呗账单", "花呗这个月账单多少了")):
        orchestrator.call_router = lambda q, _c=_cat: ("tx", {"category": _c}, 0, True)
        _tr = {}
        out = orchestrator.handle(_q, _R(), {}, entry="test", trace=_tr)
        check("PF-MARKER" in out and _tr.get("t") == "pf",
              f"#28 {_cat} 须 reroute pf（得 t={_tr.get('t')!r}, out={out!r}）")

    # branch_tx：转账 → 诚实不支持（不进过滤、不记 miss）
    _tr2 = {}
    out = orchestrator.branch_tx({"period": "本月", "category": "转账记录"}, {}, trace=_tr2)
    check("转账" in out and "不支持" in out, f"#27 转账须诚实答复（得 {out!r}）")
    check("category_hit" not in _tr2, f"#27 转账不得记 category_hit（得 {_tr2!r}）")
finally:
    (orchestrator.call_router, orchestrator.branch_pf, orchestrator.branch_tx,
     orchestrator.telemetry.record) = _saved5

# ---- #25：版本问句确定性自报（不进路由、零 LLM）；正反例 ----
_orig_vp, _orig_rec = orchestrator._VERSION_PATH, orchestrator.telemetry.record
orchestrator.telemetry.record = lambda *a, **k: None
try:
    with tempfile.TemporaryDirectory() as _td:
        orchestrator._VERSION_PATH = Path(_td) / "VERSION"
        orchestrator._VERSION_PATH.write_text(
            "build_date=2026-09-25\ngit_head=abc1234\napp_sha256=deadbeef00\ninstall_date=2026-09-25\n")
        for q in ("这是我的版本信息，请问还有其他问题吗？", "你是什么版本"):
            out = orchestrator.handle(q, _R(), {}, entry="test")
            check("2026-09-25" in out and "deadbeef" in out, f"#25 版本问句须自报戳（{q} → {out!r}）")
    orchestrator._VERSION_PATH = _orig_vp
    check("开发布局" in orchestrator.version_reply(), "#25 无戳须兜底文案")
finally:
    orchestrator._VERSION_PATH, orchestrator.telemetry.record = _orig_vp, _orig_rec
check(not orchestrator._VERSION_Q_RE.search("苹果最新版本手机值得买吗"),
      "#25 产品版本题不得误拦")

if FAILS:
    print(f"FAIL ×{len(FAILS)}")
    for f in FAILS:
        print(" -", f)
    sys.exit(1)
print("test_orchestrator: 全部通过")
