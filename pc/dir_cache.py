"""dir 商家库缓存——vlt PayeeProfile 目录 dump 的本地快照（P2 件B，2026-09-24）。

设计（对抗审查后定稿）：
- **同步-缓存制**：vlt 为 canonical 源（GET /api/v1/bean/payee-profiles，#1504），
  本地惰性 TTL 检查 + serve-stale（照 orchestrator._CTX_CACHE 先例，无常驻线程）；
  三级回落：新鲜缓存 → 陈旧缓存 → 内嵌空基线（= 仅本地词表，行为等同旧版）。
- **三层合并词典**（命中面 = 旧版超集，金标现役路径全保留）：
  层1 本地口语词（narration 子串，键不动）+ 层2 dir 别名（canonical+aliases 按枚举分组）
  + 层3 英文 account 段词（"Transport"⊂"Transportation" 类现役巧合显式化）。
- **规范化**：vlt 按原文存储（ADR-0060 #1309——trim 只在其服务端匹配层），本模块在
  缓存构建时对 canonical/aliases 逐项 strip+casefold；匹配面对 narration/account 同 casefold。
- **外卖特判**：枚举无 DELIVERY 值、subCategory='delivery' 桶被快递公司污染（20 条中 15 条），
  故不落枚举——白名单（category=OTHER AND subCategory=delivery AND canonical∈四家外卖平台）。
- 热路径零网络：`_metric_hook` 不用本模块（只用 orchestrator.CAT_SYNONYMS 原表——防类别槽污染）。
"""

import json
import os
import sys
import time
from pathlib import Path

DIR_CACHE_PATH = os.environ.get("FIRELA_DIR_CACHE",
                        str(Path.home() / ".firela-pa" / "dir-cache.json"))
_TTL = 24 * 3600

# ---- 层1/层3 判定表：口语词 → (枚举桶列表, 英文 account 段词) -------------------
# 枚举桶仅作 dir 分组键与文案；过滤命中面 = account 段词（现役路径）+ 口语词。
_REST = ("RESTAURANT", "FAST_FOOD", "CAFE", "BAR")
_TRAFFIC = ("TAXI", "RIDE_SHARING", "PUBLIC_TRANSPORT")
_MARKET = ("SUPERMARKET",)
LOCAL_CAT_WORDS = {
    "外卖": ((), ("Delivery",)),          # 不落枚举（无 DELIVERY；dir 白名单特判，见下）
    "餐饮": (_REST, ("Dining",)),
    "吃饭": (_REST, ("Dining",)),
    "聚餐": (_REST, ("Dining",)),
    "交通": (_TRAFFIC, ("Transport",)),
    "交通费": (_TRAFFIC, ("Transport",)),
    "话费": (("TELECOM",), ("Phone",)),
    "咖啡": (("CAFE",), ("Coffee",)),
    "超市购物": (_MARKET, ("Groceries",)),
    "超市采购": (_MARKET, ("Groceries",)),
    "超市": (_MARKET, ("Groceries",)),
    "买菜": (_MARKET, ("Groceries",)),
}
# 外卖平台 canonical 白名单（排除顺丰等快递公司对 subCategory=delivery 桶的污染）
WAIMAI_WHITELIST = {"mt", "meituan", "meituan-waimai", "elm", "eleme"}

_state = {"ts": 0.0, "profiles": []}     # 进程内缓存（serve-stale）


def _normalize(entry):
    """strip+casefold（vlt 原文存储 → 客户端规范化，ADR-0060 #1309 客户端义务）。"""
    return str(entry).strip().casefold()


