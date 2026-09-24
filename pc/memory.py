#!/usr/bin/env python3
"""代理记忆 — P9 最小实现（D5 裁定，格式真相源 docs/advisor-memory-format.md v1）。

本地 SQLite（与 telemetry.db 分库：诊断 vs 顾问状态不同生命周期/隐私等级——清诊断
不丢档案）；0600 每次写后显式 chmod（telemetry 先例）；默认 rollback journal 禁 WAL
（-wal 文件逃逸 0600）；写路径仅显式「记住」指令（orchestrator 保存门）且 source
必须为 human（synthetic/persona 禁写——mod4）；任何失败只打 stderr，永不打断作答。

用法：FIRELA_MEMORY_DB 须在 import 前落位（eval boot 先例）；单元测试 monkeypatch
memory.DB 到 tmp——模块级常量在 import 时定型，env 后置无效。
"""

import os
import sqlite3
import sys
import time
from pathlib import Path

DB = os.environ.get("FIRELA_MEMORY_DB", str(Path.home() / ".firela-pa" / "memory.db"))

# sanity 窗（格式 spec v1 唯一定义处——边界值本身进 spec，勿在他处复制）
WINDOWS = {"wr": (0.005, 0.10), "real_return": (0.0, 0.15),
           "inflation": (0.0, 0.10), "retirement_age": (45, 80)}
LABELS = {"wr": "提款率（实际口径）", "real_return": "预期实际年化",
          "inflation": "预期通胀（存而不用）", "retirement_age": "目标退休年龄"}


def _con():
    Path(DB).parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB)
    con.execute("CREATE TABLE IF NOT EXISTS profile ("
               "key TEXT PRIMARY KEY, value REAL, label TEXT, ts TEXT, provenance TEXT, "
               "formula_version TEXT, ledger_as_of TEXT)")
    con.execute("CREATE TABLE IF NOT EXISTS goals (id INTEGER PRIMARY KEY AUTOINCREMENT, "
               "text TEXT, ts TEXT, provenance TEXT)")
    return con


def get_profile():
    """档案 → dict（key→value）；读失败返回空 dict（作答路径永不因记忆失败）。
    读路径不建库（无库=无档案——防止只读查询在磁盘创建真实库文件）。"""
    if not Path(DB).exists():
        return {}
    try:
        con = _con()
        rows = con.execute("SELECT key, value FROM profile").fetchall()
        con.close()
        return dict(rows)
    except Exception as e:
        print(f"[记忆] 读失败（{e}）——按无档案处理", file=sys.stderr)
        return {}


def _disp(key, v):
    """人读单位显示：分数键显示百分比、年龄键显示岁（sanity 反问文案用）。"""
    return f"{v:g} 岁" if key == "retirement_age" else f"{v * 100:g}%"


def save(key, value, source, formula_version="", ledger_as_of=""):
    """显式保存门的后端：sanity 窗 + synthetic 拒写。返回 (ok, 说明)。

    sanity 先于 source 门（越界值无论 source 一律收到 sanity 反问——不落库相同，
    文案区分让 eval 两路径可分别钉死；synthetic 的合法值仍被拒写）。"""
    if key not in WINDOWS:
        return False, f"未知假设键 {key}"
    try:
        lo, hi = WINDOWS[key]
        v = float(value)
    except (TypeError, ValueError):
        return False, "假设值不可解析"
    if not lo <= v <= hi:
        return False, f"{LABELS[key]} 需在 {_disp(key, lo)}–{_disp(key, hi)} 之间（收到 {_disp(key, v)}）"
    if source != "human":
        return False, "synthetic/persona 会话禁写代理记忆"
    try:
        con = _con()
        con.execute("INSERT INTO profile (key, value, label, ts, provenance, formula_version, ledger_as_of) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value, ts=excluded.ts, "
                    "provenance=excluded.provenance, formula_version=excluded.formula_version, "
                    "ledger_as_of=excluded.ledger_as_of",
                    (key, v, LABELS[key], time.strftime("%Y-%m-%dT%H:%M:%S"), source,
                     formula_version, ledger_as_of))
        con.commit()
        con.close()
        os.chmod(DB, 0o600)
        return True, f"{LABELS[key]} = {v:g} 已记住"
    except Exception as e:
        print(f"[记忆] 写失败（{e}）", file=sys.stderr)
        return False, "记忆库暂不可写"


def add_goal(text, source):
    if source != "human":
        return False, "synthetic/persona 会话禁写代理记忆"
    try:
        con = _con()
        con.execute("INSERT INTO goals (text, ts, provenance) VALUES (?, ?, ?)",
                    (text, time.strftime("%Y-%m-%dT%H:%M:%S"), source))
        con.commit()
        con.close()
        os.chmod(DB, 0o600)
        return True, "目标已记住"
    except Exception as e:
        print(f"[记忆] 目标写失败（{e}）", file=sys.stderr)
        return False, "记忆库暂不可写"


def inspect():
    """「我记住了什么」诚实回复素材（profile + goals 最新 3 条）。"""
    if not Path(DB).exists():
        return "目前没有记住任何假设或目标。说「记住提款率 3.5%」这类明确指令我才会记。"
    try:
        con = _con()
        prof = con.execute("SELECT key, label, value FROM profile ORDER BY key").fetchall()
        goals = [r[0] for r in con.execute("SELECT text FROM goals ORDER BY id DESC LIMIT 3")]
        con.close()
    except Exception as e:
        print(f"[记忆] 读失败（{e}）", file=sys.stderr)
        return "记忆库暂不可读。"
    if not prof and not goals:
        return "目前没有记住任何假设或目标。说「记住提款率 3.5%」这类明确指令我才会记。"
    lines = [f"  {label}：{_disp(key, v)}" for key, label, v in prof]
    lines += [f"  目标：{g}" for g in reversed(goals)]
    return "已记住：\n" + "\n".join(lines)
