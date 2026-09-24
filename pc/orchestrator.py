#!/usr/bin/env python3
"""PC 编排器 v0 — P2（docs/pc-p2-design.md，三路评审后契约）。

输入 → SessionResolver（多轮还原，上下文永不进路由器）→ 路由（Ollama raw 契约）
→ executor 五分支。失败语义（设计定稿）：
  路由输出不可解析 → 判 c；路由调用不可达 → 整轮中止（禁止默认判 c）
  tx/pf/mkt 失败不回落 l（不编造红线）；relay=spend 路径禁止自动重试
  后端 200-损坏载荷/缺键 → 报错不崩溃，vlt 拒绝假零（#11/#12，panel-r2 §3-5）
上云唯一出口 = send_to_relay()——涂黑在其内部执行（egress 单点，设计 §0）。
配置：~/.firela-pa/config.toml（0600 强制，启动检查；env 覆盖：FIRELA_RELAY_API_KEY /
VLT_ACCESS_TOKEN）。orchestrator 永不写 config（tomllib 只读契约）——缺配置时打印
模板由用户保存（onboarding=print-not-write，化解 §3.1 与 L2 的张力）。

用法：python3 pc/orchestrator.py "这个月外卖花了多少"    # 单问
      python3 pc/orchestrator.py                         # REPL
"""

import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))                     # pc/（仓内布局）或 ~/.firela-pa（安装布局）
_FINETUNE = _HERE.parent / "finetune"
if _FINETUNE.is_dir():
    sys.path.insert(0, str(_FINETUNE))             # 仓内布局时 resolver/模板在 finetune/
from eval_router import extract_pred, wrap_raw      # noqa: E402  P1 实测 raw 契约
from redact import RedactError, redact             # noqa: E402
from session_resolver import SessionResolver       # noqa: E402
import telemetry                                    # noqa: E402  ZDC 问题日记（#1）
import verify                                       # noqa: E402  数字对账门（#3）
import memory                                       # noqa: E402  代理记忆（P9，本地腿专用）

CONFIG_PATH = Path.home() / ".firela-pa" / "config.toml"
OLLAMA = os.environ.get("OLLAMA_URL", "http://127.0.0.1:11434")
ROUTER_MODEL = "firela-router"
TIMEOUTS = {"router": 30, "vlt": 15, "openbb": 15, "relay": 120, "gen": 120}
_TODAY = date.today            # 时钟缝（eval_agent 固定时钟注入用；生产恒等 date.today）
CONFIG_TEMPLATE = """\
# ~/.firela-pa/config.toml  — 保存后执行: chmod 600 {path}
vlt_base_url = "https://<vlt-host>"      # vlt API（OCI prod 或 radxa dev）
vlt_region = "cn"                        # /api/v1/{{region}}/bean/...
vlt_access_token = "<access-token>"      # 换 180d JWT 用（实为全读写凭证，妥善保管）
relay_base_url = "https://<relay-host>"  # relay 网关（OpenAI 兼容 /v1/chat/completions）
relay_api_key = "<relay-key>"            # 建议专用 token+有限配额（relay 现成能力）
relay_model = "<model-id>"
openbb_base_url = "http://192.168.100.100:6900"  # mkt 行情（LAN dev；需与 openbb 同网）
gen_model = "qwen2.5:3b-instruct"       # l 分支本地生成（非思考原生，~2s；qwen3:4b 可选但思考链 ~45s）
speak_answers = false                   # 任何输入来源的回答都播报（需 --voice 语音链已装；未装则静默）
known_names = ["<你的称呼>", "<家人称呼>"]  # 姓名涂黑清单（设计 §3.1）
audit_overspend_pct = 30                # 主动审计：月支出超中位基线阈值 %（教育性缺省，P10）
audit_large_mult = 10                   # 主动审计：单笔大额 = 12m 单笔中位 × 倍数
"""

C_SYSTEM_PROMPT = ("你是本地财务顾问的云端算力腿。用户问句前可能附带一条数据上下文"
                   "（账本聚合，含 as_of 日期）：回答可引用其中数字，引用时原样保留两位小数，"
                   "比率尽量用文字表述（如「约四成」）。仍严禁编造上下文之外的账本数字"
                   "（余额/支出/资产）；用户问句自带的数字可以使用。上下文缺失而回答必须依赖"
                   "账本数据时，明确说「需接通账本工具后才能回答」。不提供具体资产配置比例、"
                   "具体标的买卖建议或调仓指令；只讲一般性教育原理。用中文简洁作答。")


class RouterUnavailable(Exception):
    pass


class VltAuthError(Exception):
    pass


class VltBadResponse(Exception):
    """vlt 200-应答结构损坏（缺键/非 dict）——报错不编造（#12 假零防线）。"""


def _post(url, payload, timeout, headers=None):
    req = urllib.request.Request(url, data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json",
                                          "User-Agent": "firela-pa-pc/0.1",  # CF 1010 拦 python-urllib UA（实测）
                                          **(headers or {})},
                                 method="POST")
    return json.loads(urllib.request.urlopen(req, timeout=timeout).read())


def _get(url, timeout, headers):
    req = urllib.request.Request(url, headers={"User-Agent": "firela-pa-pc/0.1", **(headers or {})}, method="GET")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def load_config():
    import tomllib
    cfg = {}
    if CONFIG_PATH.exists():
        if os.name == "posix" and (os.stat(CONFIG_PATH).st_mode & 0o077):
            sys.exit(f"配置文件权限过宽（应为 600）：{CONFIG_PATH}\n修复：chmod 600 {CONFIG_PATH}")
        cfg = tomllib.loads(CONFIG_PATH.read_text())
    cfg["relay_api_key"] = os.environ.get("FIRELA_RELAY_API_KEY", cfg.get("relay_api_key"))
    cfg["vlt_access_token"] = os.environ.get("VLT_ACCESS_TOKEN", cfg.get("vlt_access_token"))
    return cfg


def need(cfg, key):
    v = cfg.get(key)
    if not v or str(v).startswith("<"):
        raise SystemExit(f"缺少配置 {key}——编辑 {CONFIG_PATH}（模板：python3 pc/orchestrator.py --template）")
    return v


# ---------- 路由（P1 raw 契约；传输失败≠解析失败——设计失败语义） ----------

# 路由指令模板：安装布局（同目录）优先，仓内布局（finetune/）兜底
ROUTER_TEMPLATE = next(p for p in (_HERE / "prompt-template.txt",
                                   _FINETUNE / "prompt-template.txt") if p.exists()
                       ).read_text().rstrip("\n")


def _est_tokens(s):
    """守卫用粗估：CJK≈1 token/字，其余≈1/4 token/字符（宁保守）。"""
    cjk = sum(1 for c in s if ord(c) > 0x2E7F)
    return cjk + (len(s) - cjk) // 4


_ROUTER_Q_CAP = 1024 - _est_tokens(ROUTER_TEMPLATE) - 160  # num_ctx−模板−余量（think 包裹+64 生成+估差）


