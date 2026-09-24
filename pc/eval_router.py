#!/usr/bin/env python3
"""eval200 gate runner for the GGUF router on Ollama (PC form P1).

Stdlib only (deployment-barrier constraint). Reads finetune/data/eval.jsonl,
sends each prompt as a stateless single-turn /api/generate, scores:

  G1  per-branch t-accuracy over meta.intent (tx/pf/mkt/l/c), every branch >= 90%
  G2  gold=tx routed to c, count must be 0
  G3  parseable fmt-v1.1 JSON rate, 100% (unconstrained pass is the gate)
  G4  overall t-accuracy >= 90%
  x   flag accuracy vs meta.x — report only (narrow identity-only gold, SPEC §56)

Two-tier verdict (docs/pc-package-plan.md): Tier-1 PASS = G1-G4; Tier-2 PARITY
investigate trigger = G4 <= 93 or any branch <= 88 (vs .rkllm actuals 96 /
tx98 pf95 mkt97 l92 c95). Never sends `think` (custom-template models reject it).

Call contract (empirically settled 2026-09-19): RAW mode — Ollama 0.34 declares
this model "thinking"-capable and strips the empty-think block out of the
Modelfile TEMPLATE, breaking the training distribution. So we bypass templates
entirely: raw=true with the exact training-format string (user markers + empty
think block on the prompt side, model answers bare JSON).

Usage:
  python3 pc/eval_router.py --model firela-router                       # gate pass
  python3 pc/eval_router.py --model firela-router --format json         # contract pass
  python3 pc/eval_router.py --model firela-router --limit 30 --out r.json
"""
import argparse
import collections
import json
import re
import sys
import time
import urllib.request
from datetime import date

BRANCHES = ("tx", "pf", "mkt", "l", "c")


def wrap_raw(prompt):
    """Exact training format: user turn + assistant opener + empty think block."""
    return f"<|im_start|>user\n{prompt}<|im_end|>\n<|im_start|>assistant\n<think>\n\n</think>\n\n"


def post(url, payload, retries=2):
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(
                url, data=json.dumps(payload).encode(),
                headers={"Content-Type": "application/json"}, method="POST")
            with urllib.request.urlopen(req, timeout=180) as r:
                return json.loads(r.read())
        except Exception as e:
            if attempt == retries:
                raise
            print(f"  retry after {e}", file=sys.stderr)
            time.sleep(2)


def extract_pred(text):
    """Return (t, x, p, parsed, raw). Strips think remnants/fences, slices first { to last }.
    p 归一：null/缺省/非 dict → {}（金标 110/200 p=null，模型实证输出无 p 的 JSON）。"""
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.S)
    text = re.sub(r"```(?:json)?", "", text)
    lo, hi = text.find("{"), text.rfind("}")
    if lo < 0 or hi <= lo:
        return None, None, {}, False, text[:120]
    try:
        obj = json.loads(text[lo:hi + 1])
        t = obj.get("t")
        if t not in BRANCHES:
            return None, None, {}, False, text[:120]
        p = obj.get("p")
        if not isinstance(p, dict):
            p = {}
        return t, obj.get("x", 0), p, True, None
    except json.JSONDecodeError:
        return None, None, {}, False, text[lo:hi + 1][:120]


# ---------- p 参数判分（Nimble 面板 P1，2026-09-22；report-only，不进 Tier-1） ----------
# 金标双格式实证（2026-09-22 核验扫描）：period 口语 51 vs ISO 4、category 中英同概念双写
# （储蓄率/savings_rate、星巴克/Starbucks）、s 多元素列表非码点序（2/29）——比对前双侧归一。
# m 剔除出逐键比对（gold 28/28 全自由文本，q/hist teacher 从不打），单独报 m_exact。

_PERIOD_OFFSETS = {"本月": 0, "这个月": 0, "当前": 0, "上月": -1, "上个月": -1}  # 未知口语值字面保序（不并桶）
_CATEGORY_EQ = {"储蓄率": "savings_rate", "星巴克": "starbucks", "星巴克消费": "starbucks",
                "娱乐预算": "entertainment", "账单-外出就餐": "外出就餐"}
_SYMBOL_EQ = {"以太币": "ETH", "比特币": "BTC", "瑞波币": "XRP", "新台币": "TWD",
              "道琼斯工业指数": "道琼斯"}   # s 元素同物异名（2026-09-22 p_fails 实证对）
_METRIC_EQ = {"top_expenses": "最大三笔支出"}


