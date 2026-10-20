"""合规词过滤（DESIGN §7.4：黑名单随站点配置，生成与上架前双重校验）。

词表数据：`assets/compliance_lexicon.json`（可维护；含 severity=block|warn）。
公开常识类示例词，**非法律意见**，须人工复核（见词表 disclaimer）。

**为什么用词边界而不是子串包含**（A2 spike 实测教训）：
朴素 `term in text` 会把 "Secure packaging" 命中成 `cure`——
误杀会把正常商品推进人工队列，漏放则违反 §7.5，两头都是成本。
英文类词表按词边界匹配 + 常见屈折（s/es/ing/ed）；
中文/泰文类词表无词边界概念，仍用子串匹配（`治愈率90%` 必须命中 `治愈`）。
"""

from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import asdict, dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

Severity = Literal["block", "warn"]

#: 需要词边界保护的脚本（拉丁文）；CJK / 泰文走去掉空白后的子串
_LATIN_ONLY = re.compile(r"^[A-Za-z0-9%'\-\.\s]+$")
#: 空格、全角空格、零宽字符。用来挡「食品 级」「100% 防水」这类拆开写的漏检。
_GAP = re.compile(r"[\s\u200b\u200c\u200d\ufeff]+")

_LEXICON_PATH = Path(__file__).resolve().parent / "assets" / "compliance_lexicon.json"


@dataclass(frozen=True)
class LexiconTerm:
    term: str
    severity: Severity
    category: str
    sites: tuple[str, ...]
    note: str = ""


@dataclass(frozen=True)
class TermHit:
    """单次命中：词、分级、类目、在归一化扫描串中的起止位置。"""

    term: str
    severity: Severity
    category: str
    start: int
    end: int
    field: str | None = None
    haystack_mode: str = "spaced"  # spaced | compact

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def match_forms(text: str) -> tuple[str, str]:
    """NFKC 之后的两种形态：保留单空格，以及去掉全部空白。

    拉丁词在保留空格的文本上做词边界匹配，避免把 Secure 判成 cure。
    中文、泰文和中英混排词在去空白文本上做子串匹配，使「１００％防水」
    与「100% 防水」都命中「100%防水」。
    """
    nfkc = unicodedata.normalize("NFKC", text)
    return _GAP.sub(" ", nfkc).strip(), _GAP.sub("", nfkc)


def _compile(term: str) -> re.Pattern[str]:
    """拉丁词 → 词边界正则（含屈折）；CJK/泰文 → 直接子串。

    以 e 结尾的词必须走**词干**：`cure` 的词干 `cur` + {e,es,ed,ing}
    才能覆盖 cured / curing。直接拼 `cure` + `ed` 会得到 "cureed"，
    于是 "fully cured" 漏放——这是测试当场抓出来的，不是推演出来的。
    """
    normalized = unicodedata.normalize("NFKC", term)
    if _LATIN_ONLY.match(normalized):
        if normalized.lower().endswith("e"):
            body = rf"{re.escape(normalized[:-1])}(?:e|es|ed|ing)?"
        else:
            body = rf"{re.escape(normalized)}(?:s|es|ed|ing)?"
        # **多词拉丁词要容忍连字符**：`absolutely water-proof` / `food-grade`
        # 是英文里的正常拼写（不是刻意规避），而 `re.escape("absolutely waterproof")`
        # 只认**恰好一个空格**，于是这些形态静默漏放。
        # 把词内空白换成 `[\s-]+`：既覆盖连字符，也覆盖多空格与换行折行。
        # 两处都要容忍连字符，因为是两种不同情形：
        #   1. **词之间**：`absolutely water-proof` → 空格位换 `[\s-]+`
        #   2. **词内部**：`water-proof` / `food-grade` → 每个字母间允许 `-?`
        # 只做第 1 种会漏掉第 2 种（我第一版就是这样，`absolutely water-proof` 仍不命中）。
        body = body.replace("\\ ", "[\\s-]+")
        # 词内部也允许连字符：把每个字母段拆开、段间允许一个 `-?`。
        # 直接对整串逐字母插 `-?` 会得到一个又长又难读的正则；按字母段处理即可。
        body = re.sub(
            r"[A-Za-z]+",
            lambda m: "-?".join(re.escape(ch) for ch in m.group(0)),
            body,
        )
        return re.compile(rf"(?<![A-Za-z0-9]){body}(?![A-Za-z0-9])", re.IGNORECASE)
    return re.compile(re.escape(_GAP.sub("", normalized)))


