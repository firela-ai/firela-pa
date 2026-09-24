#!/usr/bin/env python3
"""ZDC 问题日记 — 架构裁决报告近期项 #1（docs/investigations/advisor-arch-alternatives-panel.md）。

逐问记录到本地 SQLite（0600）：实例层调参的唯一仪表（重复问句率/分支分布/延迟）。
隐私立场：与 server 访问日志静默同源——那防无意识落盘，本库是**有意收集且仅本地**
（永不上报；报告 §4.3 盘上静态暴露为已知开放项）。记录失败绝不影响作答。

用法（报告）：python3 pc/telemetry.py report
"""

import os
import sqlite3
import sys
import time
from pathlib import Path

DB = os.environ.get("FIRELA_TELEMETRY_DB",
                    str(Path.home() / ".firela-pa" / "telemetry.db"))

# renderer = 答案由什么产出（架构报告 ZDC 口径；cache/kb 为后续件预留）
RENDERER = {"l": "gen", "c": "cloud"}                     # 其余分支 → template

_SCHEMA = """CREATE TABLE IF NOT EXISTS qa (
  id INTEGER PRIMARY KEY, ts TEXT NOT NULL, entry TEXT NOT NULL,
  source TEXT NOT NULL DEFAULT 'human', question TEXT NOT NULL,
  branch TEXT NOT NULL, renderer TEXT NOT NULL, total_ms INTEGER,
  decode_tokens INTEGER, answer_chars INTEGER,
  answer TEXT, cloud_tokens INTEGER, runtime_version TEXT)"""

# #8/#9/#15 三列（2026-09-24）：answer=transcript-review 层；cloud_tokens=c 腿 usage；
# runtime_version=ollama /api/version。IF NOT EXISTS 不加列——存量库 PRAGMA 探测后 ALTER。
_EXTRA_COLS = (("answer", "TEXT"), ("cloud_tokens", "INTEGER"), ("runtime_version", "TEXT"),
               ("category_hit", "TEXT"))  # P2 词典命中观测（hit/miss/NULL=无类目槽）


_RT_CACHE = {"v": None, "ts": 0.0}               # ponytail: 1h TTL——每问一探会加延迟


def _runtime_version():
    """#15：ollama /api/version（本地、无 PATH 依赖；失败返 None 不打断）。"""
    import time as _t
    if _RT_CACHE["v"] and _t.monotonic() - _RT_CACHE["ts"] < 3600:
        return _RT_CACHE["v"]
    try:
        import json as _json
        import urllib.request as _ur
        with _ur.urlopen("http://127.0.0.1:11434/api/version", timeout=2) as r:
            v = _json.loads(r.read()).get("version")
            _RT_CACHE.update(v=v, ts=_t.monotonic())
            return v
    except Exception:
        return None


def record(entry, question, branch, total_ms, decode_tokens=None,
           answer_len=0, source="human", answer=None, cloud_tokens=None,
           category_hit=None):
    """单点写入；任何异常只上 stderr（遥测永不打断作答路径）。"""
    try:
        Path(DB).parent.mkdir(parents=True, exist_ok=True)
        con = sqlite3.connect(DB, timeout=5)
        try:
            con.execute(_SCHEMA)
            have = {r[1] for r in con.execute("PRAGMA table_info(qa)")}   # 存量库迁移
            for col, typ in _EXTRA_COLS:
                if col not in have:
                    con.execute(f"ALTER TABLE qa ADD COLUMN {col} {typ}")
            con.execute("INSERT INTO qa(ts,entry,source,question,branch,renderer,"
                        "total_ms,decode_tokens,answer_chars,answer,cloud_tokens,"
                        "runtime_version,category_hit) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (time.strftime("%Y-%m-%dT%H:%M:%S"), entry, source,
                         question, branch, RENDERER.get(branch, "template"),
                         int(total_ms), decode_tokens, answer_len,
                         answer[:2000] if answer else None, cloud_tokens,
                         _runtime_version(), category_hit))
            con.commit()
        finally:
            con.close()
        os.chmod(DB, 0o600)                                # umask 兜底之外的显式门
    except Exception as e:                                 # noqa: BLE001 — 遥测失败静默
        print(f"[telemetry] 记录失败（不影响作答）：{e}", file=sys.stderr)


def report():
    """两周报告的机器腿：分支分布 / 入口拆分 / 重复问句率 / 延迟。"""
    try:
        con = sqlite3.connect(DB)
    except sqlite3.Error as e:
        print(f"[telemetry] 打开失败：{e}", file=sys.stderr)
        return
    try:
        con.row_factory = sqlite3.Row
        n = con.execute("SELECT COUNT(*) c FROM qa").fetchone()["c"]
        if not n:
            print(f"{DB}: 暂无记录")
            return
        print(f"== ZDC 问题日记（{n} 问，{DB}）==")
        for r in con.execute("SELECT branch, COUNT(*) c, ROUND(AVG(total_ms)) avg_ms "
                             "FROM qa GROUP BY branch ORDER BY c DESC"):
            print(f"  {r['branch']:>4} {r['c']:>4} 问   avg {r['avg_ms'] or 0:>7} ms")
        for r in con.execute("SELECT entry, COUNT(*) c FROM qa GROUP BY entry ORDER BY c DESC"):
            print(f"  入口 {r['entry']:<7} {r['c']:>4}")
        dup = con.execute("SELECT COALESCE(SUM(c - 1), 0) d FROM "
                          "(SELECT COUNT(*) c FROM qa GROUP BY LOWER(TRIM(question)) "
                          "HAVING c > 1)").fetchone()["d"]
        print(f"  重复问句率（精确匹配上界）: {dup}/{n} = {dup / n:.0%}")
        for r in con.execute("SELECT LOWER(TRIM(question)) q, COUNT(*) c FROM qa "
                             "GROUP BY q HAVING c > 1 ORDER BY c DESC LIMIT 5"):
            print(f"    ×{r['c']}  {r['q'][:40]}")
    finally:
        con.close()


if __name__ == "__main__":
    report()
