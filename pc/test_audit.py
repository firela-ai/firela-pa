#!/usr/bin/env python3
"""audit 测试 — P10 件 1：逐规则 crafted stub + 去重/降级/绊线/环引守卫。

全部 monkeypatch audit.DB 到 tmp（零模块级缓存是 unlink 隔离前提——用例钉死）；
时间经 orchestrator._TODAY 时钟缝（fixed 2026-09-15，eval 同款）。
"""

import json
import subprocess
import sys
import tempfile
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import audit
import memory
import orchestrator as ORIG_ORCH

FAILS = []
_tmp = tempfile.mkdtemp(prefix="test-audit-")
audit.DB = str(Path(_tmp) / "audit.db")
memory.DB = str(Path(_tmp) / "memory.db")
_CFG = {"vlt_base_url": "http://x", "vlt_access_token": "t"}
TODAY = date(2026, 9, 15)
_saved_today = ORIG_ORCH._TODAY
ORIG_ORCH._TODAY = lambda: TODAY


def check(cond, why):
    if not cond:
        FAILS.append(why)


class StubVlt:
    """crafted 账本：months = {绝对月: [(单笔额, 币种, 是否支出), ...]}。"""

    def __init__(self, months):
        self.base, self.token = "http://stub", "t"
        self._months = months

    def transactions(self, df, dt, max_n=500):
        y, m = int(df[:4]), int(df[5:7])
        rows = []
        for amt, cur, is_exp in self._months.get((y, m), []):
            if is_exp:
                rows.append({"date": df, "narration": "x", "postings": [
                    {"account": "Expenses:Food", "units": amt, "currency": cur},
                    {"account": "Assets:Checking", "units": -amt, "currency": cur}]})
            else:
                rows.append({"date": df, "narration": "in", "postings": [
                    {"account": "Assets:Trading", "units": amt, "currency": cur},
                    {"account": "Income:Salary", "units": -amt, "currency": cur}]})
        return {"data": rows, "truncated": False}

    def accounts(self):
        return {"items": [{"path": "Assets:Checking", "type": "Assets"}]}

    def balances(self, account):
        return {"balances": {"CNY": 50000.0}}


