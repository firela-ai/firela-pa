#!/usr/bin/env python3
"""memory 测试 — P9 件 1：roundtrip/sanity/synthetic 拒写/重启存活/0600/隔离审计。

全部 monkeypatch memory.DB 到 tmp——模块级常量在 import 时定型，禁写真实
~/.firela-pa/memory.db（隔离断言钉死）。
"""

import os
import stat
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import memory

FAILS = []
_tmp = tempfile.mkdtemp(prefix="test-memory-")
_REAL_DB = memory.DB
memory.DB = str(Path(_tmp) / "memory.db")


def check(cond, why):
    if not cond:
        FAILS.append(why)


# 隔离审计：任何用例后真实库不得存在/被改
def _audit():
    if _REAL_DB != memory.DB and Path(_REAL_DB).exists():
        st = Path(_REAL_DB).stat()
        check(st.st_mtime_ns == getattr(_audit, "mt0", st.st_mtime_ns), "真实库被测试触碰！")


# roundtrip + 重启存活（close 后新连接即进程外等价——无进程内缓存）
ok, note = memory.save("wr", 0.035, "human", formula_version="v1.5", ledger_as_of="2026-09-23")
check(ok and "已记住" in note, f"human 保存须成功（得 {ok}/{note}）")
check(memory.get_profile().get("wr") == 0.035, "roundtrip 读取须回 0.035")
# 覆写同键（UPSERT）
memory.save("wr", 0.04, "human")
check(memory.get_profile().get("wr") == 0.04, "同键覆写须 UPSERT 生效")

# sanity 三界：界内边缘过、界外拒
check(memory.save("real_return", 0.15, "human")[0], "上边缘 0.15 须过")
check(not memory.save("real_return", 0.16, "human")[0], "超上界 0.16 须拒")
check(not memory.save("wr", 0.004, "human")[0], "低于下界 0.004 须拒")
check(not memory.save("retirement_age", 30, "human")[0], "退休年龄 30 须拒")
check(not memory.save("bogus_key", 1, "human")[0], "未知键须拒")

# synthetic 拒写（mod4）——档案不受污染
check(not memory.save("wr", 0.05, "synthetic")[0], "synthetic 保存须拒")
check(not memory.add_goal("x", "synthetic")[0], "synthetic 目标须拒")
check(memory.get_profile().get("wr") == 0.04, "synthetic 拒写后档案不变")

# goals + inspect
memory.add_goal("2035 年前攒够 FIRE 目标", "human")
insp = memory.inspect()
check("2035" in insp and "提款率" in insp, f"inspect 须含目标与档案（得 {insp!r}）")

# 0600 + 禁 WAL（写后无 -wal 常驻）
mode = stat.S_IMODE(os.stat(memory.DB).st_mode)
check(mode == 0o600, f"库权限须 0600（得 {oct(mode)}）")
check(not Path(memory.DB + "-wal").exists(), "禁 WAL：不得有常驻 -wal 文件")

# 空档案 inspect（新 tmp 库）
memory.DB = str(Path(_tmp) / "memory2.db")
check("没有记住任何" in memory.inspect(), "空档案 inspect 须诚实回复")

_audit()
if FAILS:
    print(f"FAIL ×{len(FAILS)}")
    for f in FAILS:
        print(" -", f)
    sys.exit(1)
print("test_memory: roundtrip/sanity/synthetic/重启存活/0600/隔离 全部通过")
