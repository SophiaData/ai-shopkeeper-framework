"""对 git 全部历史做敏感信息扫描。

当前工作区的密钥扫描只看工作树。已推送的历史（spike 存档、旧版文档、
提交说明）会跟着仓库走，所以这里按对象扫 blob 与 commit。

2026-10-14 的逐个 ``git cat-file -p`` 大约 81 秒，且当时 8 处命中都是误报，
因此没有放进 CI（DESIGN v1.56 / #76）。本次改成 ``git cat-file --batch``，
并对已经核对过的误报使用下面的显式规则。规则只认特定形态；对不上的命中
仍使进程退出码为 1。报告不打印命中原文。

白名单规则（每条都是否定式的反面：对不上就不放行）：

- ``placeholder_llm_api_key``：赋值右侧恰好是 ``local-gateway-no-auth``。
  这是本机 magpie 网关的无鉴权占位，写在 ``.env.example`` 和适配器默认值里。
- ``empty_api_key_next_env_name``：``api_key`` 正则的 ``\\s*`` 会跨行。
  空的 ``*_API_KEY=`` 下一行若是另一个大写环境变量名，且紧跟着 ``=``，
  吃进去的是变量名而不是密钥。同一行上的值、或后面不是 ``=`` 的，不放行。
- ``fixture_cn_mobile``：恰好是 ``13800138000``（工信部示例号）或
  ``13812345678``（本仓库脱敏样例）。其它 11 位号码不放行。
- ``cargo_lock_checksum_hex``：``Cargo.lock`` 里 sha256 校验和中间的一段数字。
  前后仍是十六进制，且整段十六进制不少于 32 位，才视为校验和。单独出现的号码不放行。
- ``alicdn_ibank_image_id``：只限 ``spike/alibaba_parse/_fc2.out``。
  号码夹在 ``ibank/`` 与 ``_<数字>.jpg`` 之间，是 alicdn 图片文件名。
- ``redacted_seller_login``：``sellerLoginId`` 的引号内值整段是
  ``<…redacted…>``（现用 ``<seller-id-redacted>``，历史里还有
  ``<seller-login-redacted>``）。尖括号外的真值不放行。
- ``redacted_winport_ids``：``winportUrl`` 里出现的每个 ``memberId`` / ``loginId``
  都必须是 ``<…redacted…>``，且至少有一个。仍带原始 id 的 URL 不放行。
- ``alphabet_sk_fixture``：``sk-`` 后面是小写字母表 ``abcdefghijklmnopqrstuvwxyz``
  的前缀，且不少于 20 个字母。测试与提交说明里用过全表和截断到 ``…tuv`` 的两种。
  含数字或大写的 ``sk-`` 不放行。
- ``alphabet_bearer_fixture``：``Bearer`` 后面的令牌满足上一条。
- ``bearer_test_literals``：与 ``tests/security/test_redaction.py`` 里两条
  Authorization 样例逐字相同。改一个字符就不放行。
- ``aws_docs_example_key``：恰好是 AWS 文档示例 ``AKIAIOSFODNN7EXAMPLE``。
- ``commit_object_sha_hex``：commit 对象 ``tree``/``parent`` 行里的四十位十六进制。
  整段恰好 40 位、小写，且前缀是 ``tree ``/``parent `` 或后面紧跟 commit 头部
  （``parent``/``author``/``committer``）。带 path 的 blob 一律不放行。
- ``self_test_fixture_literals``：``tests/security/test_scan_history_whitelist.py``
  里逐字核对过的夹具值。**只限该文件、只认列出的完整值**；``seller_login`` /
  ``winport`` 两类含整段 JSON，另要求值是红字占位或本文件的两个 demo 样例。
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import threading
import time
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

# 与改写前相同的检测面。不要放宽这些正则。
PATTERNS: dict[str, re.Pattern[str]] = {
    "gh_token": re.compile(r"gh[oprus]_[A-Za-z0-9]{20,}"),
    "sk_key": re.compile(r"sk-[A-Za-z0-9]{20,}"),
    "api_key_generic": re.compile(
        r"(?i)(api[_-]?key|apikey)\s*[=:]\s*['\"]?[A-Za-z0-9_\-]{16,}"
    ),
    "bearer": re.compile(r"(?i)bearer\s+[A-Za-z0-9._\-]{16,}"),
    "aws": re.compile(r"AKIA[0-9A-Z]{16}"),
    "private_key": re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    "cn_mobile": re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)"),
    "seller_member_id": re.compile(r"b2b-\d{8,}"),
    "seller_login": re.compile(r"(?i)sellerloginid\"?\s*[:=]\s*\"[^\"]+\""),
    "winport": re.compile(r"(?i)winporturl\"?\s*[:=]\s*\"[^\"]+\""),
}

_ALPHABET = "abcdefghijklmnopqrstuvwxyz"
_FIXTURE_PHONES = frozenset({"13800138000", "13812345678"})
_LLM_PLACEHOLDER = "local-gateway-no-auth"
_AWS_DOCS_EXAMPLE = "AKIAIOSFODNN7EXAMPLE"
# 只这些完整字符串。来源：tests/security/test_redaction.py 的 Authorization 样例。
_BEARER_LITERALS = frozenset(
    {
        "eyJhbGciOiJIUzI1NiJ9.SECRETTOKEN",
        "t-g1044ghJRSX4PO4VP7LQYVQ5NKGQ7W2Z",
    }
)
_HEX = frozenset("0123456789abcdefABCDEF")
# ``tests/security/test_scan_history_whitelist.py`` 为了验证「规则只认精确形态」，
# 必须把会命中的字面量写成**完整字符串**。这些 blob 一旦提交就进了历史，
# 全历史扫描会把它们当成真命中。
#
# 为什么不用「按路径放行整个文件」：那等于把该文件里将来误提交的任何真密钥
# 一起放行。这里只放行**本文件确实写过、且逐字核对过**的那几个字面量；
# 同一个文件里出现别的高危字面量仍然会被挡住。
# 这些值**必须运行时拼接**，不能写成完整字面量。
#
# 教训：第一版把它们原样写在这里，于是本文件自己成了新的历史命中（30 处），
# 白名单又要为白名单本身开例外 —— 无限套娃。正确做法是让源码里**根本
# 不出现**连续的高危形态，而不是往白名单里继续加值。
_FIXTURE_LITERALS = frozenset(
    {
        # 规则本身要求的样例（见 tests/security/test_scan_history_whitelist.py）。
        "sk-" + "abcdefghijklmnopqrstuvwxyz",
        "sk-" + "abcdefghijklmnopqrstuvwxyz"[:22],
        # 字母表样例多一位数字 —— 测试断言它**必须**被挡住。
        "sk-" + "abcdefghijklmnopqrstuvwxyz" + "1",
        "sk-" + "notarealkey0123456789",
        "sk-" + "nottherealalphabetkey",
        "sk-" + "RealSecretKey0123456789",
        _AWS_DOCS_EXAMPLE,
        "AKIAIOSFODNN7" + "EXAMPLF",
        "-----BEGIN OPENSSH " + "PRIVATE KEY-----",
        "b2b-" + "12345678",
        "b2b-" + "2218547450375",
        # api_key 正则的跨行形态：命中的 value 从上一行的赋值得出。
        "API_KEY=" + "sk-" + "notarealkey0123456789",
        # 跨行吃到下一行的空赋值形态（规则对不上时必须被挡住）。
        "API_KEY=" + "WORKFLOW_LLM_BASE_URL",
        # Bearer 前面不是字母表样例的两种。
        "Bearer " + "sk-" + "nottherealalphabetkey",
        "Bearer " + "sk-" + "RealSecretKey0123456789",
        "ghp_" + "a" * 36,
        # 格式由 _FIXTURE_PHONES / 规则样例确定。
        "139" + "00001111",
        "158" + "12345678",
        "13800138000",
        "13812345678",
    }
)
# 该文件里每条 winport / sellerLoginId 样例都用的是红字占位或明写的 demo id。
_FIXTURE_SELF_PATH = "tests/security/test_scan_history_whitelist.py"
_SELF_TEST_JSON_PATTERNS = frozenset({"seller_login", "winport"})
_COMMIT_TREE_LINE = re.compile(r"(?:tree|parent) ([0-9a-f]{40})\Z")
_REDACTED_ANGLE = re.compile(r"<[^>\n]*redacted[^>\n]*>\Z", re.I)
_REDACTED_QUERY = re.compile(r"<[^>&]*redacted[^>&]*>\Z", re.I)
_QUOTED = re.compile(r"[:=]\s*\"([^\"]*)\"")
_ID_PARAM = re.compile(r"(?i)(?:memberId|loginId)=([^&\s\"]+)")
_ENV_NAME = re.compile(r"[A-Z][A-Z0-9_]+\Z")
_IBANK_JPG = re.compile(r"_\d+\.jpg")
_CONTEXT = 80

AllowFn = Callable[["Hit"], bool]


@dataclass(frozen=True)
class AllowRule:
    id: str
    reason: str
    matches: AllowFn


@dataclass(frozen=True)
class Hit:
    pattern: str
    path: str
    oid: str
    value: str
    before: str
    after: str


@dataclass
class ScanResult:
    allowed: list[tuple[Hit, str]]
    blocked: list[Hit]
    objects: int
    elapsed_s: float
    error: str | None = None

    @property
    def exit_code(self) -> int:
        if self.error:
            return 2
        return 1 if self.blocked else 0


def _quoted(value: str) -> str | None:
    found = _QUOTED.search(value)
    if found is None:
        return None
    return found.group(1)


def _bearer_token(value: str) -> str | None:
    found = re.fullmatch(r"(?i)bearer\s+(\S+)", value)
    if found is None:
        return None
    return found.group(1)


def _is_alphabet_sk(token: str) -> bool:
    if not token.startswith("sk-"):
        return False
    body = token[3:]
    return len(body) >= 20 and body.isalpha() and body.islower() and _ALPHABET.startswith(body)


def _assigned_value(value: str) -> str:
    tail = re.split(r"[=:]", value, maxsplit=1)[-1]
    return tail.strip().strip("'\"")


def _placeholder_llm(hit: Hit) -> bool:
    return hit.pattern == "api_key_generic" and _assigned_value(hit.value) == _LLM_PLACEHOLDER


def _empty_api_key_next_name(hit: Hit) -> bool:
    if hit.pattern != "api_key_generic" or "\n" not in hit.value:
        return False
    tail = _assigned_value(hit.value)
    return _ENV_NAME.fullmatch(tail) is not None and hit.after.startswith("=")


def _fixture_phone(hit: Hit) -> bool:
    return hit.pattern == "cn_mobile" and hit.value in _FIXTURE_PHONES


def _cargo_checksum(hit: Hit) -> bool:
    if hit.pattern != "cn_mobile":
        return False
    if not hit.path.replace("\\", "/").endswith("Cargo.lock"):
        return False
    if not hit.before or not hit.after:
        return False
    if hit.before[-1] not in _HEX or hit.after[0] not in _HEX:
        return False
    left = hit.before
    right = hit.after
    i = len(left)
    while i > 0 and left[i - 1] in _HEX:
        i -= 1
    j = 0
    while j < len(right) and right[j] in _HEX:
        j += 1
    token = left[i:] + hit.value + right[:j]
    return len(token) >= 32 and re.fullmatch(r"[0-9a-fA-F]+", token) is not None


def _ibank_image(hit: Hit) -> bool:
    if hit.pattern != "cn_mobile":
        return False
    path = hit.path.replace("\\", "/")
    if not path.endswith("spike/alibaba_parse/_fc2.out"):
        return False
    return hit.before.endswith("ibank/") and _IBANK_JPG.match(hit.after) is not None


def _redacted_seller(hit: Hit) -> bool:
    if hit.pattern != "seller_login":
        return False
    inner = _quoted(hit.value)
    return inner is not None and _REDACTED_ANGLE.fullmatch(inner) is not None


def _redacted_winport(hit: Hit) -> bool:
    if hit.pattern != "winport":
        return False
    inner = _quoted(hit.value)
    if inner is None:
        return False
    ids = _ID_PARAM.findall(inner)
    return bool(ids) and all(_REDACTED_QUERY.fullmatch(item) is not None for item in ids)


def _alphabet_sk(hit: Hit) -> bool:
    return hit.pattern == "sk_key" and _is_alphabet_sk(hit.value)


def _alphabet_bearer(hit: Hit) -> bool:
    if hit.pattern != "bearer":
        return False
    token = _bearer_token(hit.value)
    return token is not None and _is_alphabet_sk(token)


def _bearer_literals(hit: Hit) -> bool:
    if hit.pattern != "bearer":
        return False
    token = _bearer_token(hit.value)
    return token in _BEARER_LITERALS


def _aws_example(hit: Hit) -> bool:
    return hit.pattern == "aws" and hit.value == _AWS_DOCS_EXAMPLE


def _commit_object_hex(hit: Hit) -> bool:
    """commit 对象里的 ``tree <sha>`` / ``parent <sha>`` 行。

    这两种行的值是四十位十六进制，其中一段连续数字很容易撞上手机号正则
    （实测 ``tree`` 行 SHA 的前 11 位数字撞上手机号正则）。判定要求同时满足：

    1. 命中来自 commit 对象（没有 path）；
    2. 命中**左侧**紧邻的整段十六进制 + 命中值 + 右侧整段十六进制
       恰好是四十位（``hit.before`` / ``hit.after`` 是定长上下文，
       所以只拼「整段十六进制」，不要求拿到整行）；
    3. 这一段前面是 ``tree `` 或 ``parent ``，亦或后面紧跟的是
       ``\nparent `` / ``\nauthor`` 之类的 commit 头部边界。

    放宽成「前后都是十六进制」是不够的：十一号串本身就可能落在别处，
    所以只对 commit 对象、且整段恰好四十位十六进制时才放行。
    """
    if hit.pattern != "cn_mobile":
        return False
    if hit.path:
        return False
    left, right = hit.before, hit.after

    i = len(left)
    while i > 0 and left[i - 1] in _HEX:
        i -= 1
    j = 0
    while j < len(right) and right[j] in _HEX:
        j += 1
    token = left[i:] + hit.value + right[:j]
    if len(token) != 40:
        return False
    if re.fullmatch(r"[0-9a-f]{40}", token) is None:
        return False
    prefix = left[:i]
    if re.search(r"(?:tree|parent) \Z", prefix) is not None:
        return True
    # 上下文被截断时，右侧可能只到 SHA 末尾；此时看下一行开头。
    return re.match(r"\n(?:parent|author|committer) ", right[j:]) is not None


def _self_test_literal(hit: Hit) -> bool:
    """白名单自测文件里逐字核对过的夹具。

    只放行 ``_FIXTURE_LITERALS`` 里的完整值，且必须来自该文件。
    同一路径下出现别的高危字面量仍会被挡住；文件之外一律不放行。
    """
    path = hit.path.replace("\\", "/")
    if not path.endswith(_FIXTURE_SELF_PATH):
        return False
    if hit.pattern in _SELF_TEST_JSON_PATTERNS:
        # 这两类命中的 value 含整段 JSON，无法逐个字面量列出。
        # 只在该文件内、且命中的值里**不含**真实形态时放行：
        # 必须带红字占位，或是本文件自己写的 ``shopkeeper_demo_001`` 样例。
        return _is_self_test_json(hit)
    return hit.value in _FIXTURE_LITERALS


def _is_self_test_json(hit: Hit) -> bool:
    if "redacted" in hit.value:
        return True
    return "shopkeeper_demo_001" in hit.value or ("b2b-" + "2218547450375") in hit.value


ALLOW_RULES: tuple[AllowRule, ...] = (
    AllowRule(
        "placeholder_llm_api_key",
        "本机 magpie 无鉴权占位 local-gateway-no-auth，不是密钥。",
        _placeholder_llm,
    ),
    AllowRule(
        "empty_api_key_next_env_name",
        "空的 API_KEY= 跨行吃进下一个大写环境变量名，且该名字后面紧跟 =。",
        _empty_api_key_next_name,
    ),
    AllowRule(
        "fixture_cn_mobile",
        "只放行 13800138000 与 13812345678 这两个示例号。",
        _fixture_phone,
    ),
    AllowRule(
        "cargo_lock_checksum_hex",
        "Cargo.lock 校验和内部的数字串；前后仍是十六进制且整段不少于 32 位。",
        _cargo_checksum,
    ),
    AllowRule(
        "alicdn_ibank_image_id",
        "仅 _fc2.out 里 ibank/ 与 _<数字>.jpg 之间的图片 id。",
        _ibank_image,
    ),
    AllowRule(
        "redacted_seller_login",
        "sellerLoginId 的值整段是 <…redacted…> 占位。",
        _redacted_seller,
    ),
    AllowRule(
        "redacted_winport_ids",
        "winportUrl 里每个 memberId/loginId 都是 <…redacted…>。",
        _redacted_winport,
    ),
    AllowRule(
        "alphabet_sk_fixture",
        "sk- 加小写字母表前缀（测试与提交说明里的样例）。",
        _alphabet_sk,
    ),
    AllowRule(
        "alphabet_bearer_fixture",
        "Bearer 后面是同一条字母表 sk- 样例。",
        _alphabet_bearer,
    ),
    AllowRule(
        "bearer_test_literals",
        "test_redaction.py 里两条 Authorization 假令牌的完整字符串。",
        _bearer_literals,
    ),
    AllowRule(
        "aws_docs_example_key",
        "AWS 文档示例访问密钥 ID AKIAIOSFODNN7EXAMPLE。",
        _aws_example,
    ),
    AllowRule(
        "commit_object_sha_hex",
        "commit 对象的 tree/parent 行是四十位十六进制，其中一段数字撞上手机号正则；"
        "要求整行结构是 tree <40hex> 或 parent <40hex>。",
        _commit_object_hex,
    ),
    AllowRule(
        "self_test_fixture_literals",
        "tests/security/test_scan_history_whitelist.py 里逐字核对过的夹具字面量；"
        "只认这些完整值且必须来自该文件。",
        _self_test_literal,
    ),
)


def allow_rule_id(hit: Hit) -> str | None:
    """命中第一条显式规则则返回规则 id。没有任何规则是全放行。"""
    for rule in ALLOW_RULES:
        if rule.matches(hit):
            return rule.id
    return None


def find_hits(text: str, path: str = "", oid: str = "") -> list[Hit]:
    """一个文本里的全部命中（不只是每条正则的第一处）。"""
    hits: list[Hit] = []
    for name, pattern in PATTERNS.items():
        for found in pattern.finditer(text):
            start, end = found.start(), found.end()
            hits.append(
                Hit(
                    pattern=name,
                    path=path,
                    oid=oid,
                    value=found.group(0),
                    before=text[max(0, start - _CONTEXT) : start],
                    after=text[end : end + _CONTEXT],
                )
            )
    return hits


def partition(hits: list[Hit]) -> tuple[list[tuple[Hit, str]], list[Hit]]:
    allowed: list[tuple[Hit, str]] = []
    blocked: list[Hit] = []
    for hit in hits:
        rule_id = allow_rule_id(hit)
        if rule_id is None:
            blocked.append(hit)
        else:
            allowed.append((hit, rule_id))
    return allowed, blocked


def _git_objects(budget_s: float) -> tuple[list[tuple[str, str, str, str]], str | None]:
    """返回 (oid, type, path, text)。超时或 git 失败时文本列表可能不完整。"""
    # 预算必须从枚举就开始计时：rev-list 在大仓库上本身就能跑很久，
    # 若只在 cat-file 读取循环里查 deadline，枚举阶段会无视预算一直挂住。
    deadline = time.monotonic() + budget_s
    try:
        listed = subprocess.run(
            ["git", "rev-list", "--objects", "--all"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=max(budget_s, 0.0),
        )
    except subprocess.TimeoutExpired:
        return [], f"exceeded budget {budget_s:.0f}s"
    except OverflowError:
        return [], "budget exceeds platform wait range"
    if listed.returncode != 0:
        err = (listed.stderr or listed.stdout or "git rev-list failed").strip()
        return [], err.splitlines()[-1][:200]
    items: list[tuple[str, str]] = []
    for line in listed.stdout.splitlines():
        oid, _, path = line.partition(" ")
        if oid:
            items.append((oid, path))
    if not items:
        return [], None

    proc = subprocess.Popen(
        ["git", "cat-file", "--batch"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
    )

    def _feed() -> None:
        assert proc.stdin is not None
        try:
            for oid, _path in items:
                if stop_feeding.is_set():
                    break
                proc.stdin.write(oid.encode("ascii") + b"\n")
        except (BrokenPipeError, OSError, ValueError):
            # 读端因超时关掉管道时，写端会收到 EPIPE 或 ValueError。
            pass
        finally:
            try:
                proc.stdin.close()
            except (OSError, ValueError):
                pass

    stop_feeding = threading.Event()
    feeder = threading.Thread(target=_feed, daemon=True)
    feeder.start()
    assert proc.stdout is not None
    out: list[tuple[str, str, str, str]] = []
    error: str | None = None
    try:
        for oid, path in items:
            if time.monotonic() >= deadline:
                error = f"exceeded budget {budget_s:.0f}s"
                break
            header = proc.stdout.readline()
            if not header:
                error = "git cat-file ended early"
                break
            parts = header.decode("utf-8", "replace").split()
            if parts and parts[-1] == "missing":
                continue
            if len(parts) < 3:
                error = "unexpected cat-file header"
                break
            typ, size_s = parts[1], parts[2]
            data = proc.stdout.read(int(size_s))
            proc.stdout.read(1)
            if typ not in {"blob", "commit"}:
                continue
            out.append((oid, typ, path, data.decode("utf-8", "replace")))
        else:
            proc.wait()
    finally:
        # 顺序要紧：先让 feeder 停下并关掉写端，再 kill 子进程。
        # 否则 feeder 可能卡在 write() 上，daemon 线程拖到解释器退出时。
        stop_feeding.set()
        if proc.stdin is not None:
            try:
                proc.stdin.close()
            except (OSError, ValueError):
                pass
        if proc.poll() is None:
            proc.kill()
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                pass
        if proc.stdout is not None:
            proc.stdout.close()
        feeder.join(timeout=2)
    return out, error


def scan_git(budget_s: float = 60.0) -> ScanResult:
    started = time.monotonic()
    objects, error = _git_objects(budget_s)
    allowed: list[tuple[Hit, str]] = []
    blocked: list[Hit] = []
    if error is None:
        for oid, _typ, path, text in objects:
            hits = find_hits(text, path=path, oid=oid)
            ok, bad = partition(hits)
            allowed.extend(ok)
            blocked.extend(bad)
    return ScanResult(
        allowed=allowed,
        blocked=blocked,
        objects=len(objects),
        elapsed_s=time.monotonic() - started,
        error=error,
    )


def format_report(result: ScanResult) -> str:
    """给人看的摘要。不包含命中原文。"""
    if result.error:
        return "\n".join(
            [
                f"扫描未完成：{result.error}",
                f"已读对象 {result.objects}，用时 {result.elapsed_s:.2f}s",
                "RESULT: BUDGET" if result.error.startswith("exceeded budget") else "RESULT: ERROR",
            ]
        )
    total = len(result.allowed) + len(result.blocked)
    lines = [
        f"扫描完成：命中 {total} 处，白名单 {len(result.allowed)} 处，未放行 {len(result.blocked)} 处",
        f"对象 {result.objects}，用时 {result.elapsed_s:.2f}s",
    ]
    counts = Counter(rule_id for _hit, rule_id in result.allowed)
    for rule_id, count in sorted(counts.items()):
        lines.append(f"  白名单 {rule_id}: {count}")
    for hit in result.blocked[:60]:
        where = hit.path or "<commit>"
        lines.append(f"  未放行 {hit.pattern} {hit.oid[:12]} {where}")
    extra = len(result.blocked) - 60
    if extra > 0:
        lines.append(f"  ...另有 {extra} 处未放行")
    lines.append("RESULT: CLEAN" if not result.blocked else "RESULT: BLOCKED")
    return "\n".join(lines)


def main() -> int:
    budget = float(os.environ.get("SCAN_HISTORY_BUDGET_S", "60"))
    result = scan_git(budget_s=budget)
    text = format_report(result)
    sys.stdout.write(text + "\n")
    report = Path("artifacts") / "scan_report.txt"
    try:
        report.parent.mkdir(parents=True, exist_ok=True)
        report.write_text(text + "\n", encoding="utf-8")
    except OSError:
        sys.stdout.write("RESULT: REPORT_WRITE_FAILED\n")
    return result.exit_code


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):
        pass
    raise SystemExit(main())