def _norm_period(v, today):
    """已知月度词 → (y, m) 规范桶；ISO YYYY-MM 直接 (y, m)；未知值字面返回（宁漏判不误并桶）。
    注意与 orchestrator.normalize_period 的分工：那是执行器解析（未识别默认当月），这是评分等价
    （无 fallback——近三个月/最近一周等必须字面保序，防止被静默并桶）。"""
    v = str(v or "").strip()
    if v in _PERIOD_OFFSETS:
        off = _PERIOD_OFFSETS[v]
        y, m = today.year, today.month + off
        y, m = (y - 1, 12) if m == 0 else (y, m)
        return ("ym", y, m)
    if re.fullmatch(r"\d{4}-\d{2}", v):
        return ("ym", int(v[:4]), int(v[5:7]))
    return ("lit", v)


def _canon_cat(v):
    v = str(v or "").strip()
    return _CATEGORY_EQ.get(v, v.lower())


def _p_key_match(key, g, o):
    if g is None:
        return True
    if key == "s" and isinstance(g, list):
        if not isinstance(o, list):
            return False
        canon = lambda xs: sorted(_SYMBOL_EQ.get(str(x), str(x)).upper() for x in xs)
        return canon(g) == canon(o)
    if key == "period":
        today = date.today()                      # 单次取值防跨午夜两侧分桶不一致
        return _norm_period(g, today) == _norm_period(o, today)
    if key == "category":
        return _canon_cat(g) == _canon_cat(o)
    if key == "metric":
        canon_v = lambda v: _METRIC_EQ.get(str(v), str(v))
        return canon_v(g) == canon_v(o)
    return g == o


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://127.0.0.1:11434")
    ap.add_argument("--model", default="firela-router")
    ap.add_argument("--data", default="finetune/data/eval.jsonl")
    ap.add_argument("--format", choices=["none", "json"], default="none",
                    help="none = unconstrained (gate pass, SPEC §3); json = production contract pass")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--out", default="")
    args = ap.parse_args()

    rows = [json.loads(x) for x in open(args.data)]
    if args.limit:
        rows = rows[:args.limit]

    version = "?"
    try:
        with urllib.request.urlopen(f"{args.url}/api/version", timeout=10) as r:
            version = json.loads(r.read()).get("version", "?")
    except Exception:
        pass
    print(f"model={args.model} ollama={version} n={len(rows)} format={args.format}\n")

    per_branch = collections.defaultdict(lambda: [0, 0])  # intent -> [correct, total]
    conf = collections.defaultdict(int)                    # (gold, pred) -> n
    mismatches, parse_fails = [], []
    g2_leaks, g4_correct = 0, 0
    x_ok, x_n = 0, 0
    p_ok, p_n, p_fails = 0, 0, []                          # p 参数判分（m 剔除；仅 t 对且 gold 有 p 的行）
    m_ok, m_n = 0, 0

    t0 = time.time()
    for i, row in enumerate(rows):
        payload = {
            "model": args.model, "prompt": wrap_raw(row["prompt"]), "raw": True,
            "stream": False, "keep_alive": "10m",
            "options": {"temperature": 0, "repeat_penalty": 1.0, "num_ctx": 1024,
                        "num_predict": 64, "seed": 42},
        }
        if args.format == "json":
            payload["format"] = "json"
        resp = post(f"{args.url}/api/generate", payload)
        t, x, p, parsed, raw = extract_pred(resp.get("response", ""))
        gold = row["meta"]["intent"]
        if not parsed:
            parse_fails.append((i, gold, raw))
            conf[(gold, "?")] += 1
        else:
            conf[(gold, t)] += 1
            if t == gold:
                g4_correct += 1
                per_branch[gold][0] += 1
            else:
                mismatches.append((i, gold, t, row["meta"]["q"][:40], row["target"][:60],
                                   resp.get("response", "")[:80]))
            if gold == "tx" and t == "c":
                g2_leaks += 1
            # p 参数判分：gold_p 来自 target（今日起判分，此前仅展示）；m 剔除单报
            try:
                gold_p = json.loads(row["target"]).get("p") or {}
            except (ValueError, json.JSONDecodeError):
                gold_p = {}
            if isinstance(gold_p, dict) and gold_p and t == gold:
                keys = [k for k in gold_p if k != "m"]
                if keys:
                    diffs = [k for k in keys if not _p_key_match(k, gold_p.get(k), p.get(k))]
                    p_n += 1
                    if diffs:
                        p_fails.append((i, gold, {k: gold_p[k] for k in keys},
                                        {k: p.get(k) for k in keys}, diffs))
                    else:
                        p_ok += 1
                if "m" in gold_p:
                    m_n += 1
                    if p.get("m") == gold_p.get("m"):
                        m_ok += 1
        per_branch[gold][1] += 1
        gx = row["meta"].get("x", 0)
        x_n += 1
        if parsed and x == gx:
            x_ok += 1
        if (i + 1) % 25 == 0:
            print(f"  {i+1}/{len(rows)} elapsed={time.time()-t0:.0f}s", flush=True)

    n = len(rows)
    if n == 0:
        print("no eval rows loaded; check --data path", file=sys.stderr)
        sys.exit(1)
    g4 = 100 * g4_correct / n
    g3 = 100 * (n - len(parse_fails)) / n
    branch_acc = {b: (100 * per_branch[b][0] / per_branch[b][1] if per_branch[b][1] else None)
                  for b in BRANCHES}

    print("\n=== per-branch (G1) ===")
    for b in BRANCHES:
        c, tot = per_branch[b]
        acc = branch_acc[b]
        acc_s = f"{acc:5.1f}%" if acc is not None else "  n/a (0 rows)"
        print(f"  {b:3s} {c:3d}/{tot:<3d} = {acc_s}{'  <-- FAIL' if acc is not None and acc < 90 else ''}")
    print(f"G3 parse = {g3:.1f}%  ({len(parse_fails)} fails)")
    print(f"G4 overall = {g4:.1f}%")
    print(f"G2 tx->c leaks = {g2_leaks}")
    print(f"x-flag accuracy = {100*x_ok/x_n:.1f}% ({x_ok}/{x_n})  [report only]")
    if p_n:
        print(f"p-param accuracy = {100*p_ok/p_n:.1f}% ({p_ok}/{p_n})  "
              f"[report only; period/category 双侧归一, s sorted, m 剔除]")
    if m_n:
        print(f"m_exact = {100*m_ok/m_n:.1f}% ({m_ok}/{m_n})  [teacher 自由文本措辞复刻率, report only]")

    print("\n=== confusion gold->pred ===")
    print("        " + "  ".join(f"{b:>4s}" for b in BRANCHES) + "   ?")
    for g in BRANCHES:
        print(f"  {g:4s} " + "  ".join(f"{conf[(g,p)]:4d}" for p in BRANCHES) + f" {conf[(g,'?')]:4d}")

    if mismatches:
        print(f"\n=== mismatches ({len(mismatches)}) ===")
        for i, gold, pred, q, tgt, resp in mismatches:
            print(f"  #{i} gold={gold} pred={pred} q={q!r} gold_json={tgt!r} out={resp!r}")
    if parse_fails:
        print(f"\n=== parse fails ({len(parse_fails)}) ===")
        for i, gold, raw in parse_fails:
            print(f"  #{i} gold={gold} raw={raw!r}")
    if p_fails:
        print(f"\n=== p-param fails ({len(p_fails)}) ===")
        for i, gold, gp, pp, diffs in p_fails:
            print(f"  #{i} gold={gold} diff_keys={diffs} gold_p={gp!r} pred_p={pp!r}")

    # two-tier verdict (docs/pc-package-plan.md P1)
    tier1 = (all(branch_acc[b] is None or branch_acc[b] >= 90 for b in BRANCHES)
             and g2_leaks == 0 and g3 == 100 and g4 >= 90)
    parity_trigger = g4 <= 93 or any(
        branch_acc[b] is not None and branch_acc[b] <= 88 for b in BRANCHES)
    print(f"\nTier-1 PASS = {tier1}")
    print(f"Tier-2 PARITY investigate trigger = {parity_trigger} (threshold G4<=93 or branch<=88)")

    if args.out:
        json.dump({"model": args.model, "ollama": version, "format": args.format,
                   "n": n, "g4": g4, "g3": g3, "g2_leaks": g2_leaks,
                   "branch": {b: branch_acc[b] for b in BRANCHES},
                   "x_acc": 100 * x_ok / x_n, "tier1": tier1,
                   "parity_trigger": parity_trigger,
                   "p_acc": (100 * p_ok / p_n) if p_n else None, "p_n": p_n,
                   "m_exact": (100 * m_ok / m_n) if m_n else None, "m_n": m_n,
                   "mismatches": mismatches, "parse_fails": parse_fails,
                   "p_fails": p_fails},
                  open(args.out, "w"), ensure_ascii=False, indent=1)
        print(f"report -> {args.out}")

    sys.exit(0 if tier1 else 1)


if __name__ == "__main__":
    main()