_CACHE: dict[str, re.Pattern[str]] = {}


def _pattern(term: str) -> re.Pattern[str]:
    pat = _CACHE.get(term)
    if pat is None:
        pat = _compile(term)
        _CACHE[term] = pat
    return pat


def _is_latin_term(term: str) -> bool:
    return bool(_LATIN_ONLY.match(unicodedata.normalize("NFKC", term)))


@lru_cache(maxsize=1)
def load_lexicon() -> dict[str, Any]:
    """加载词表 JSON；失败时抛错（CI/启动即暴露，不静默空表）。"""
    raw = json.loads(_LEXICON_PATH.read_text(encoding="utf-8"))
    if not isinstance(raw, dict) or "terms" not in raw:
        raise ValueError(f"compliance lexicon invalid: {_LEXICON_PATH}")
    return raw


def lexicon_disclaimer() -> str:
    return str(load_lexicon().get("disclaimer") or "")


def _all_terms() -> list[LexiconTerm]:
    data = load_lexicon()
    out: list[LexiconTerm] = []
    for row in data.get("terms") or []:
        if not isinstance(row, dict):
            continue
        term = str(row.get("term") or "").strip()
        if not term:
            continue
        sev = str(row.get("severity") or "block").lower()
        if sev not in {"block", "warn"}:
            sev = "block"
        sites = tuple(str(s).upper() for s in (row.get("sites") or ["*"]))
        out.append(
            LexiconTerm(
                term=term,
                severity=sev,  # type: ignore[arg-type]
                category=str(row.get("category") or "general"),
                sites=sites or ("*",),
                note=str(row.get("note") or ""),
            )
        )
    return out


def _scopes_for(site: str) -> set[str]:
    """解析扫描作用域。

    - 通用表 `*` 始终生效。
    - `SEA` → 别名 `TH`（DESIGN §12.1 目标站）。
    - 历史测试把语种码（EN/ZH/VI）误当 site 传入：只扫 `*`，不套用 TH 站点词。
    """
    scopes: set[str] = {"*"}
    if not site:
        return scopes
    site_u = site.upper()
    data = load_lexicon()
    lang_as_site = {str(x).upper() for x in (data.get("language_as_site") or [])}
    if site_u in lang_as_site:
        return scopes
    aliases = {str(k).upper(): str(v).upper() for k, v in (data.get("site_aliases") or {}).items()}
    resolved = aliases.get(site_u, site_u)
    scopes.add(resolved)
    if site_u != resolved:
        scopes.add(site_u)
    return scopes


def _term_in_scopes(term: LexiconTerm, scopes: set[str]) -> bool:
    term_sites = {s.upper() for s in term.sites}
    if "*" in term_sites:
        return True
    return bool(term_sites & scopes)


def _compact_span_to_spaced(spaced: str, compact_start: int, compact_end: int) -> tuple[int, int]:
    """把去空白串上的偏移映射回保留单空格串。"""
    ci = 0
    start_s: int | None = None
    end_s = 0
    for si, ch in enumerate(spaced):
        if ch.isspace():
            continue
        if ci == compact_start and start_s is None:
            start_s = si
        ci += 1
        if ci == compact_end:
            end_s = si + 1
            break
    if start_s is None:
        return 0, 0
    return start_s, end_s


def scan_term_hits(
    text: str,
    site: str = "*",
    *,
    severities: frozenset[str] | None = None,
    field: str | None = None,
) -> list[TermHit]:
    """返回结构化命中（含 severity 与位置）。默认 block+warn 都返回。"""
    if not text:
        return []
    wanted = severities or frozenset({"block", "warn"})
    spaced, compact = match_forms(text)
    scopes = _scopes_for(site)
    hits: list[TermHit] = []
    seen: set[tuple[str, str]] = set()  # (term, severity) 去重
    for entry in _all_terms():
        if entry.severity not in wanted:
            continue
        if not _term_in_scopes(entry, scopes):
            continue
        key = (entry.term, entry.severity)
        if key in seen:
            continue
        latin = _is_latin_term(entry.term)
        haystack = spaced if latin else compact
        mode = "spaced" if latin else "compact"
        m = _pattern(entry.term).search(haystack)
        if not m:
            continue
        if latin:
            start, end = m.start(), m.end()
        else:
            start, end = _compact_span_to_spaced(spaced, m.start(), m.end())
            mode = "spaced"
        hits.append(
            TermHit(
                term=entry.term,
                severity=entry.severity,
                category=entry.category,
                start=start,
                end=end,
                field=field,
                haystack_mode=mode,
            )
        )
        seen.add(key)
    return hits


