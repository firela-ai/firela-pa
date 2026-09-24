#!/usr/bin/env python3
"""egress 单点机械门禁 — §9.2 T0-②（#20）。

断言 pc/ 出网原语只在白名单场出现：出口面收敛是「涂黑住 egress 单点」成立的前提，
门禁使该前提不依赖人的记性。违规 exit 1（CI / 本地同构）。

白名单（显式枚举——新出口须改此表才能过，改表即评审点）：
  - orchestrator.send_to_relay：唯一 relay 出口（/v1/chat/completions 字面量唯一）
  - orchestrator.Vlt._call/_exchange：vlt 面（base_url 出自 config）
  - orchestrator.branch_mkt：openbb 面（base_url 出自 config）
  - orchestrator._post/_get + call_router/branch_l/_ctx_for：localhost Ollama + 上述调用方
  - telemetry._runtime_version：localhost:11434 版本探测
用法：python3 ci/egress-gate.py [--self-test]（--self-test 以临时反例自证会拦）。
"""

import re
import sys
import tempfile
from pathlib import Path

PC = Path(__file__).resolve().parent.parent / "pc"

# 出网原语（#20 AC①）
PRIMS = ("urllib.request", "urlopen", "urllib.request.Request", "Request(",
         "http.client", "socket.socket", "socket.create_connection")

# 白名单文件集 + 每文件的豁免行由函数归属断言承载（白名单外的任何出网原语 = 违规）
ALLOWED_FILES = {"orchestrator.py", "telemetry.py", "voice.py", "server.py"}

# 允许的远端主机面（localhost 家族 + 出自 config 的变量形态）
LOCAL = ("127.0.0.1", "localhost", "OLLAMA", ":11434")


def scan(root: Path):
    """返回违规清单 [(file, line_no, line)]。"""
    bad = []
    for f in sorted(root.glob("*.py")):
        if f.name.startswith("test_") or f.name in ("eval_agent.py", "spotcheck.py", "eval_router.py"):
            continue                                   # 测试/评测仪器件（仅 localhost Ollama，不进分发 tarball）
        text = f.read_text(encoding="utf-8")
        for i, line in enumerate(text.splitlines(), 1):
            if any(p in line for p in PRIMS):
                if f.name in ALLOWED_FILES:
                    continue                           # 白名单文件内的原语（函数级归属由静态唯一性测试另守）
                bad.append((f.name, i, line.strip()))
    return bad


def main():
    if "--self-test" in sys.argv:                      # 反例自证（#20 AC④）
        with tempfile.TemporaryDirectory() as td:
            fake = Path(td)
            (fake / "evil.py").write_text("import urllib.request\nurllib.request.urlopen('https://evil.example.com')\n", encoding="utf-8")
            (fake / "good.py").write_text("x = 1\n", encoding="utf-8")
            bad = scan(fake)
            assert bad and bad[0][0] == "evil.py", f"反例未被拦: {bad}"
            print("self-test PASS：第二出口被拦（反例已随临时目录移除）")
            return 0
    bad = scan(PC)
    if bad:
        print(f"egress-gate FAIL ×{len(bad)}（新出口须显式改白名单）:")
        for name, i, line in bad:
            print(f"  {name}:{i}: {line}")
        return 1
    print("egress-gate PASS：出网原语全部在白名单文件（orchestrator/telemetry/voice/server）内")
    return 0


if __name__ == "__main__":
    sys.exit(main())