def refresh(cfg, force=False):
    """拉取 dir dump 并落盘（0600）。失败抛异常由调用方按 serve-stale 处理。"""
    from orchestrator import Vlt            # 惰性导入（环引守卫：orchestrator 亦懒加载本模块）
    body = Vlt(cfg)._call_flat("bean/payee-profiles")
    profiles = body.get("profiles", []) if isinstance(body, dict) else []
    norm = [{"canonical": _normalize(p.get("canonical", "")),
             "aliases": [_normalize(a) for a in p.get("aliases", []) if str(a).strip()],
             "category": p.get("category", "OTHER"),
             "subcategory": str(p.get("subCategory") or "").strip()}
            for p in profiles if p.get("canonical")]
    os.makedirs(os.path.dirname(DIR_CACHE_PATH), exist_ok=True)
    tmp = DIR_CACHE_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"fetched_at": time.time(), "profiles": norm}, f, ensure_ascii=False)
    os.replace(tmp, DIR_CACHE_PATH)
    os.chmod(DIR_CACHE_PATH, 0o600)
    _state["ts"], _state["profiles"] = time.time(), norm
    return norm


def profiles(cfg):
    """惰性 TTL + serve-stale + 空基线回落（永不抛——词典属尽力而为面）。"""
    now = time.time()
    if _state["profiles"] and now - _state["ts"] < _TTL:
        return _state["profiles"]
    if not _state["profiles"]:
        try:
            with open(DIR_CACHE_PATH, encoding="utf-8") as f:
                blob = json.load(f)
            _state["ts"], _state["profiles"] = blob.get("fetched_at", 0), blob.get("profiles", [])
        except (OSError, ValueError):
            pass
    if _state["profiles"] and now - _state["ts"] < _TTL:
        return _state["profiles"]
    try:
        return refresh(cfg)
    except Exception as e:                  # noqa: BLE001 — serve-stale：缓存/基线继续服务
        print(f"[dir_cache] 刷新失败（沿用过期缓存/基线）：{e}", file=sys.stderr)
        return _state["profiles"]           # 可能为空 = 仅本地词表（旧版行为）


class Matcher:
    """三层合并匹配器：patterns(口语词) → (narration 模式表, account 模式表)。
    模式已 casefold；调用侧对 narration/account 同 casefold 后子串匹配。"""

    def __init__(self, dir_profiles):
        by_enum = {}
        for p in dir_profiles:
            p = {"canonical": _normalize(p["canonical"]),                   # 入口双保险：
                 "aliases": [_normalize(a) for a in p["aliases"]],          # refresh 已规范化，注入面再保一次
                 "category": p["category"], "subcategory": str(p.get("subcategory") or "").strip()}
            by_enum.setdefault(p["category"], []).append(p)
        self._by_enum = by_enum
        self._cache = {}

    def _aliases(self, enums):
        out = []
        for e in enums:
            for p in self._by_enum.get(e, ()):
                out.append(p["canonical"])
                out.extend(p["aliases"])
        return out

    def patterns(self, cat):
        """旧版超集：narration ⊇ {口语词}, account ⊇ {口语词, 英文段}；层2 别名加两侧。"""
        if cat in self._cache:
            return self._cache[cat]
        word = cat.casefold()
        spec = LOCAL_CAT_WORDS.get(cat)
        if spec is None:                    # 未知词（路由 p 槽原样）→ 旧版行为逐字保留
            from orchestrator import CAT_SYNONYMS
            syn = CAT_SYNONYMS.get(cat, "")
            narr, acct = [word], [word] + ([syn.casefold()] if syn else [])
        else:
            enums, segs = spec
            if not enums:                   # 外卖：白名单别名（无枚举桶）
                aliases = []
                for p in self._by_enum.get("OTHER", ()):
                    if p["subcategory"] == "delivery" and p["canonical"] in WAIMAI_WHITELIST:
                        aliases.append(p["canonical"])
                        aliases.extend(p["aliases"])
                narr = [word] + aliases
                acct = [word] + [s.casefold() for s in segs]
            else:
                aliases = self._aliases(enums)
                narr = [word] + aliases
                acct = [word] + [s.casefold() for s in segs] + aliases
        self._cache[cat] = (narr, acct)
        return narr, acct


_matcher = None


def matcher(cfg):
    """进程内单例（profiles 变更随 TTL 刷新重建）。"""
    global _matcher
    if _matcher is None:
        _matcher = Matcher(profiles(cfg))
    return _matcher