def call_router(question):
    """路由 prompt = 指令模板 + 问句（训练分布；eval.jsonl 的 prompt 同构）。
    超长问句保尾截断——左截断会吃掉指令模板致 parsed=False 静默判 c 上云（2026-09-21 实测洞），
    且路由问句常在粘贴文本末尾。"""
    if _est_tokens(question) > _ROUTER_Q_CAP:
        print(f"[截断守卫] 问句 {len(question)} 字超预算，保尾部进路由（前段未进路由器、未上云）",
              file=sys.stderr)
        question = question[-_ROUTER_Q_CAP:]
    payload = {"model": ROUTER_MODEL, "prompt": wrap_raw(f"{ROUTER_TEMPLATE}\n{question}"),
               "raw": True, "stream": False, "keep_alive": "30m",
               "options": {"temperature": 0, "repeat_penalty": 1.0, "num_ctx": 1024,
                           "num_predict": 64, "seed": 42}}
    try:
        r = _post(f"{OLLAMA}/api/generate", payload, TIMEOUTS["router"])
    except (urllib.error.URLError, OSError) as e:
        raise RouterUnavailable(
            f"本地路由模型不可达（{e}）——检查 ollama 服务与 {ROUTER_MODEL}；本轮中止，不走云端") from e
    t, x, p, parsed, _ = extract_pred(r.get("response", ""))   # p 由 extract_pred 归一带出（原内联 span 解析已收口）
    return t, p, x, parsed


# ---------- vlt（access token → 180d JWT；交换 401=token 失效；JWT 401 透明重换） ----------

# JWT 进程内缓存（#2：REPL/server 常驻进程不再每问重付交换；键=base+token）。
# ponytail: 并发下两线程同撞 401 会各换一次 JWT、后写覆盖——两次都有效，无害；
# 需要去重再加 per-key 锁。
_JWT_CACHE = {}


class Vlt:
    def __init__(self, cfg):
        self.base = need(cfg, "vlt_base_url").rstrip("/")
        self.region = cfg.get("vlt_region", "cn")
        self.token = need(cfg, "vlt_access_token")
        self._jwt = _JWT_CACHE.get((self.base, self.token))

    def _exchange(self):
        try:
            # 真实路径带 /api/v1 前缀（billclaw vlt-auth.ts 同源；实测 2026-09-19）
            r = _post(f"{self.base}/api/v1/auth/sessions/anonymous",
                      {"accessToken": self.token}, TIMEOUTS["vlt"])
        except urllib.error.HTTPError as e:
            if e.code == 401:
                raise VltAuthError("vlt access token 失效——请重新配置（编辑 "
                                   f"{CONFIG_PATH} 的 vlt_access_token）") from e
            raise
        if not isinstance(r, dict) or "authToken" not in r:
            raise VltBadResponse("auth 交换应答缺 authToken（结构损坏）")
        self._jwt = r["authToken"]
        _JWT_CACHE[(self.base, self.token)] = self._jwt
        return self._jwt

    def _auth_header(self):
        return {"Authorization": f"Bearer {self._jwt or self._exchange()}"}

    def _call(self, path, **params):
        url = f"{self.base}/api/v1/{self.region}/bean/{path}"
        if params:
            url += "?" + "&".join(f"{k}={urllib.parse.quote(str(v))}" for k, v in params.items() if v is not None)
        try:
            return _get(url, TIMEOUTS["vlt"], self._auth_header())
        except urllib.error.HTTPError as e:
            if e.code == 401 and self._jwt:      # JWT 过期 → 透明重换一次（设计 M1）
                self._jwt = None
                return _get(url, TIMEOUTS["vlt"], self._auth_header())
            raise

    def transactions(self, date_from, date_to, search=None, max_n=500):
        """分页拉全（limit @Max(100) 实测；列表在 .data——accounts 在 .items，后端不一致）。
        拉到短页为止、千条硬顶——truncated 只在硬顶时为真（恰满页不再多拉一页，取保守）。"""
        out, offset = [], 0
        while len(out) < max_n * 2:
            page = self._call("transactions", dateFrom=date_from, dateTo=date_to,
                              search=search, limit=100, offset=offset)
            batch = page.get("data") if isinstance(page, dict) else None
            if not isinstance(batch, list):                 # 缺 data 键 ≠ 空列表（#12 拒绝假零）
                raise VltBadResponse("列表应答缺 data 键（结构损坏，拒绝报 0 笔）")
            out.extend(batch)
            if len(batch) < 100:
                # 截断标志=切片真丢行（500–999 行短页路径曾被误标 False——sim/metrics 静默低估源）
                return {"data": out[:max_n], "truncated": len(out) > max_n}
            offset += 100
        return {"data": out[:max_n], "truncated": True}

    def accounts(self):
        r = self._call("accounts", limit=200)
        if not isinstance(r, dict) or not isinstance(r.get("items"), list):   # #12
            raise VltBadResponse("accounts 应答缺 items 键（结构损坏）")
        return r

    def balances(self, account):
        # multi-currency 单账户口径（实测）；单币种端点强制 currency 参数
        return self._call("balances/multi-currency", account=account)


# ---------- 分支实现 ----------