def _months_uniform(n_per_month=2, amt=100.0, months_back=12, big=None):
    out = {}
    for k in range(months_back + 1):
        tm = TODAY.year * 12 + (TODAY.month - 1) - k
        out[(tm // 12, tm % 12 + 1)] = [(amt, "CNY", True)] * n_per_month
    if big is not None:
        tm = TODAY.year * 12 + (TODAY.month - 1)
        out[(tm // 12, tm % 12 + 1)] = out[(tm // 12, tm % 12 + 1)] + [(big, "CNY", True)]
    return out


# ================= ① 超支 =================
# 含零中位锚（攻击轮 #1 风险：months-with-data 误读会反转 eval 金标）
# 镜像种子形状：基线月有工资行（非冷启动）但支出全零，仅 2025-12/2026-08 有支出
zero_months = {}
for k in range(1, 12):
    tm = TODAY.year * 12 + (TODAY.month - 1) - k
    zero_months[(tm // 12, tm % 12 + 1)] = [(8000.0, "CNY", False)]      # 工资行（有交易、非支出）
zero_months[(2025, 12)] = [(8000.0, "CNY", False), (800.0, "CNY", True)]
zero_months[(2026, 8)] = [(8000.0, "CNY", False), (495.5, "CNY", True)]
zero_months[(TODAY.year, TODAY.month)] = [(522.5, "CNY", True)]          # 当月
f, n = audit.run_audit(StubVlt(zero_months), TODAY, _CFG)
check(any(x["rule"] == "overspend" and "基线近零" in x["text"] and "522.50" in x["text"] for x in f),
      f"含零中位=0 → 当月 522.50 须以「基线近零」形态触发（得 {[x['rule'] for x in f]}）")

# 常规触发：基线月均 100×2，当月 400（>130×2=260）
f, n = audit.run_audit(StubVlt(_months_uniform(2, 100.0, 12)), TODAY, dict(_CFG))  # 先污染事件？——重建 DB
check(True, "")  # 占位（下一组重建 DB 后再判）

# 重建 DB（隔离）：常规超支 + 不超 + 冷启动
audit.DB = str(Path(tempfile.mkdtemp(prefix="test-audit2-")) / "a.db")
m2 = _months_uniform(2, 100.0, 12)
m2[(TODAY.year, TODAY.month)] = [(400.0, "CNY", True)] * 1
f, n = audit.run_audit(StubVlt(m2), TODAY, _CFG)
check(any(x["rule"] == "overspend" and "400.00" in x["text"] for x in f),
      f"当月 400 vs 中位 200×1.3=260 须触发（得 {[x['text'][:40] for x in f]}）")
audit.DB = str(Path(tempfile.mkdtemp(prefix="test-audit3-")) / "a.db")
m3 = _months_uniform(2, 100.0, 12)                       # 当月=200=中位 → 不触发
f, n = audit.run_audit(StubVlt(m3), TODAY, _CFG)
check(not any(x["rule"] == "overspend" for x in f), "当月=中位不得触发超支")
audit.DB = str(Path(tempfile.mkdtemp(prefix="test-audit4-")) / "a.db")
f, n = audit.run_audit(StubVlt(_months_uniform(2, 100.0, 1)), TODAY, _CFG)
check(any("基线不足" in x for x in n) and not any(x["rule"] == "overspend" for x in f),
      "基线<3 月须冷启动注记且不触发")

# ================= ② 大额 =================
audit.DB = str(Path(tempfile.mkdtemp(prefix="test-audit5-")) / "a.db")
f, n = audit.run_audit(StubVlt(_months_uniform(2, 30.0, 12, big=500.0)), TODAY, _CFG)
check(any(x["rule"] == "large" and "500.00" in x["text"] for x in f),
      f"单笔 500 > 中位 30×10=300 须触发（得 {[x['text'][:40] for x in f]}）")
# 收入/转账非支出：一笔 5000 收入不得触发
audit.DB = str(Path(tempfile.mkdtemp(prefix="test-audit6-")) / "a.db")
m4 = _months_uniform(2, 30.0, 12)
m4[(TODAY.year, TODAY.month)].append((5000.0, "CNY", False))
f, n = audit.run_audit(StubVlt(m4), TODAY, _CFG)
check(not any(x["rule"] == "large" for x in f), "收入/转账单笔不得触发大额（支出过滤）")
# 基线笔数不足
audit.DB = str(Path(tempfile.mkdtemp(prefix="test-audit7-")) / "a.db")
f, n = audit.run_audit(StubVlt(_months_uniform(2, 30.0, 1, big=500.0)), TODAY, _CFG)
check(any("单笔基线不足" in x for x in n), "单笔基线<10 须冷启动注记")

# ================= ③ 里程碑 =================
def _one_audit():
    return audit.run_audit(StubVlt(_months_uniform(2, 100.0, 12)), TODAY, _CFG)

audit.DB = str(Path(tempfile.mkdtemp(prefix="test-audit8-")) / "a.db")
f, n = _one_audit()
check(any("基线快照" in x for x in n) and not any(x["rule"] == "milestone" for x in f),
      "首跑只存快照不判里程碑")
# wr 变更 → 重基线不触发：人为写快照 wr=0.03，档案 wr=0.05
memory.DB = str(Path(tempfile.mkdtemp(prefix="test-mem-a-")) / "m.db")
memory.save("wr", 0.05, "human")
snap = {"as_of": "2026-08-01", "net_assets": 50000.0, "fire_number": 30000 / 0.03,
        "wr": 0.03, "r": 0.04, "sim_years": 5, "truncated_months": [], "formula_version": "sim-v1"}
audit._put_state("sim_snapshot", json.dumps(snap))
f, n = _one_audit()
check(any("基线重置" in x for x in n) and not any(x["rule"] == "milestone" for x in f),
      "wr 快照≠当前须重基线不触发（伪跨越压制）")

# ================= 去重（30 天 + 当月） =================
audit.DB = str(Path(tempfile.mkdtemp(prefix="test-audit9-")) / "a.db")
m5 = _months_uniform(2, 100.0, 12)
m5[(TODAY.year, TODAY.month)] = [(400.0, "CNY", True)]
f1_, _ = audit.run_audit(StubVlt(m5), TODAY, _CFG)
f2_, _ = audit.run_audit(StubVlt(m5), TODAY, _CFG)
check(any(x["rule"] == "overspend" for x in f1_) and not any(x["rule"] == "overspend" for x in f2_),
      "同月第二次运行同规则须静默（30 天/月度去重）")

# ================= 渲染/存档重显/启动降级/绊线 =================
_ = audit.audit_report(StubVlt(m5), TODAY, _CFG)          # last_report 落 state（存档重显的前提）
text = audit.render(f1_, ["注记"], TODAY)
check("教育" in text or "复盘" in text or "确认" in text, "渲染须教育语气")
check("not a licensed financial advisor" in text, "渲染须带 disclaimer 脚注")
rep = audit.stored_report(TODAY)
check(rep is not None and "已提示" in rep, "30 天内存档须可重显（防刷新烧静默）")
audit.startup_audit({"vlt_base_url": "http://127.0.0.1:1", "vlt_access_token": "t"})  # 连接失败→stderr 顺延
check(True, "")
try:
    ORIG_ORCH.send_to_relay("q", 0, _CFG, audit_context="x")
    check(False, "send_to_relay 审计绊线须 assert")
except AssertionError:
    pass

# ================= 环引守卫（双序 import 子进程） =================
for first in ("audit", "orchestrator"):
    rc = subprocess.run([sys.executable, "-c", f"import {first}; import audit, orchestrator"],
                        cwd=str(Path(__file__).resolve().parent), capture_output=True)
    check(rc.returncode == 0, f"import {first} 先行须无环（rc={rc.returncode} {rc.stderr[-120:]!r}）")

ORIG_ORCH._TODAY = _saved_today
if FAILS:
    print(f"FAIL ×{len(FAILS)}")
    for x in FAILS:
        print(" -", x)
    sys.exit(1)
print("test_audit: ①含零中位/常规/冷启动 ②大额/收入过滤/基线 ③快照/伪跨压制 去重/存档/降级/绊线/环引 全部通过")
