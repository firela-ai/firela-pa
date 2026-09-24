#!/usr/bin/env python3
"""ZDC 遥测单测 — 跑法：python3 pc/test_telemetry.py（写临时 DB，不碰 ~/.firela-pa）。"""

import os
import sqlite3
import stat
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import telemetry

tmp = tempfile.mkdtemp()
telemetry.DB = str(Path(tmp) / "t.db")                    # env 在 import 后不再生效，显式覆写

telemetry.record("repl", "这个月花了多少", "tx", 1200, answer_len=30)
telemetry.record("web", "什么是复利", "l", 900, decode_tokens=48, answer_len=40)
telemetry.record("openai", "我该怎么理财", "c", 3000, answer_len=100)
telemetry.record("web", "这个月花了多少", "tx", 1100, answer_len=28)   # 重复问句样本

con = sqlite3.connect(telemetry.DB)
rows = con.execute("SELECT entry,branch,renderer,decode_tokens FROM qa ORDER BY id").fetchall()
assert [r[0] for r in rows] == ["repl", "web", "openai", "web"], rows
assert [r[1] for r in rows] == ["tx", "l", "c", "tx"], rows
assert [r[2] for r in rows] == ["template", "gen", "cloud", "template"], rows  # renderer 映射
assert rows[1][3] == 48 and rows[0][3] is None, rows                          # decode_tokens 只进 l
mode = stat.S_IMODE(os.stat(telemetry.DB).st_mode)
assert mode == 0o600, oct(mode)                                               # 0600 门
con.close()

# 遥测失败绝不影响作答路径：DB 指向目录 → 连接必败 → 只上 stderr 不抛
telemetry.DB = str(Path(tmp))
telemetry.record("repl", "q", "l", 1)

print("OK: 4 行写入 + renderer 映射 + decode_tokens 归属 + 0600 + 失败静默 全过")