def normalize_period(p, today):
    """口语时间词 → (dateFrom, dateTo)。未识别默认当月（SPEC：p 可能出口语值）。"""
    p = (p or "").strip()
    y, m = today.year, today.month
    first = date(y, m, 1)
    prev_last = first - timedelta(days=1)
    prev_first = date(prev_last.year, prev_last.month, 1)
    q_first = date(y, (m - 1) // 3 * 3 + 1, 1)                 # 本季度首日
    pq_last = q_first - timedelta(days=1)                      # 上季度末
    pq_first = date(pq_last.year, (pq_last.month - 1) // 3 * 3 + 1, 1)
    table = {
        "本月": (first, today), "这个月": (first, today),
        "上个月": (prev_first, prev_last), "上月": (prev_first, prev_last),
        "本季度": (q_first, today), "这个季度": (q_first, today),
        "上个季度": (pq_first, pq_last), "上季度": (pq_first, pq_last),
        "今年": (date(y, 1, 1), today), "去年": (date(y - 1, 1, 1), date(y - 1, 12, 31)),
    }
    if re.fullmatch(r"\d{4}-\d{2}", p):
        yy, mm = map(int, p.split("-"))
        s = date(yy, mm, 1)
        e = date(yy + (mm == 12), mm % 12 + 1, 1) - timedelta(days=1)
        return s.isoformat(), e.isoformat()
    span = table.get(p, (first, today))
    return (span[0].isoformat(), span[1].isoformat()) if span else (first.isoformat(), today.isoformat())


def _tx_expense(t):
    """支出额 = Expenses 账户 posting 的 units 之和（真实契约：金额在 postings，非顶层）。"""
    try:
        return sum(float(p.get("units", 0)) for p in t.get("postings", [])
                   if str(p.get("account", "")).startswith("Expenses"))
    except (TypeError, ValueError):
        return 0.0


# 类别同义词（执行器层归一化：口语类别词 → account-standards 英文段；全表应后续交 dir/判例表）
CAT_SYNONYMS = {"外卖": "Delivery", "餐饮": "Dining", "吃饭": "Dining", "聚餐": "Dining",
                "交通": "Transport", "交通费": "Transport", "话费": "Phone", "咖啡": "Coffee",
                "超市购物": "Groceries", "超市采购": "Groceries", "超市": "Groceries", "买菜": "Groceries"}
# 聚合词=无类别过滤（路由器 p 槽噪声：「总共花了多少」→category=总支出→零过滤零结果；
# 归一层归一，与期间口语值同辖区——agent eval 真基线 2026-09-23 发现）
AGG_CATEGORIES = {"总支出", "支出总额", "总消费", "全部支出", "全部消费", "支出", "总花销",
                  "花销", "消费总额", "总花费", "花费总额"}
# 中文标的映射（yfinance 不认中文名；全表应后续交 dir 商家/机构库）
ZH_SYMBOLS = {"贵州茅台": "600519.SS", "茅台": "600519.SS", "五粮液": "000858.SZ",
              "腾讯": "0700.HK", "阿里巴巴": "BABA", "宁德时代": "300750.SZ",
              "比亚迪": "002594.SZ", "苹果": "AAPL",
              "平安银行": "000001.SZ", "招商银行": "600036.SS", "工商银行": "601398.SS"}


def branch_tx(p, cfg):
    date_from, date_to = normalize_period((p or {}).get("period"), _TODAY())
    page = Vlt(cfg).transactions(date_from, date_to)
    items, truncated = page["data"], page["truncated"]
    expenses = [t for t in items if _tx_expense(t) > 0]
    cat = (p or {}).get("category")
    if cat in AGG_CATEGORIES:
        cat = None                                         # 聚合词 = 全类查询
    if cat:
        syn = CAT_SYNONYMS.get(cat, "")
        expenses = [t for t in expenses
                    if cat in t.get("narration", "")
                    or any(cat in str(p_.get("account", ""))
                           or (syn and syn in str(p_.get("account", "")))
                           for p_ in t.get("postings", []))]
    total_by_cur = _exp_by_cur(expenses)                # #13：按币种分列（原混币单值——pf 同款纪律）
    parts = " / ".join(f"{c} {v:,.2f}" for c, v in sorted(total_by_cur.items()))
    lines = []
    if truncated:                                       # #10：⚠ 提首行（语音 _speak_out 只念前 3 行——尾部警示会被吃掉）
        lines.append("  ⚠ 达到 500 笔查询上限，以下为部分数据")
    lines.append(f"{date_from}~{date_to}{' · ' + cat if cat else ''}: {len(expenses)} 笔支出，合计 {parts}")
    for t in expenses[:3]:
        lines.append(f"  {t.get('date', '?')} {t.get('narration', '')[:24]} {_tx_expense(t):.2f}")
    if len(expenses) > 3:
        lines.append(f"  …共 {len(expenses)} 笔")
    return "\n".join(lines)


def branch_pf(cfg):
    vlt = Vlt(cfg)
    accounts = [a for a in vlt.accounts().get("items", [])
                if a.get("type") in ("Assets", "Liabilities")]   # 净资产只算资产/负债账户
    with ThreadPoolExecutor(max_workers=8) as ex:                # #2：串行 N×RTT → 8 并发
        bal_maps = list(ex.map(lambda a: _pf_one(vlt, a), accounts))
    # 三行分列（A5 首批信号②：单口径全景答不了「欠多少 / 净资产 vs 总资产」分列问法）
    assets, debts = {}, {}
    for a, b in zip(accounts, bal_maps):
        bucket = assets if a.get("type") == "Assets" else debts
        for cur, amt in b.items():
            bucket[cur] = bucket.get(cur, 0.0) + float(amt or 0)
    net = {cur: assets.get(cur, 0.0) + debts.get(cur, 0.0)       # 负债余额为负值，直接相加
           for cur in set(assets) | set(debts)}

    def _lines(d):
        return "\n".join(f"    {c}: {abs(v):,.2f}" for c, v in sorted(d.items())) or "    （无）"

    return (f"资产全景（{len(accounts)} 个资产/负债账户，按币种分列）：\n"
            f"  资产合计：\n{_lines(assets)}\n"
            f"  负债合计：\n{_lines(debts)}\n"
            f"  净资产：\n{_lines(net)}")


def _pf_one(vlt, account):
    try:
        return vlt.balances(account.get("path", "")).get("balances") or {}
    except Exception:
        return {}                                          # 单账户失败不拖垮全景（原语义）


# ---------- 度量执行器（P7.5：趋势/环比/同比/占比/储蓄率——月窗客户端聚合） ----------

_MONTHS_CN = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9,
              "十": 10, "十一": 11, "十二": 12, "1月": 1, "2月": 2, "3月": 3, "4月": 4, "5月": 5,
              "6月": 6, "7月": 7, "8月": 8, "9月": 9, "10月": 10, "11月": 11, "12月": 12}


def _month_bounds(y, m, today):
    """月窗 (first, last)——当前月 last=今天（与 normalize_period 本月口径一致）。"""
    first = date(y, m, 1)
    last = date(y + (m == 12), m % 12 + 1, 1) - timedelta(days=1)
    if (y, m) == (today.year, today.month):
        last = today
    return first.isoformat(), last.isoformat()


def _win_txns(vlt, df, dt):
    """月窗交易 + 截断标志（聚合腿全保真取数：max_n=2000 覆盖重月；截断窗聚合值须带 ⚠
    ——防把部分和当事实。branch_tx 列表腿有意保持 500+⚠（UX 截断，范围另裁——#17）。"""
    page = vlt.transactions(df, dt, max_n=2000)
    return page["data"], bool(page.get("truncated"))


def _cat_filter(expenses, cat):
    syn = CAT_SYNONYMS.get(cat, "")
    return [t for t in expenses
            if cat in t.get("narration", "")
            or any(cat in str(p_.get("account", "")) or (syn and syn in str(p_.get("account", "")))
                   for p_ in t.get("postings", []))]


def _exp_by_cur(expenses):
    tot = {}
    for t in expenses:
        cur = next((p.get("currency", "CNY") for p in t.get("postings", [])
                    if str(p.get("account", "")).startswith("Expenses")), "CNY")
        tot[cur] = tot.get(cur, 0.0) + _tx_expense(t)
    return tot


def _inc_by_cur(items):
    tot = {}
    for t in items:
        for p in t.get("postings", []):
            if str(p.get("account", "")).startswith("Income"):
                v = -float(p.get("units", 0))
                if v > 0:
                    c = p.get("currency", "CNY")
                    tot[c] = tot.get(c, 0.0) + v
    return tot


def branch_metrics(kind, p, cfg):
    """度量腿：趋势/环比/同比/占比 top-3/储蓄率（月窗 transactions 客户端聚合）。"""
    today = _TODAY()
    vlt = Vlt(cfg)
    p = p or {}
    cat = p.get("category")
    y, m = today.year, today.month

    def _cur_exp(yy, mm):
        df, dt = _month_bounds(yy, mm, today)
        rows, tr = _win_txns(vlt, df, dt)
        exp = [t for t in rows if _tx_expense(t) > 0]
        if cat:
            exp = _cat_filter(exp, cat)
        return _exp_by_cur(exp), tr

    def _fmt(tot):
        return " + ".join(f"{v:,.2f} {c}" for c, v in sorted(tot.items())) if tot else "0.00"

    _TRUNC_NOTE = "  ⚠ 达到 500 笔查询上限，以上为部分数据"

    if kind == "trend":
        n = int(p.get("months", 6))
        lines, trunc_months = [], []
        for k in range(n - 1, -1, -1):
            _t = y * 12 + (m - 1) - k                 # 绝对月算术（负月不回卷错年）
            yy, mm = _t // 12, _t % 12 + 1
            tot, tr = _cur_exp(yy, mm)
            if tr:
                trunc_months.append(f"{yy:04d}-{mm:02d}")
            lines.append(f"  {yy:04d}-{mm:02d}: {_fmt(tot)}")
        if trunc_months:
            lines.append(f"  ⚠ 达到 500 笔查询上限（数据被截断，按部分数据聚合）：{'、'.join(trunc_months)}")
        head = f"近 {n} 个月支出逐月（{f'{cat}·' if cat else ''}按月窗聚合）："
        return head + "\n" + "\n".join(lines)

    if kind == "mom":
        py, pm = (y - 1, 12) if m == 1 else (y, m - 1)
        prev, ptr = _cur_exp(py, pm)
        cur, ctr = _cur_exp(y, m)
        pc = ((cur.get("CNY", 0) - prev.get("CNY", 0)) / prev["CNY"] * 100) if prev.get("CNY") else None
        delta = f"（环比 {pc:+.1f}%）" if pc is not None else ""
        return (f"环比（{cat or '全类'}，CNY 口径）：\n"
                f"  上月 {py:04d}-{pm:02d}: {_fmt(prev)}\n"
                f"  本月 {y:04d}-{m:02d}: {_fmt(cur)}{delta}"
                + (f"\n{_TRUNC_NOTE}" if ptr or ctr else ""))

    if kind == "yoy":
        mm = int(p.get("month", m))
        prev, ptr = _cur_exp(y - 1, mm)
        cur, ctr = _cur_exp(y, mm)
        pc = ((cur.get("CNY", 0) - prev.get("CNY", 0)) / prev["CNY"] * 100) if prev.get("CNY") else None
        delta = f"（同比 {pc:+.1f}%）" if pc is not None else ""
        return (f"同比（{cat or '全类'}，CNY 口径）：\n"
                f"  去年 {y-1:04d}-{mm:02d}: {_fmt(prev)}\n"
                f"  今年 {y:04d}-{mm:02d}: {_fmt(cur)}{delta}"
                + (f"\n{_TRUNC_NOTE}" if ptr or ctr else ""))

    if kind == "topn":
        df, dt = normalize_period(p.get("period"), today)
        rows, tr = _win_txns(vlt, df, dt)
        exp = [t for t in rows if _tx_expense(t) > 0]
        groups = {}
        for t in exp:
            seg = next((str(p_.get("account", "")).split(":")[-1] for p_ in t.get("postings", [])
                        if str(p_.get("account", "")).startswith("Expenses")), "?")
            cur_ = next((p_.get("currency", "CNY") for p_ in t.get("postings", [])
                         if str(p_.get("account", "")).startswith("Expenses")), "CNY")
            g = groups.setdefault((seg, cur_), [0, 0.0])
            g[0] += 1
            g[1] += _tx_expense(t)
        cny_total = sum(v for (s, c), (n, v) in groups.items() if c == "CNY")
        top = sorted(groups.items(), key=lambda kv: -kv[1][1])[:3]
        lines = [f"  {s}（{c}）: {n} 笔 {v:,.2f}"
                 + (f" · 占 {v / cny_total * 100:.0f}%" if c == "CNY" and cny_total else "")
                 for (s, c), (n, v) in top] or ["  （无支出）"]
        note = "" if len({c for (_, c) in groups}) <= 1 else "\n  （多币种分列，占比按 CNY 口径）"
        return (f"{df}~{dt} 支出构成 top-3（按类目末段聚合）：\n" + "\n".join(lines) + note
                + (f"\n{_TRUNC_NOTE}" if tr else ""))

    if kind == "savrate":
        df, dt = normalize_period(p.get("period"), today)
        items, tr = _win_txns(vlt, df, dt)
        inc, exp = _inc_by_cur(items), _exp_by_cur([t for t in items if _tx_expense(t) > 0])
        lines = []
        for c in sorted(set(inc) | set(exp)):
            i_, e_ = inc.get(c, 0.0), exp.get(c, 0.0)
            rate = f"{(i_ - e_) / i_ * 100:.1f}%" if i_ > 0 else "n/a（无收入）"
            lines.append(f"  {c}: 收入 {i_:,.2f} · 支出 {e_:,.2f} · 储蓄率 {rate}")
        return (f"{df}~{dt} 储蓄率（收入=Income 类 posting，按币种分列）：\n" + "\n".join(lines)
                + (f"\n{_TRUNC_NOTE}" if tr else ""))

    return f"度量类型未实现：{kind}"


def _metric_hook(q):
    """pre-router 确定性词典：度量类问句 → (kind, p)。零重训（D7 mods② 同机制）。"""
    p = {}
    for kw in CAT_SYNONYMS:
        if kw in q:
            p["category"] = kw
            break
    if re.search(r"上个月|上月", q):
        p["period"] = "上个月"
    elif re.search(r"去年", q):                      # topn/savrate 窗口词覆盖（缺省回落当月=错窗静默）
        p["period"] = "去年"
    elif re.search(r"今年", q):
        p["period"] = "今年"
    elif re.search(r"上个?季度", q):
        p["period"] = "上个季度"
    if re.search(r"储蓄率|存下.*比例|能存下多少", q):
        return "savrate", p
    if re.search(r"趋势|走势", q) and re.search(r"支出|花|消费", q):
        p["months"] = 12 if re.search(r"一年|12 ?个?月|近12", q) else 6
        return "trend", p
    if re.search(r"环比|比上个月|跟上个月比|和上个月比", q):
        return "mom", p
    if re.search(r"同比|去年.*(今年|同月)|比去年", q):
        mm = re.search(r"([一二三四五六七八九十]{1,2}|\d{1,2})\s*月", q)
        if mm:
            p["month"] = (_MONTHS_CN.get(mm.group(1)) or _MONTHS_CN.get(mm.group(1) + "月")
                          or _TODAY().month)                # 未识别月兜底当月
        return "yoy", p
    if re.search(r"占比|花在哪|大头|主要花|构成", q):
        return "topn", p
    return None


def branch_l(question, cfg, stats=None):
    model = cfg.get("gen_model", "qwen2.5:3b-instruct")
    # qwen3:4b 的自然分布就是先思考：强压思考（think 参数/raw 空块/no_think）实测全部
    # 产生更糟的无标签推理（P3 演练三连败）——让它自然思考，剥到 </think> 之后即干净
    # 答案。代价：~10s 思考延迟（num_predict 512 覆盖思考+答案）
    payload = {"model": model, "prompt": question, "stream": False, "keep_alive": "30m",
               "system": ("你是简洁的中文助手，回答不超过两句话。你当前没有账本与行情工具，"
                          "严禁编造任何账本数字（余额/支出/资产）或股价。用户问句自带的数字可以使用。"
                          "若回答必须依赖这类数据，明确说「需接通账本/行情工具后才能回答」。"
                          "不提供具体资产配置比例、具体标的买卖建议或调仓指令。"),
               "options": {"temperature": 0.3, "num_predict": 512}}
    try:
        r = _post(f"{OLLAMA}/api/generate", payload, TIMEOUTS["gen"])
    except urllib.error.HTTPError as e:
        if e.code == 404:
            raise SystemExit(f"本地生成模型未安装：ollama pull {model}") from e
        raise
    content = r.get("response", "")
    if stats is not None:                                 # decode tokens 出参（勿挂共享 cfg——并发串号，OCR R1）
        stats["eval_count"] = r.get("eval_count")
    if "</think>" in content:
        content = content.rsplit("</think>", 1)[1]
    return re.sub(r"<think>.*?</think>", "", content, flags=re.S).strip()


def branch_mkt(p, cfg):
    base = cfg.get("openbb_base_url", "")
    if not base or str(base).startswith("<"):
        return "行情暂不可达（未配置 openbb_base_url——PC 需与 openbb 实例同网或填公网地址）"
    base = base.rstrip("/")
    symbols = (p or {}).get("s") or []
    m = str((p or {}).get("m", "q"))
    # 口语值归一（股价/现价→q；\d+d 变体〔5d/7d〕→hist——agent eval 基线 2026-09-23 发现）
    m = "hist" if ("hist" in m or "历史" in m or "走势" in m or re.fullmatch(r"\d+d", m)) else "q"
    out = []
    for raw_sym in symbols:
        sym = ZH_SYMBOLS.get(raw_sym, raw_sym)
        try:
            if m == "hist":
                end, start = _TODAY(), _TODAY() - timedelta(days=14)
                r = _get(f"{base}/api/v1/equity/price/historical?symbol={urllib.parse.quote(sym)}"
                         f"&provider=yfinance&start_date={start}&end_date={end}",
                         TIMEOUTS["openbb"], {})
                rows = r.get("results", [])[-5:]
                closes = "  ".join(f"{x['date'][5:]} {x['close']:.2f}" for x in rows)
                out.append(f"{sym} 近 {len(rows)} 日收盘：{closes}" if rows else f"{sym} 无历史数据")
            else:
                r = _get(f"{base}/api/v1/equity/price/quote?symbol={urllib.parse.quote(sym)}"
                         f"&provider=yfinance", TIMEOUTS["openbb"], {})
                q = r["results"][0]
                # ETF 变体无 last_price（实测 QQQ/VTI）——回退链：last→close→bid/ask 中点→prev_close
                price = q.get("last_price") or q.get("close")
                if not price and q.get("bid") and q.get("ask"):
                    price = (q["bid"] + q["ask"]) / 2
                price = price or q.get("prev_close")
                if not price:
                    out.append(f"{raw_sym}：行情返回无价格字段（数据源形态异常）")
                    continue
                base_px = q.get("prev_close") or price
                chg = (price - base_px) / base_px * 100 if base_px else 0.0
                out.append(f"{q.get('name') or raw_sym}（{sym}）现价 {price:.2f}  今日 {chg:+.2f}%")
        except Exception as e:
            out.append(f"{sym}：行情暂不可达（{e.__class__.__name__}——代码可能需映射，如中文标的）")
    return "\n".join(out) if out else "未识别到标的代码"


# ---------- P8 带数据的 c 腿：数据上下文（D4 方案 A；仅聚合、按构造排除 narration） ----------

_CTX_CACHE = {}                                  # ponytail: (base, token)→(monotonic, json)；
_CTX_TTL = 600                                   # 10min TTL——12 月窗逐月拉取 3-8s/次（先例 _JWT_CACHE）


def build_data_context(vlt, today):
    """账本聚合 → 上云数据上下文 JSON 串（None=降级：异常/超 2KB，云调用照发 2 消息旧形状）。

    字段级 egress（面板 D4 mod4）：仅聚合数字 + 账户末段结构键（纯数字长段=身份样，
    弃——敌意账户名 canary 面），narration 自由文本按构造不进入；身份 canary 断言在
    r2_check（eval 件 2）。截断月如实降注（部分和披露）。
    """
    import time as _t
    key = (vlt.base, vlt.token)
    hit = _CTX_CACHE.get(key)
    if hit and _t.monotonic() - hit[0] < _CTX_TTL:
        return hit[1]
    try:
        exp = inc = 0.0
        cat, trunc_months = {}, []
        y0, m0 = today.year, today.month
        for k in range(12):
            _tmon = y0 * 12 + (m0 - 1) - k
            yy, mm = _tmon // 12, _tmon % 12 + 1
            rows, trunc = _win_txns(vlt, *_month_bounds(yy, mm, today))
            if trunc:
                trunc_months.append(f"{yy:04d}-{mm:02d}")
            for t in rows:
                row_cny = sum(float(p.get("units", 0) or 0) for p in t.get("postings", [])
                              if str(p.get("account", "")).startswith("Expenses")
                              and p.get("currency") == "CNY")            # CNY-only（ledger_params 同口径）
                if row_cny > 0:
                    seg = next((str(p_.get("account", "")).split(":")[-1]
                                for p_ in t.get("postings", [])
                                if str(p_.get("account", "")).startswith("Expenses")
                                and p_.get("currency") == "CNY"), None)
                    if seg and not re.fullmatch(r"[\dXx\-]{7,}", seg):    # 结构键卫生：身份样段弃
                        cat[seg] = cat.get(seg, 0.0) + row_cny
                    exp += row_cny
                else:
                    for p in t.get("postings", []):
                        if str(p.get("account", "")).startswith("Income") and p.get("currency") == "CNY":
                            v = -float(p.get("units", 0) or 0)
                            if v > 0:
                                inc += v
        accounts = [a for a in vlt.accounts().get("items", []) if a.get("type") in ("Assets", "Liabilities")]
        with ThreadPoolExecutor(max_workers=8) as ex:                    # branch_pf 同款并发
            bals = list(ex.map(lambda a: _pf_one(vlt, a), accounts))
        assets, debts = {}, {}
        for a, b in zip(accounts, bals):
            bucket = assets if a.get("type") == "Assets" else debts
            for cur, amt in b.items():
                bucket[cur] = bucket.get(cur, 0.0) + float(amt or 0)
        ctx = {
            "as_of": today.isoformat(),
            "net_worth": {c: round(assets.get(c, 0) + debts.get(c, 0), 2) for c in sorted(set(assets) | set(debts))},
            "assets_total": {c: round(assets[c], 2) for c in sorted(assets)},
            "liabilities_total": {c: round(debts[c], 2) for c in sorted(debts)},
            "trailing_12m_cny": {"expense": round(exp, 2), "income": round(inc, 2),
                                 "saving": round(inc - exp, 2)},
            "expense_top3_12m_cny": [{"category": s, "amount": round(v, 2)}
                                     for s, v in sorted(cat.items(), key=lambda kv: -kv[1])[:3]],
        }
        if trunc_months:
            ctx["data_note"] = f"{'、'.join(trunc_months)} 触达单窗查询上限，相关聚合为部分和"
        blob = json.dumps(ctx, ensure_ascii=False, separators=(",", ":"))
        if len(blob.encode("utf-8")) > 2048:
            print(f"[数据上下文] {len(blob.encode('utf-8'))}B 超 2048 上限——降级不带上下文", file=sys.stderr)
            return None
        _CTX_CACHE[key] = (_t.monotonic(), blob)
        return blob
    except (Exception, SystemExit) as e:         # 上下文是增强件：任何失败不杀云调用
        if hit:
            print(f"[数据上下文] 刷新失败（{type(e).__name__}）——serve stale", file=sys.stderr)
            return hit[1]
        print(f"[数据上下文] 构建失败（{type(e).__name__}: {e}）——不带上下文上云", file=sys.stderr)
        return None


def _ctx_for(cfg):
    """c 腿数据上下文入口：缺 vlt 配置（SystemExit）/构造失败 → None。"""
    try:
        return build_data_context(Vlt(cfg), _TODAY())
    except (Exception, SystemExit):
        return None


def send_to_relay(question, x_flag, cfg, data_context=None, memory_context=None, audit_context=None):
    """全仓唯一 relay 出口（设计 §0 egress 单点）：涂黑在内部执行，断言后置条件。
    消息表契约（P8，R2 断言面）：[system] + 可选 [user(数据上下文 JSON)] + [user(涂黑问句)]。
    记忆门（P9，D5 mod1 红线）：memory_context 恒 None——代理记忆禁入上云载荷，
    c 腿注入须用户显式修订裁定才开（结构保证：无调用方传非 None）。
    审计门（P10，D10 预裁）：audit_context 恒 None——审计文本禁入上云载荷（同款结构保证）。
    spend 路径：禁止自动重试（评审 H2）；401/403 统一文案（评审 M2）。"""
    assert memory_context is None, "代理记忆禁入上云载荷（D5 mod1 红线）"
    assert audit_context is None, "审计文本禁入上云载荷（P10 预裁红线）"
    try:
        text, stats = redact(question, cfg.get("known_names", []))
    except RedactError as e:
        return f"已拦截：问句含无法安全替换的身份信息（{e}）。请改写后重试。", None
    if x_flag and not any(stats.values()):
        print("[隐私绊线] 路由器标记含身份信息但涂黑器零命中——最可能是姓名（known_names 未覆盖）",
              file=sys.stderr)
    messages = [{"role": "system", "content": C_SYSTEM_PROMPT}]
    if data_context:
        messages.append({"role": "user", "content": data_context})
    messages.append({"role": "user", "content": text})
    payload = {"model": need(cfg, "relay_model"), "stream": False, "messages": messages}
    try:
        r = _post(f"{need(cfg, 'relay_base_url').rstrip('/')}/v1/chat/completions",
                  payload, TIMEOUTS["relay"],
                  headers={"Authorization": f"Bearer {need(cfg, 'relay_api_key')}"})
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            return "relay key 无效、已禁用或配额耗尽——请查 relay 控制台（该 key 建议专用+限配额）", None
        return f"relay 服务异常（HTTP {e.code}）。本次未自动重试（计费路径）——可手动重发。", None
    except (urllib.error.URLError, OSError) as e:
        return f"relay 不可达（{e}）。本次未自动重试（计费路径）——可手动重发。", None
    except ValueError as e:                                   # 200-非 JSON body（#11：报错非崩溃）
        return f"relay 应答损坏（非 JSON：{e}）。本次未自动重试——可手动重发。", None
    try:
        answer = r["choices"][0]["message"]["content"].strip()
    except (KeyError, IndexError, TypeError):                 # 200-缺 choices/message/content（#11）
        return "relay 应答损坏（缺 choices/message/content）。本次未自动重试——可手动重发。", None
    usage = (r.get("usage") or {}).get("total_tokens")        # #9：c 腿成本遥测（缺 usage 面=None）
    redacted_note = "（已涂黑: " + ",".join(f"{k}×{v}" for k, v in stats.items() if v) + "）" if any(stats.values()) else ""
    return answer + (f"\n{redacted_note}" if redacted_note else ""), usage


# 能力外快答（A5 信号④：时间/天气类问句交给 LLM 会得到不通反问或套话——
# 确定性诚实短答优于生成腿硬答；写诗类创作题不拦，模型答得好）
_QUICK_REPLIES = (
    (re.compile(r"几点|现在什么时间|现在时间"), "我没有时钟，看不了现在几点——手机状态栏更准。财务问题随时问我。"),
    (re.compile(r"天气|气温|下雨"), "我没有天气数据，查不了天气。财务问题随时问我。"),
)


def _sim_tail(entry, resolved, question, t0, t, out, source, resolver):
    resolver.update(resolved, t, {})                 # 与其他分支一致：喂 resolved（非原始问句）
    telemetry.record(entry, question, t, (time.monotonic() - t0) * 1000,
                     answer_len=len(out), source=source)


# ---------- P9 保存门：显式「记住」指令（D5 mod3；锚定触发、绝不落穿到路由） ----------

_MEM_SAVE_START = ("记住", "请记住", "帮我记住", "remember", "Remember")
_MEM_TRIGGER = re.compile(r"^(?:请|帮我)?记住|^remember|就这样记住|按这样记住|记住了什么|记了什么",
                          re.I)


def _profile_citation(prof):
    parts = []
    if "retirement_age" in prof:
        parts.append(f"目标退休年龄 {prof['retirement_age']:g} 岁")
    if "wr" in prof:
        parts.append(f"提款率 {prof['wr'] * 100:g}%")
    if "real_return" in prof:
        parts.append(f"实际年化 {prof['real_return'] * 100:g}%")
    if "inflation" in prof:
        parts.append(f"预期通胀 {prof['inflation'] * 100:g}%（存档未用于计算）")
    return "已按你的假设档案：" + "、".join(parts) + "。"


def _mem_gate(resolved, source):
    """解析「记住…」显式指令 → 保存（经 memory.save 的 sanity+synthetic 门）/反问。

    解析在前、source 门在写时——synthetic 的合法指令也得「没记成」回复（mod4），
    越界值无论 source 一律反问不落库。不可解析 → 使用提示，绝不落穿到路由
    （记忆意图上云 = egress 红线观感）。
    """
    import sim
    if "名义" in resolved:
        return ("你提到「名义」口径——我只按实际口径（今日购买力）记假设。"
                "请换算后说，例如「记住实际年化 4%」。")
    m = (re.search(r"(\d+(?:\.\d+)?)\s*%\s*(?:的)?\s*(提款|提取|年化|收益|利率|回报)", resolved)
         or re.search(r"(提款率?|提取|年化|收益率?|利率|回报率?)\s*(?:是|为|：|:)?\s*(\d+(?:\.\d+)?)\s*%", resolved))
    if m:
        word = m.group(1 if m.re.pattern.startswith("(提款") else 2)
        num = m.group(2 if m.re.pattern.startswith("(提款") else 1)
        x = float(num) / 100
        key = "wr" if word.startswith(("提款", "提取")) else "real_return"
        ok, note = memory.save(key, x, source, formula_version=sim.SIM_FORMULA_VERSION,
                               ledger_as_of=_TODAY().isoformat())
        if ok:
            name = "提款率" if key == "wr" else "预期实际年化"
            return (f"已记住：{name} {x * 100:g}%（实际口径，{sim.SIM_FORMULA_VERSION}，"
                    f"账本口径 {_TODAY().isoformat()}）。说「我记住了什么」可查看。")
        return f"没记成——{note}。"
    m = re.search(r"(\d{2})\s*岁", resolved)
    if m and ("退休" in resolved or "年龄" in resolved):
        ok, note = memory.save("retirement_age", int(m.group(1)), source,
                               formula_version=sim.SIM_FORMULA_VERSION,
                               ledger_as_of=_TODAY().isoformat())
        return (f"已记住：目标退休年龄 {int(m.group(1))} 岁。" if ok else f"没记成——{note}。")
    if re.search(r"就这样记住|记住现在|按这样记住", resolved):
        if sim.STATE is not None and sim.STATE.ttl > 0:
            # 显式指令=存当前会话 wr/r 全量（与默认值相同也无妨——用户点名要记）
            memory.save("wr", sim.STATE.wr, source, formula_version=sim.SIM_FORMULA_VERSION,
                        ledger_as_of=_TODAY().isoformat())
            memory.save("real_return", sim.STATE.r, source, formula_version=sim.SIM_FORMULA_VERSION,
                        ledger_as_of=_TODAY().isoformat())
            return (f"已记住：提款率 {sim.STATE.wr * 100:.1f}%、实际年化 {sim.STATE.r * 100:.1f}%"
                    "（实际口径）。说「我记住了什么」可查看。")
        return ("当前没有进行中的模拟会话——先问「我什么时候能退休」再做 what-if，"
                "然后说「就这样记住」。")
    m = re.search(r"目标(?:是|：|:)?\s*(.+)$", resolved)
    if m:
        text = m.group(1).strip()[:80]
        ok, note = memory.add_goal(text, source)
        return f"已记住目标：{text}" if ok else f"没记成——{note}。"
    if re.search(r"记住了什么|记了什么", resolved):
        return memory.inspect()
    return ("想记住什么？例如「记住提款率 3.5%」「记住目标：2035 年退休」"
            "——或模拟会话里说「就这样记住」。")


def handle(question, resolver, cfg, entry="repl", trace=None, source="human"):
    t0 = time.monotonic()
    gen = {}                                               # branch_l 出参容器（每问局部，并发安全）
    _ctx = None                                            # P8 c 腿数据上下文（None=不带）
    _cloud_tokens = None                                   # #9：c 腿 usage（send_to_relay 元组契约）
    try:
        resolved = resolver.resolve(question) or question
    except Exception:
        resolved = question                                # 设计 L3：resolver 异常原样直通
    for _pat, _reply in _QUICK_REPLIES:                    # 能力外确定性快答（零 LLM、零上云）
        if _pat.search(resolved):
            t, p, x, parsed = "l", {}, 0, True
            if trace is not None:
                trace.update(resolved=resolved, t=t, p=p, parsed=parsed)
            out = _reply
            telemetry.record(entry, question, t, (time.monotonic() - t0) * 1000,
                             answer_len=len(out), source=source)
            return out
    if _MEM_TRIGGER.search(resolved):
        t, p, x, parsed = "mem", {}, 0, True               # P9 保存门：锚定触发、不落穿路由
        if trace is not None:
            trace.update(resolved=resolved, t=t, p=p, parsed=parsed)
        out = _mem_gate(resolved, source)
        _sim_tail(entry, resolved, question, t0, t, out, source, resolver)
        return out
    if re.search(r"审计一下|跑个审计|做次审计|^audit\b|^run an audit\b", resolved, re.I):
        import audit                                        # 禁模块级 import（环引守卫——audit 模块级 import 本模块）
        t, p, x, parsed = "audit", {}, 0, True              # P10 问题面：单实现共用 run_audit
        if trace is not None:
            trace.update(resolved=resolved, t=t, p=p, parsed=parsed)
        try:
            _txt, _f = audit.audit_report(Vlt(cfg), _TODAY(), cfg)
            out = _txt
        except (VltAuthError, VltBadResponse, urllib.error.URLError, OSError, ValueError) as e:
            out = f"账本暂不可达或应答损坏（{e}）——审计顺延，可稍后重试（不编造红线）。"
        _sim_tail(entry, resolved, question, t0, t, out, source, resolver)
        return out
    _mk = _metric_hook(resolved)                           # 度量类确定性分发（零重训）
    if _mk is None:                                        # FIRE/sim 钩子 + D7 拦截（模拟态优先）
        import sim
        try:
            if re.search(r"什么时候能退休|多久能退休|多少年能退休|FIRE|财务自由|退休金够不够|when can i retire|financial independence", resolved, re.I):
                sim.reset(sim.ledger_params(Vlt(cfg), _TODAY()))
                _prof = memory.get_profile()               # P9：档案覆写会话默认（首问重置后套用）
                if "wr" in _prof:
                    sim.STATE.wr = _prof["wr"]
                if "real_return" in _prof:
                    sim.STATE.r = _prof["real_return"]
                t, p, x, parsed = "sim", {}, 0, True
                if trace is not None:
                    trace.update(resolved=resolved, t=t, p=p, parsed=parsed)
                out = sim.narrate(sim.STATE.effective_params(), wr=sim.STATE.wr, r=sim.STATE.r)
                if _prof:
                    out += "\n" + _profile_citation(_prof)  # 假设引用行（验收：答案正确引用假设）
                _sim_tail(entry, resolved, question, t0, t, out, source, resolver)
                return out
            if re.search(r"钱会不会花完|花得完|取崩|跑得完跑不完|够花.*年", resolved):
                _params = sim.ledger_params(Vlt(cfg), _TODAY())
                t, p, x, parsed = "sim", {}, 0, True
                if trace is not None:
                    trace.update(resolved=resolved, t=t, p=p, parsed=parsed)
                out = sim.depletion_reply(_params)
                _sim_tail(entry, resolved, question, t0, t, out, source, resolver)
                return out
            if sim.STATE is not None and sim.STATE.ttl > 0:
                note, hit = sim.STATE.mutate(resolved)
                if hit:
                    ep = sim.STATE.effective_params()
                    t, p, x, parsed = "sim", {}, 0, True
                    if trace is not None:
                        trace.update(resolved=resolved, t=t, p=p, parsed=parsed)
                    out = (f"（{note}；提款率 {sim.STATE.wr * 100:.1f}%、实际年化 {sim.STATE.r * 100:.1f}%）\n"
                           + sim.narrate(ep, wr=sim.STATE.wr, r=sim.STATE.r))
                    _p = memory.get_profile()               # mod3 UI 回显：what-if 偏离档案时提示未保存
                    if any(k in _p for k in ("wr", "real_return")) and (
                            abs(sim.STATE.wr - _p.get("wr", sim.STATE.wr)) > 1e-9
                            or abs(sim.STATE.r - _p.get("real_return", sim.STATE.r)) > 1e-9):
                        out += "\n（当前为会话内假设，未保存——说「就这样记住」可存入档案）"
                    _sim_tail(entry, resolved, question, t0, t, out, source, resolver)
                    return out
                if sim.STATE.ttl <= 0:
                    sim.STATE = None
        except (VltAuthError, VltBadResponse, urllib.error.URLError, OSError, ValueError) as e:
            out = f"账本暂不可达或应答损坏（{e}）——无法取数算模拟（不编造红线），可稍后重试。"
            _sim_tail(entry, resolved, question, t0, "sim", out, source, resolver)
            return out
    if _mk is not None:
        kind, p = _mk
        t, x, parsed = "tx", 0, True
        if trace is not None:
            trace.update(resolved=resolved, t=t, p=p, parsed=parsed)
        try:
            out = branch_metrics(kind, p, cfg)
        except VltAuthError as e:
            out = f"账本认证失败：{e}"
        except VltBadResponse as e:
            out = f"账本应答异常（{e}）——本次未编造数据，可稍后重试。"
        except (urllib.error.URLError, OSError, ValueError) as e:
            out = f"服务暂不可达或应答损坏（{e}）——该分支不回落本地生成（不编造红线）。"
        resolver.update(resolved, t, p)
        telemetry.record(entry, question, t, (time.monotonic() - t0) * 1000,
                         answer_len=len(out), source=source)
        return out
    t, p, x, parsed = call_router(resolved)
    if not parsed:
        t, p, x = "c", {}, 0                               # 分支标签记 c（评测分支门口径）；处置见下——不上云（#14）
    if trace is not None:                                  # 观测缝（镜像 branch_l stats 先例；eval_agent 用）
        trace.update(resolved=resolved, t=t, p=p, parsed=parsed)
    if not parsed:
        # #14（定位面板裁定 fail-safe local）：路由输出不可解析 → 本地拒答，绝不静默上云
        out = "路由输出不可解析——为稳妥起见本次未上云、未编造数据；请换个问法再试。"
        resolver.update(resolved, t, p)
        telemetry.record(entry, question, t, (time.monotonic() - t0) * 1000,
                         answer_len=len(out), source=source)
        return out
    try:
        if t == "l":
            out = branch_l(resolved, cfg, gen)
        elif t == "tx":
            out = branch_tx(p, cfg)
        elif t == "pf":
            out = branch_pf(cfg)
        elif t == "mkt":
            out = branch_mkt(p, cfg)
        else:
            _ctx = _ctx_for(cfg)
            if trace is not None:
                trace["data_context"] = _ctx
            out, _cloud_tokens = send_to_relay(resolved, x, cfg, data_context=_ctx)
    except VltAuthError as e:
        out = f"账本认证失败：{e}"
    except VltBadResponse as e:
        out = f"账本应答异常（{e}）——本次未编造数据，可稍后重试。"
    except (urllib.error.URLError, OSError, ValueError) as e:   # ValueError 含 200-非 JSON（#11/#12）
        out = f"服务暂不可达或应答损坏（{e}）——该分支不回落本地生成（不编造红线）。"
    if t in ("l", "c"):
        if trace is not None:                              # pre-gate 捕获（编造率/兜底率分列的判定面）
            trace["raw_out"] = out
        out = verify.gate(resolved, out, extra=_ctx if t == "c" else None)   # #3 对账；c 腿允许集含上下文数字∪舍入锚（P8）
    resolver.update(resolved, t, p)
    telemetry.record(entry, question, t, (time.monotonic() - t0) * 1000,
                     decode_tokens=gen.get("eval_count") if t == "l" else None,
                     answer_len=len(out), source=source,   # F1：source 经 handle 透传（eval=synthetic）
                     answer=out, cloud_tokens=_cloud_tokens if t == "c" else None)  # #8/#9
    return out


def _speak_out(out, voice):
    """长输出只念前 3 行（口语 UX）；全文上屏。优先流式（首句快出），回退整句。"""
    lines = out.splitlines()
    spoken = " ".join(lines[:3]) + ("" if len(lines) <= 3 else "。详情见屏幕")
    try:
        getattr(voice, "speak_stream", voice.speak)(spoken)
    except Exception as e:
        print(f"（TTS 失败：{e}）", file=sys.stderr)


def _maybe_speak(out, cfg):
    """speak_answers 胶水（插拔契约的输出腿）：任何输入来源的回答都播报；
    voice 缺席静默跳过——纯文字用户的输出腿允许不存在。"""
    if not cfg.get("speak_answers"):
        return
    try:
        import voice
    except ImportError:
        return
    try:
        voice._require()
    except voice.VoiceUnavailable:
        return
    _speak_out(out, voice)


def _voice_mode(question, resolver, cfg):
    """P4 语音入口：--voice [问题]。无参=全语音轮（听→答→播），带参=只播答。"""
    try:
        import voice
        voice._require()
    except ImportError:
        sys.exit("voice.py 缺失——重装应用包（bash install.sh）")
    except voice.VoiceUnavailable as e:
        sys.exit(f"语音未安装：{e}")
    try:
        voice.warm_fillers()                             # #2：filler 预渲染（一次性）
    except Exception as e:
        print(f"（filler 预渲染跳过：{e}）", file=sys.stderr)
    if question:
        fp = voice.play_filler()
        try:
            try:
                out = handle(question, resolver, cfg, entry="voice")
            except (RouterUnavailable, SystemExit) as e:
                print(f"{e}\n", file=sys.stderr)
                _speak_out(str(e), voice)
                return
            print(out)
            _speak_out(out, voice)
        finally:
            voice.stop_filler(fp)                          # 任意异常都抢停（与循环路径一致）
        return
    print("firela-pa PC 语音模式（Enter=说话 / 直接打字=文字问 / exit 退出）")
    while True:
        try:
            typed = input("你> ").strip()
        except EOFError:
            break
        if typed == "exit":
            break
        try:
            q = typed if typed else voice.listen()
            if not q:
                print("（没听清，再试一次）")
                continue
            print(f"🗣 {q}")
            fp = voice.play_filler()                     # #2：问句落定即应声，路由并行
            try:
                out = handle(q, resolver, cfg, entry="voice")
            except (RouterUnavailable, SystemExit) as e:
                out = str(e)
            finally:
                voice.stop_filler(fp)
            print(out, "\n")
            _speak_out(out, voice)
        except (voice.VoiceUnavailable, RuntimeError) as e:
            print(f"语音链异常：{e}\n")


def main():
    argv = sys.argv[1:]
    if "--template" in argv:
        print(CONFIG_TEMPLATE.format(path=CONFIG_PATH))
        return
    voice_on = "--voice" in argv
    argv = [a for a in argv if a != "--voice"]
    serve_on = "--serve" in argv
    if serve_on:
        rest = [a for a in argv if a != "--serve"]
        port = int(rest[0]) if rest and rest[0].isdigit() else 8765
        argv = []
    if not CONFIG_PATH.exists() and not os.environ.get("VLT_ACCESS_TOKEN"):
        print(f"未找到配置。请创建 {CONFIG_PATH}（模板：\n{CONFIG_TEMPLATE.format(path=CONFIG_PATH)}）")
        sys.exit(1)
    cfg = load_config()
    resolver = SessionResolver()
    if serve_on:
        from server import serve
        serve(cfg, port)
        return
    if voice_on:
        _voice_mode(argv[0] if argv else None, resolver, cfg)
        return
    if argv:
        try:
            out = handle(argv[0], resolver, cfg, entry="cli")
            print(out)
            _maybe_speak(out, cfg)
        except (RouterUnavailable, SystemExit) as e:
            print(f"{e}\n", file=sys.stderr)
            sys.exit(1)
        return
    import audit                                           # P10 启动补审：仅 REPL（argv/serve/voice 已 return——单发 CLI 管道零污染）
    audit.startup_audit(cfg)
    print("firela-pa PC v0（exit/Ctrl-D 退出；--voice 语音；--serve 开 web）")
    while True:
        try:
            q = input("你> ").strip()
        except EOFError:
            break
        if not q:
            continue
        try:
            out = handle(q, resolver, cfg)
            print(out, "\n")
            _maybe_speak(out, cfg)
        except (RouterUnavailable, SystemExit) as e:
            print(f"{e}\n")


if __name__ == "__main__":
    main()