def scan_terms(text: str, site: str = "*") -> list[str]:
    """返回 **block** 级命中词（兼容旧调用方；命中即阻断，DESIGN §7.5）。"""
    return [h.term for h in scan_term_hits(text, site, severities=frozenset({"block"}))]


def scan_listing_hits(body: dict[str, object], site: str = "*") -> list[TermHit]:
    """扫描 listing 文本字段，附 field 名；同一 term+severity 跨字段只保留首次。"""
    hits: list[TermHit] = []
    seen: set[tuple[str, str]] = set()
    for key, value in body.items():
        candidates: list[str] = []
        if isinstance(value, str):
            candidates.append(value)
        elif isinstance(value, list):
            candidates.extend(x for x in value if isinstance(x, str))
        field = str(key)
        for text in candidates:
            for h in scan_term_hits(text, site, field=field):
                key2 = (h.term, h.severity)
                if key2 in seen:
                    continue
                seen.add(key2)
                hits.append(h)
    return hits


def scan_listing(body: dict[str, object], site: str = "*") -> list[str]:
    """扫描 listing 的所有文本字段；只返回 block 级词（兼容旧调用方）。"""
    return [h.term for h in scan_listing_hits(body, site) if h.severity == "block"]


def hits_to_report(hits: list[TermHit]) -> dict[str, Any]:
    """给 QA / 编排器 / stylist 用的统一报告片段。"""
    blocks = [h for h in hits if h.severity == "block"]
    warns = [h for h in hits if h.severity == "warn"]
    return {
        "disclaimer": lexicon_disclaimer(),
        "blocked": bool(blocks),
        "block_hits": [h.to_dict() for h in blocks],
        "warn_hits": [h.to_dict() for h in warns],
        "block_terms": [h.term for h in blocks],
        "warn_terms": [h.term for h in warns],
    }


# ---------------------------------------------------------------------------
# 供给侧信息泄漏检查（B4 实跑抓出来的真实缺陷）
# ---------------------------------------------------------------------------
#: 这些是**供货侧**事实，出现在买家可见文案里等于自曝无货源代发。
#: 注意：DESIGN §7 的硬规则清单里没有这一条（§7 讲的是合规/时效/水印）。
#: 本检查先作为**需人工处置**的确定性信号落地，是否升格为 §7 硬规则请 owner 拍板
#: （已登记到 docs/M1-PLAN.md §0.2 待拍板）。
_SUPPLIER_LEAK_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("moq", re.compile(r"(?<![A-Za-z])moq\b", re.IGNORECASE)),
    ("minimum_order", re.compile(r"minimum\s+(?:order|quantity|purchase)", re.IGNORECASE)),
    ("wholesale", re.compile(r"(?<![A-Za-z])(?:wholesale|bulk\s+order)(?![A-Za-z])", re.IGNORECASE)),
    ("dropship", re.compile(r"(?<![A-Za-z])(?:drop\s?ship|dropshipping)(?![A-Za-z])", re.IGNORECASE)),
    ("起订", re.compile(r"起订")),
    ("批发", re.compile(r"批发")),
    ("一件代发", re.compile(r"一件代发")),
    ("cost_price", re.compile(r"(?:cost\s+price|unit\s+cost|进货价|成本价)", re.IGNORECASE)),
]


def scan_supplier_leakage(text: str) -> list[str]:
    """返回命中的供给侧标签（如 ['moq','起订']）。空列表 = 未泄漏。"""
    if not text:
        return []
    return [label for label, pat in _SUPPLIER_LEAK_PATTERNS if pat.search(text)]


def scan_listing_leakage(body: dict[str, object]) -> list[str]:
    hits: list[str] = []
    for value in body.values():
        texts: list[str] = []
        if isinstance(value, str):
            texts.append(value)
        elif isinstance(value, list):
            texts.extend(x for x in value if isinstance(x, str))
        for t in texts:
            for h in scan_supplier_leakage(t):
                if h not in hits:
                    hits.append(h)
    return hits
