"""Local operations records: no platform calls, purchasing or advertising execution."""
from __future__ import annotations

import base64
import hashlib
import binascii
import ipaddress
import socket
import sqlite3
import os
import re
from urllib.parse import urlparse, urlencode, urljoin
import io
import json
import uuid
import httpx
from bs4 import BeautifulSoup
from datetime import date, datetime, timezone
from typing import TYPE_CHECKING, Annotated, Any, Literal

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from PIL import Image, UnidentifiedImageError

from shopkeeper.adapters.alibaba_1688 import build_product_source, extract_offer_id
from shopkeeper.adapters.base import _build_http_client
from shopkeeper.security.redaction import redact_spans
from shopkeeper.workflow import work_models
from shopkeeper.workflow.providers import ChatProvider, build_provider
from shopkeeper.workflow.shops import ShopError, require_shop_id
if TYPE_CHECKING:
    from shopkeeper.workflow.store import WorkflowStore

PREFIX = '/api/admin/operations'
MAX_BYTES = 5 * 1024 * 1024
Text = Annotated[str, Field(max_length=20000)]
Name = Annotated[str, Field(min_length=1, max_length=200)]
Money = Annotated[float, Field(ge=0, le=1e12, allow_inf_nan=False)]
Count = Annotated[int, Field(ge=0, le=10**12, strict=True)]
Currency = Annotated[str, Field(pattern=r'^[A-Z]{3}$')]


class Record(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)


class Candidate(Record):
    name: Name
    source_url: Annotated[str, Field(max_length=2000)] = ''
    facts: Text = ''
    notes: Text = ''
    status: Literal['researching', 'shortlisted', 'rejected'] = 'researching'
    cost_amount: Money | None = None
    currency: Currency = 'THB'


class Asset(Record):
    name: Name
    candidate_id: str | None = None
    source: Annotated[str, Field(max_length=2000)] = ''
    language: Annotated[str, Field(max_length=32)] = ''
    draft_source: Annotated[str, Field(max_length=200)] = ''
    kind: Literal['text', 'image'] = 'text'
    text: Text = ''
    mime_type: str = ''
    data_base64: Annotated[str, Field(max_length=4 * ((MAX_BYTES + 2) // 3))] = ''
    # 服务端专用字段：素材是否已通过质检并可作为「可用素材」使用。
    # 客户端不可自报——创建时强制为 draft，只有服务端采纳流程能置为 usable。
    status: Literal['draft', 'usable'] = 'draft'
    copy_run_id: str = ''
    verify_run_id: str = ''
    quality_run_id: str = ''


class Campaign(Record):
    name: Name
    candidate_id: str | None = None
    asset_ids: list[str] = Field(default_factory=list, max_length=100)
    platform: Literal['tiktok', 'shopee', 'other'] = 'tiktok'
    notes: Text = ''
    status: Literal['draft', 'manual_active', 'completed'] = 'draft'
    execution_notes: Text = ''
    # 服务端专用字段：由投放计划助手的运行记录采纳而来（O-18）。
    # 客户端自传无效——采纳流程会覆盖它。
    plan_run_id: str = ''


class DraftImport(Record):
    draft_id: Annotated[str, Field(min_length=1, max_length=100)]
    candidate_id: str | None = None
    name: Name | None = None
    language: Literal['en','th'] = 'en'


class Metric(Record):
    campaign_id: str
    source: Literal['manual', 'csv', 'mock'] = 'manual'
    day: date
    currency: Currency = 'THB'
    impressions: Count | None = None
    clicks: Count | None = None
    spend: Money | None = None
    revenue: Money | None = None
    orders: Count | None = None


class ExtractUrlBody(Record):
    url: Annotated[str, Field(min_length=1, max_length=2000)]


def _validate_candidate_url(text: str) -> httpx.URL:
    """Check the initial URL and every redirect before issuing its request."""
    if any(char.isspace() or ord(char) < 32 for char in text) or "\\" in text:
        raise HTTPException(422, "货源链接包含非法字符。")
    try:
        target = httpx.URL(text)
        # Use HTTPX's ASCII host: socket's Unicode IDNA codec can resolve a different domain.
        raw_hostname = target.raw_host.decode("ascii")
        hostname = raw_hostname.rstrip(".").lower()
        port = target.port
    except (httpx.InvalidURL, ValueError) as exc:
        raise HTTPException(422, "无法解析货源链接。") from exc
    if target.scheme not in {"http", "https"} or not hostname:
        raise HTTPException(422, "货源链接必须是合法的 http:// 或 https:// 网址。")
    if target.userinfo or (port is not None and not 1 <= port <= 65535):
        raise HTTPException(422, "货源链接不可包含凭据，端口必须有效。")
    if hostname == "localhost" or hostname.endswith((".localhost", ".local", ".internal", ".onion")):
        raise HTTPException(422, "禁止提取本地或私有网络地址。")

    try:
        direct_ip = ipaddress.ip_address(hostname)
    except ValueError:
        try:
            # inet_aton recognizes numeric IPv4 aliases without resolving DNS.
            direct_ip = ipaddress.IPv4Address(socket.inet_aton(hostname))
        except OSError:
            direct_ip = None
    if direct_ip is not None:
        direct_ip = getattr(direct_ip, "ipv4_mapped", None) or direct_ip
        if not direct_ip.is_global or direct_ip.is_multicast:
            raise HTTPException(422, "禁止提取本地或私有网络地址。")
        return target
    try:
        addresses = socket.getaddrinfo(raw_hostname, port or (443 if target.scheme == "https" else 80))
        if not addresses:
            raise OSError("empty DNS response")
        for addr in addresses:
            ip = ipaddress.ip_address(addr[4][0])
            ip = getattr(ip, "ipv4_mapped", None) or ip
            # Existing TUN/Fake-IP deployments resolve public domains to this range.
            fake_ip = ip in ipaddress.ip_network("198.18.0.0/15")
            if (not ip.is_global and not fake_ip) or ip.is_multicast:
                raise HTTPException(422, "禁止提取本地或私有网络地址。")
    except (OSError, ValueError) as exc:
        raise HTTPException(422, "无法安全解析货源主机地址，请稍后重试。") from exc
    return target


def _fetch_candidate_page(client: httpx.Client, url: str) -> httpx.Response:
    for hop in range(6):
        target = _validate_candidate_url(url)
        response = client.get(target, follow_redirects=False)
        if response.status_code not in {301, 302, 303, 307, 308}:
            if response.status_code >= 300:
                raise HTTPException(422, f"请求货源网址返回错误状态码 (HTTP {response.status_code})。")
            return response
        location = response.headers.get("location")
        if not location or hop == 5:
            raise HTTPException(422, "货源网址重定向缺少目标或超过 5 次。")
        url = urljoin(str(response.url), location)
    raise HTTPException(422, "货源网址重定向超过限制。")


def extract_candidate_from_url(url: str, *, client: httpx.Client | None = None) -> dict[str, Any]:
    """从货源链接自动解析候选商品基础事实（1688 或通用 HTML 元数据）。"""
    text = str(url or "").strip()
    text = str(_validate_candidate_url(text))
    parsed = urlparse(text)
    hostname = (parsed.hostname or "").rstrip(".").lower()

    # 1. 1688 货源适配器优先（具备结构化商品事实与离线 mock）
    is_1688 = hostname == "1688.com" or hostname.endswith(".1688.com") or hostname == "cbu01.alicdn.com"
    if is_1688:
        official_source = build_product_source()
        is_official = getattr(official_source, "name", "") != "mock_product_source"
        use_mock = (
            client is not None
            or bool(os.getenv("PYTEST_CURRENT_TEST"))
            or os.getenv("ALIBABA_1688_FORCE_MOCK", "0").lower() in ("1", "true")
        )
        if is_official:
            source = official_source
        elif use_mock:
            source = official_source
        else:
            try:
                from shopkeeper.adapters.alibaba_mobile import AlibabaMobileProductSource
                source = AlibabaMobileProductSource()
            except Exception:
                source = official_source
        is_mock = getattr(source, "name", "") == "mock_product_source"
        res = source.fetch(text)
        if not res.ok:
            if res.error and res.error.code == "A1688_ANTIBLOCK":
                raise HTTPException(
                    422,
                    "1688 触发了平台反爬验证（滑块拦截），未能自动读取。请在浏览器中直接复制标题与价格录入，或使用小书签一键导入。",
                )
            err_msg = res.error.message if res.error else "未能解析该 1688 商品"
            raise HTTPException(422, f"1688 商品解析失败: {err_msg}")
        if res.ok and res.data:
            facts_obj = res.data
            parts = []
            if facts_obj.moq:
                parts.append(f"起订量: {facts_obj.moq}")
            if facts_obj.stock:
                parts.append(f"库存: {facts_obj.stock}")
            if facts_obj.seller:
                parts.append(f"供应商: {facts_obj.seller}")
            facts_text = " | ".join(parts) if parts else "1688 货源商品"
            cost = round(facts_obj.price_cents / 100.0, 2) if facts_obj.price_cents else None
            name_text = facts_obj.title_raw[:200]
            note_text = f"1688 自动提取 (货源ID: {facts_obj.source_item_id})"
            if is_mock:
                name_text = f"[Mock演示] {name_text}"
                note_text = f"[离线Mock] 1688 API未接通 (货源ID: {facts_obj.source_item_id})"
            return {
                "name": redact_spans(name_text)[0],
                "source_url": facts_obj.source_url or text,
                "facts": redact_spans(facts_text)[0],
                "notes": note_text,
                "cost_amount": cost,
                "currency": facts_obj.currency if facts_obj.currency in {"CNY", "THB", "USD"} else "CNY",
                "status": "researching",
                "is_mock": is_mock,
            }

    # 2. 通用网页元数据抓取兜底
    headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    }
    try:
        if client is not None:
            resp = _fetch_candidate_page(client, text)
        else:
            with _build_http_client(timeout=8.0, follow_redirects=False, headers=headers) as h_client:
                resp = _fetch_candidate_page(h_client, text)
    except (httpx.HTTPError, httpx.InvalidURL, ValueError) as exc:
        raise HTTPException(422, f"请求货源网址超时或失败: {type(exc).__name__}") from exc

    html_text = resp.text[:500_000]
    soup = BeautifulSoup(html_text, "html.parser")

    title = ""
    og_title = soup.find("meta", property="og:title") or soup.find("meta", attrs={"name": "og:title"})
    if og_title and og_title.get("content"):
        title = str(og_title["content"]).strip()
    elif soup.title and soup.title.get_text():
        title = soup.title.get_text().strip()
    elif soup.find("h1") and soup.find("h1").get_text():
        title = soup.find("h1").get_text().strip()

    if not title:
        raise HTTPException(422, "未能自动从该网页提取到商品标题，请手动填写。")

    title = re.sub(r"\s*[-_|]\s*(1688|淘宝|京东|天猫|TikTok|Shopee|拼多多).*$", "", title, flags=re.IGNORECASE).strip()
    og_desc = soup.find("meta", property="og:description") or soup.find("meta", attrs={"name": "og:description"}) or soup.find("meta", attrs={"name": "description"})
    desc = str(og_desc["content"]).strip() if (og_desc and og_desc.get("content")) else "网页元数据自动提取"

    return {
        "name": redact_spans(title[:200])[0],
        "source_url": text,
        "facts": redact_spans(desc[:1000])[0],
        "notes": f"网页元数据提取 ({parsed.netloc})",
        "cost_amount": None,
        "currency": "THB",
        "status": "researching",
    }


MODELS = {'candidates': Candidate, 'assets': Asset, 'campaigns': Campaign}
MEASURES = ('impressions', 'clicks', 'spend', 'revenue', 'orders')


def ratios(row: dict) -> dict:
    for key, numerator, denominator in [('ctr', 'clicks', 'impressions'), ('cpc', 'spend', 'clicks'), ('roas', 'revenue', 'spend')]:
        a, b = row.get(numerator), row.get(denominator)
        row[key] = a / b if a is not None and b is not None and b > 0 else None
    return row




class OperationsStore:
    def __init__(self, store: WorkflowStore):
        self.store = store

    @staticmethod
    def view(row):
        result = dict(row)
        if 'data_json' in result:
            raw_data = result.pop('data_json')
            data = json.loads(raw_data)
            result.update(data)
            if 'updated_at' in result:
                result['version_digest'] = hashlib.sha256(
                    f"{raw_data}:{result['updated_at']}".encode()
                ).hexdigest()
        result.pop('content', None)
        result.pop('data_base64', None)
        if 'kind' in result:
            result['content_url'] = f"{PREFIX}/assets/{result['id']}/content?{urlencode({'shop_id': result['shop_id']})}"
        return ratios(result) if 'day' in result else result

    def get(self, conn, resource, shop, record_id, *, include_content=False):
        columns = 'id,shop_id,candidate_id,data_json,created_at,updated_at' if resource == 'assets' and not include_content else '*'
        row = conn.execute(f'SELECT {columns} FROM operations_{resource} WHERE shop_id=? AND id=?', (shop, record_id)).fetchone()
        if row is None:
            raise HTTPException(404, '记录不存在于当前店铺')
        return row

    def associations(self, conn, resource, shop, data):
        candidate = data.get('candidate_id')
        if candidate:
            self.get(conn, 'candidates', shop, candidate)
        if resource == 'campaigns':
            for asset in data['asset_ids']:
                self.get(conn, 'assets', shop, asset)

    def save(self, resource, shop, data, record_id=None):
        now = datetime.now(timezone.utc).isoformat()
        with self.store._lock, self.store._connect() as conn:
            conn.execute('BEGIN IMMEDIATE')
            if record_id:
                old = self.get(conn, resource, shop, record_id)
                merged = json.loads(old['data_json'])
                merged.update(data)
            else:
                merged = data
            try:
                model = MODELS[resource].model_validate(merged)
            except ValidationError as exc:
                raise HTTPException(422, exc.errors(include_input=False, include_context=False)) from exc
            fields = model.model_dump(mode='json')
            self.associations(conn, resource, shop, fields)
            record_id = record_id or uuid.uuid4().hex
            conn.execute(f'''INSERT INTO operations_{resource}(id,shop_id,candidate_id,data_json,created_at,updated_at)
                VALUES(?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET candidate_id=excluded.candidate_id,
                data_json=excluded.data_json,updated_at=excluded.updated_at''',
                (record_id, shop, fields.get('candidate_id'), json.dumps(fields, ensure_ascii=False), now, now))
            return self.view(self.get(conn, resource, shop, record_id))

    def list(self, resource, shop, limit, offset, campaign_id=None, start_date=None, end_date=None, source=None):
        where, values = 'shop_id=?', [shop]
        if campaign_id:
            where += ' AND campaign_id=?'
            values.append(campaign_id)
        if start_date:
            where += ' AND day>=?'
            values.append(str(start_date))
        if end_date:
            where += ' AND day<=?'
            values.append(str(end_date))
        if source:
            where += ' AND source=?'
            values.append(source)
        columns = 'id,shop_id,candidate_id,data_json,created_at,updated_at' if resource == 'assets' else '*'
        order = 'day DESC,id' if resource == 'metrics' else 'created_at DESC,id'
        with self.store._connect() as conn:
            total = conn.execute(f'SELECT count(*) FROM operations_{resource} WHERE {where}', values).fetchone()[0]
            rows = conn.execute(f'SELECT {columns} FROM operations_{resource} WHERE {where} ORDER BY {order} LIMIT ? OFFSET ?', [*values,limit,offset]).fetchall()
        return {'items': [self.view(r) for r in rows], 'total': total}

    def asset(self, shop, body):
        fields = body.model_dump()
        blob = None
        if body.kind == 'image':
            try:
                blob = base64.b64decode(body.data_base64, validate=True)
            except (ValueError, binascii.Error) as exc:
                raise HTTPException(422, '非法 base64 图片') from exc
            if not blob or len(blob) > MAX_BYTES:
                raise HTTPException(422, '图片须为 1 byte 至 5 MiB')
            try:
                with Image.open(io.BytesIO(blob)) as image:
                    actual = {'PNG': 'image/png', 'JPEG': 'image/jpeg', 'WEBP': 'image/webp'}.get(image.format)
                    if actual != body.mime_type or actual is None or image.width * image.height > 20_000_000:
                        raise HTTPException(422, '仅允许 PNG/JPEG/WebP，MIME 必须匹配，最多 2000 万像素')
                    image.verify()
                with Image.open(io.BytesIO(blob)) as image:
                    image.load()
            except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError) as exc:
                raise HTTPException(422, '图片损坏或格式不支持') from exc
        elif body.data_base64 or body.mime_type not in ('', 'text/plain'):
            raise HTTPException(422, '文本素材只允许 text/plain 和 text 字段')
        fields['data_base64'] = ''
        fields['mime_type'] = body.mime_type if blob else 'text/plain'
        # 人工上传的素材一律是草稿：客户端不能自报「已通过质检」。
        # 只有服务端的采纳流程（adopt_asset_run）会写入 status=usable。
        fields['status'] = 'draft'
        fields['copy_run_id'] = ''
        fields['verify_run_id'] = ''
        fields['quality_run_id'] = ''
        record_id = uuid.uuid4().hex
        now = datetime.now(timezone.utc).isoformat()
        with self.store._lock, self.store._connect() as conn:
            conn.execute('BEGIN IMMEDIATE')
            self.associations(conn, 'assets', shop, fields)
            conn.execute('INSERT INTO operations_assets VALUES(?,?,?,?,?,?,?)', (record_id,shop,fields['candidate_id'],json.dumps(fields,ensure_ascii=False),now,now,blob))
            return self.view(self.get(conn,'assets',shop,record_id))

    def delete(self, resource, shop, record_id):
        with self.store._lock, self.store._connect() as conn:
            conn.execute('BEGIN IMMEDIATE')
            self.get(conn,resource,shop,record_id)
            if resource == 'candidates':
                for target in ('assets','campaigns'):
                    if conn.execute(f'SELECT 1 FROM operations_{target} WHERE shop_id=? AND candidate_id=? LIMIT 1',(shop,record_id)).fetchone():
                        raise HTTPException(409,'候选商品已有素材或计划关联')
            if resource == 'assets':
                if conn.execute("SELECT 1 FROM operations_campaigns c,json_each(c.data_json,'$.asset_ids') a WHERE c.shop_id=? AND a.value=? LIMIT 1",(shop,record_id)).fetchone():
                    raise HTTPException(409,'素材已被计划引用')
            if resource == 'campaigns':
                if conn.execute('SELECT 1 FROM operations_metrics WHERE shop_id=? AND campaign_id=? LIMIT 1',(shop,record_id)).fetchone():
                    raise HTTPException(409,'计划已有指标记录')
            conn.execute(f'DELETE FROM operations_{resource} WHERE shop_id=? AND id=?',(shop,record_id))
        return {'deleted':True,'id':record_id,'shop_id':shop}

    def metric(self, shop, body):
        fields = body.model_dump(mode='json')
        now = datetime.now(timezone.utc).isoformat()
        with self.store._lock, self.store._connect() as conn:
            conn.execute('BEGIN IMMEDIATE')
            self.get(conn, 'campaigns', shop, body.campaign_id)
            conn.execute('''INSERT INTO operations_metrics VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(shop_id,campaign_id,day,currency) DO UPDATE SET
                impressions=excluded.impressions,clicks=excluded.clicks,spend=excluded.spend,
                revenue=excluded.revenue,orders=excluded.orders,updated_at=excluded.updated_at,source=excluded.source''',
                (uuid.uuid4().hex,shop,body.campaign_id,fields['day'],body.currency,*[fields[k] for k in MEASURES],now,now,body.source))
            row = conn.execute('SELECT * FROM operations_metrics WHERE shop_id=? AND campaign_id=? AND day=? AND currency=?', (shop,body.campaign_id,fields['day'],body.currency)).fetchone()
            return self.view(row)

    def dashboard(self, shop, campaign_id=None, start_date=None, end_date=None, source=None):
        where, params = 'shop_id=?', [shop]
        for column, operator, value in [('campaign_id','=',campaign_id),('day','>=',start_date),('day','<=',end_date),('source','=',source)]:
            if value is not None:
                where += f' AND {column}{operator}?'
                params.append(str(value))
        with self.store._connect() as conn:
            counts = {key: conn.execute(f'SELECT count(*) FROM operations_{key} WHERE shop_id=?',(shop,)).fetchone()[0] for key in MODELS}
            # Incomplete columns remain unknown; never silently treat missing values as zero.
            columns = ','.join(f'CASE WHEN count({k})=count(*) THEN sum({k}) ELSE NULL END AS {k}' for k in MEASURES)
            rows = conn.execute(f'SELECT currency,group_concat(DISTINCT source) AS sources,count(*) AS records,{columns} FROM operations_metrics WHERE {where} GROUP BY currency ORDER BY currency',params).fetchall()
        return {'counts':counts,'currencies':[ratios({**dict(row),'sources':row['sources'].split(',')}) for row in rows], 'recent_campaigns':self.list('campaigns',shop,5,0)['items'], 'filters':{'campaign_id':campaign_id,'start_date':start_date,'end_date':end_date,'source':source}}


    def record_call(self, shop, data, *, failure_reason=None):
        """落一条工作模型调用记录；失败调用同样留痕，便于按 failure_reason 追查。

        审计表不是凭据落点：失败原因、对象 ID 与人工改动都是自由文本，入库前统一脱敏。
        """
        def clean(items):
            return [redact_spans(str(item))[0] for item in (items or [])]

        if failure_reason:
            failure_reason = redact_spans(str(failure_reason))[0]
        now = datetime.now(timezone.utc).isoformat()
        record_id = uuid.uuid4().hex
        with self.store._lock, self.store._connect() as conn:
            conn.execute(
                '''INSERT INTO operations_work_model_calls(id,shop_id,module,prompt_version,model_channel,
                input_object_ids_json,output_object_ids_json,source,human_review,human_changes_json,failure_reason,created_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?)''',
                (record_id, shop, data['module'], data['prompt_version'], data['model_channel'],
                 json.dumps(clean(data.get('input_object_ids')), ensure_ascii=False),
                 json.dumps(clean(data.get('output_object_ids')), ensure_ascii=False),
                 data['source'], int(bool(data.get('human_review'))),
                 json.dumps(clean(data.get('human_changes')), ensure_ascii=False),
                 failure_reason, now))
            row = conn.execute('SELECT * FROM operations_work_model_calls WHERE id=?', (record_id,)).fetchone()
        return self.call_view(row)

    @staticmethod
    def call_view(row):
        result = dict(row)
        for column, key in (('input_object_ids_json', 'input_object_ids'),
                            ('output_object_ids_json', 'output_object_ids'),
                            ('human_changes_json', 'human_changes')):
            result[key] = json.loads(result.pop(column) or '[]')
        result['human_review'] = bool(result['human_review'])
        return result

    def list_calls(self, shop, module=None, limit=50, offset=0):
        where, values = 'shop_id=?', [shop]
        if module:
            where += ' AND module=?'
            values.append(module)
        with self.store._connect() as conn:
            total = conn.execute(f'SELECT count(*) FROM operations_work_model_calls WHERE {where}', values).fetchone()[0]
            rows = conn.execute(
                f'SELECT * FROM operations_work_model_calls WHERE {where} ORDER BY created_at DESC,id LIMIT ? OFFSET ?',
                [*values, limit, offset]).fetchall()
        return {'items': [self.call_view(row) for row in rows], 'total': total}

    def record_run(self, shop: str, data: dict, *, failure_code: str | None = None, failure_reason: str | None = None):
        run_id = str(data.get("id") or uuid.uuid4().hex)
        now = datetime.now(timezone.utc).isoformat()
        if failure_reason:
            failure_reason = redact_spans(str(failure_reason))[0]
        raw_output = redact_spans(str(data.get("raw_output") or ""))[0]
        output_json = json.dumps(data.get("output"), ensure_ascii=False) if data.get("output") is not None else ""
        input_object_ids_json = json.dumps(
            [redact_spans(str(x))[0] for x in (data.get("input_object_ids") or [])],
            ensure_ascii=False,
        )
        context_json = json.dumps(data.get("context") or {}, ensure_ascii=False)
        with self.store._lock, self.store._connect() as conn:
            try:
                conn.execute(
                    '''INSERT INTO operations_work_model_runs (
                        id, shop_id, module, idempotency_key, request_hash, status,
                        prompt_version, model_channel, input_version_digest,
                        input_object_ids_json, output_json, raw_output, failure_code,
                        failure_reason, call_record_id, created_at, updated_at, context_json
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                    (
                        run_id,
                        shop,
                        data["module"],
                        data["idempotency_key"],
                        data["request_hash"],
                        data["status"],
                        data.get("prompt_version") or "work-models-v1.2",
                        data.get("model_channel") or "mock",
                        data.get("input_version_digest") or "",
                        input_object_ids_json,
                        output_json,
                        raw_output,
                        failure_code,
                        failure_reason,
                        data.get("call_record_id"),
                        now,
                        now,
                        context_json,
                    ),
                )
            except sqlite3.IntegrityError:
                row = conn.execute(
                    'SELECT * FROM operations_work_model_runs WHERE shop_id=? AND idempotency_key=?',
                    (shop, data["idempotency_key"]),
                ).fetchone()
                return self.run_view(row)
            row = conn.execute('SELECT * FROM operations_work_model_runs WHERE id=?', (run_id,)).fetchone()
        return self.run_view(row)

    @staticmethod
    def run_view(row):
        if row is None:
            return None
        result = dict(row)
        result["input_object_ids"] = json.loads(result.pop("input_object_ids_json") or "[]")
        output_raw = result.pop("output_json") or ""
        result["output"] = json.loads(output_raw) if output_raw.strip() else None
        context_raw = result.pop("context_json", "") or ""
        result["context"] = json.loads(context_raw) if context_raw.strip() else {}
        return result

    def get_run(self, conn, shop: str, run_id: str):
        row = conn.execute(
            'SELECT * FROM operations_work_model_runs WHERE shop_id=? AND id=?',
            (shop, run_id),
        ).fetchone()
        if not row:
            raise HTTPException(404, f'未找到工作模型运行记录：{run_id}')
        return row

    def get_run_by_idempotency_key(self, shop: str, idempotency_key: str):
        with self.store._connect() as conn:
            row = conn.execute(
                'SELECT * FROM operations_work_model_runs WHERE shop_id=? AND idempotency_key=?',
                (shop, idempotency_key),
            ).fetchone()
        return self.run_view(row) if row else None

    def list_runs(self, shop: str, module: str | None = None, status: str | None = None, limit: int = 50, offset: int = 0):
        where, values = 'shop_id=?', [shop]
        if module:
            where += ' AND module=?'
            values.append(module)
        if status:
            where += ' AND status=?'
            values.append(status)
        with self.store._connect() as conn:
            total = conn.execute(f'SELECT count(*) FROM operations_work_model_runs WHERE {where}', values).fetchone()[0]
            rows = conn.execute(
                f'SELECT * FROM operations_work_model_runs WHERE {where} ORDER BY created_at DESC, id LIMIT ? OFFSET ?',
                [*values, limit, offset],
            ).fetchall()
        return {'items': [self.run_view(row) for row in rows], 'total': total}


    def adopt_candidate_run(self, shop: str, candidate_id: str, req: Any) -> dict[str, Any]:
        with self.store._lock, self.store._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            cand_row = conn.execute(
                "SELECT * FROM operations_candidates WHERE shop_id=? AND id=?",
                (shop, candidate_id),
            ).fetchone()
            if not cand_row:
                raise HTTPException(404, f"未找到商品候选：{candidate_id}")

            current_digest = hashlib.sha256(
                f"{cand_row['data_json']}:{cand_row['updated_at']}".encode()
            ).hexdigest()

            run_row = conn.execute(
                "SELECT * FROM operations_work_model_runs WHERE shop_id=? AND id=?",
                (shop, req.run_id),
            ).fetchone()
            if not run_row:
                raise HTTPException(404, f"未找到工作模型运行记录：{req.run_id}")

            run = self.run_view(run_row)
            if candidate_id not in run.get("input_object_ids", []):
                raise HTTPException(422, "该运行记录不属于此商品候选")
            if run.get("module") not in ("research", "verify"):
                raise HTTPException(422, f"不支持采纳 {run.get('module')} 模块的运行记录")
            if run.get("status") != "succeeded" or not run.get("output"):
                raise HTTPException(422, "不能采纳未成功或无输出的运行记录")

            cand_data = json.loads(cand_row["data_json"])
            # 重复采纳幂等回放：已采纳过此运行结果则直接回放原结果
            if cand_data.get("last_adopted_run_id") == req.run_id:
                view = self.view(cand_row)
                view["replayed"] = True
                return view

            # 版本校验：自分析后候选事实被改动，或请求期望版本不匹配，返回 409
            if run.get("input_version_digest") != current_digest or req.expected_version_digest != current_digest:
                raise HTTPException(
                    409,
                    "版本冲突：商品候选事实自模型分析后已被修改，旧核验或研究结果不再为新版本背书，请重新发起分析。",
                )

            output = run["output"]
            human_changes = list(req.human_changes)

            if run["module"] == "verify":
                if output.get("decision") != "pass":
                    raise HTTPException(422, "核验结果未通过（decision != pass），不能采纳为已核验事实")
                claims = output.get("claims") or []
                usable_claims = [
                    c for c in claims
                    if isinstance(c, dict)
                    and str(c.get("claim", "")).strip()
                    and str(c.get("status", "")).strip().lower() == "supported"
                    and str(c.get("evidence", "")).strip()
                ]
                if not usable_claims:
                    raise HTTPException(
                        422,
                        "核验结果中无任何有效证据支持的声明（缺证据、unsupported 或 contradicted 均不能成为可用声明）",
                    )
                verified_lines = [f"{c['claim']} (证据: {c['evidence']})" for c in usable_claims]
                if req.append_facts:
                    cand_data["facts"] = (cand_data.get("facts", "") + "\n" + req.append_facts.strip()).strip()
                    human_changes.append("追加核验事实: " + req.append_facts.strip())
                else:
                    joined = "\n".join(verified_lines)
                    cand_data["facts"] = (cand_data.get("facts", "") + "\n" + joined).strip()
                    human_changes.append("采纳有效核验声明: " + joined)
                cand_data["last_verified_run_id"] = req.run_id

            elif run["module"] == "research":
                if req.status:
                    human_changes.append(f"状态: {cand_data.get('status')} -> {req.status}")
                    cand_data["status"] = req.status
                else:
                    rec = output.get("recommendation")
                    if rec in ("shortlist", "shortlisted"):
                        human_changes.append(f"状态: {cand_data.get('status')} -> shortlisted")
                        cand_data["status"] = "shortlisted"
                    elif rec in ("reject", "rejected"):
                        human_changes.append(f"状态: {cand_data.get('status')} -> rejected")
                        cand_data["status"] = "rejected"
                if req.append_facts:
                    cand_data["facts"] = (cand_data.get("facts", "") + "\n" + req.append_facts.strip()).strip()
                    human_changes.append("追加研究事实: " + req.append_facts.strip())
                elif output.get("facts"):
                    new_facts = "\n".join(str(f).strip() for f in output.get("facts") if str(f).strip())
                    if new_facts:
                        cand_data["facts"] = (cand_data.get("facts", "") + "\n" + new_facts).strip()
                        human_changes.append("采纳研究事实: " + new_facts)
                if req.notes:
                    cand_data["notes"] = (cand_data.get("notes", "") + "\n" + req.notes.strip()).strip()
                    human_changes.append("追加调研备注: " + req.notes.strip())
                cand_data["last_research_run_id"] = req.run_id

            cand_data["last_adopted_run_id"] = req.run_id
            now = datetime.now(timezone.utc).isoformat()
            data_json_str = json.dumps(cand_data, ensure_ascii=False)
            conn.execute(
                "UPDATE operations_candidates SET data_json=?, updated_at=? WHERE shop_id=? AND id=?",
                (data_json_str, now, shop, candidate_id),
            )

            call_id = run.get("call_record_id")
            if call_id:
                conn.execute(
                    "UPDATE operations_work_model_calls SET human_review=1, human_changes_json=? WHERE shop_id=? AND id=?",
                    (json.dumps(human_changes, ensure_ascii=False), shop, call_id),
                )
            conn.commit()

            updated_row = conn.execute(
                "SELECT * FROM operations_candidates WHERE shop_id=? AND id=?",
                (shop, candidate_id),
            ).fetchone()

        result = self.view(updated_row)
        result["replayed"] = False
        result["human_changes"] = human_changes
        return result

    def adopt_asset_run(self, shop: str, candidate_id: str, req: Any) -> dict[str, Any]:
        """把通过质检的文案采纳为一条新的文本素材。

        采纳的前置条件全部由服务端判定，客户端只能提供运行 ID 与（可选的）人工
        修改后的正文：

        - 质检运行必须成功、四项检查全过、正文与本次质检的文案版本一致；
        - 质检所依据的候选事实版本必须仍是当前版本（事实变了要重新走链路）；
        - 必须存在有效核验（有 supported 且带证据的声明）作为事实背书；
        - 必须有版权来源（候选的货源链接）；
        - 人工改了正文则正文摘要与质检时的摘要不一致 → 原质检不再背书，拒绝采纳。

        重复采纳幂等回放原素材，不新增第二条。
        """
        with self.store._lock, self.store._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            cand_row = conn.execute(
                "SELECT * FROM operations_candidates WHERE shop_id=? AND id=?",
                (shop, candidate_id),
            ).fetchone()
            if not cand_row:
                raise HTTPException(404, f"未找到商品候选：{candidate_id}")
            cand_data = json.loads(cand_row["data_json"])
            candidate_digest = hashlib.sha256(
                f"{cand_row['data_json']}:{cand_row['updated_at']}".encode()
            ).hexdigest()

            run_row = conn.execute(
                "SELECT * FROM operations_work_model_runs WHERE shop_id=? AND id=?",
                (shop, req.quality_run_id),
            ).fetchone()
            if not run_row:
                raise HTTPException(404, f"未找到质检运行记录：{req.quality_run_id}")
            run = self.run_view(run_row)
            if run.get("module") != "quality_check":
                raise HTTPException(422, "只能采纳质检模块的运行记录")
            if candidate_id not in (run.get("input_object_ids") or []):
                raise HTTPException(422, "该质检运行记录不属于此商品候选")
            if run.get("status") != "succeeded" or not run.get("output"):
                raise HTTPException(422, "不能采纳未成功或无输出的质检运行记录")

            output = run["output"]
            checks = output.get("checks") or {}
            required = ("facts_supported", "language_ok", "sensitive_claims_checked", "copyright_source_present")
            if any(checks.get(key) is not True for key in required):
                raise HTTPException(422, "质检四项检查未全部通过，不能成为可用素材")
            if output.get("decision") != "pass":
                raise HTTPException(422, "质检结论未通过（decision != pass），不能成为可用素材")

            linkage = run.get("context") or {}
            copy_run_id = str(linkage.get("copy_run_id") or "")
            if not copy_run_id:
                raise HTTPException(422, "该质检记录没有绑定文案运行，不能作为素材来源")
            copy_row = conn.execute(
                "SELECT * FROM operations_work_model_runs WHERE shop_id=? AND id=?",
                (shop, copy_run_id),
            ).fetchone()
            if not copy_row:
                raise HTTPException(404, f"未找到文案运行记录：{copy_run_id}")
            copy_run = self.run_view(copy_row)
            if copy_run.get("status") != "succeeded" or not copy_run.get("output"):
                raise HTTPException(422, "文案运行记录未成功或无输出，不能采纳")

            # 质检所依据的商品事实版本必须仍是当前版本
            if str(linkage.get("candidate_digest") or "") != candidate_digest:
                raise HTTPException(
                    409,
                    "版本冲突：质检所依据的商品事实已发生变化，请重新生成文案并重新质检。",
                )

            copy_output = copy_run["output"] or {}
            copy_text = work_models.copy_asset_text(copy_output)
            if not copy_text.strip():
                raise HTTPException(422, "文案运行记录没有正文，不能采纳为素材")

            # 人工修改正文后，原质检不再为修改版背书
            final_text = str(req.text or "").strip() or copy_text
            final_digest = work_models.copy_text_digest(final_text)
            if final_digest != str(linkage.get("copy_text_digest") or ""):
                raise HTTPException(
                    409,
                    "版本冲突：正文在质检后被修改，原质检不覆盖修改版，请重新发起质检。",
                )

            # 版权来源必须存在
            copyright_source = str(cand_data.get("source_url") or "").strip()
            if not copyright_source:
                raise HTTPException(422, "候选商品缺少版权来源，无法把文案采纳为可用素材")

            # 有效核验必须存在（由服务端按当前事实版本判定）
            verify_run_id = str(linkage.get("verify_run_id") or "")
            if not verify_run_id:
                raise HTTPException(422, "缺少当前事实版本的有效核验，文案不能作为可用素材")

            # 幂等：同一质检运行已采纳过则回放原素材
            existing = conn.execute(
                "SELECT * FROM operations_assets WHERE shop_id=? AND candidate_id=? AND "
                "json_extract(data_json,'$.quality_run_id')=? LIMIT 1",
                (shop, candidate_id, req.quality_run_id),
            ).fetchone()
            if existing:
                view = self.view(existing)
                view["replayed"] = True
                return view

            human_changes: list[str] = []
            if str(req.text or "").strip() and req.text.strip() != copy_text:
                human_changes.append("人工修改文案正文")

            name = str(req.name or "").strip() or f"{cand_data.get('name') or '商品'} 文案"
            language = str(req.language or "").strip() or "th"
            now = datetime.now(timezone.utc).isoformat()
            asset_id = uuid.uuid4().hex
            fields = {
                "name": name[:200],
                "candidate_id": candidate_id,
                "source": f"work-model:{copy_run_id}",
                "draft_source": "work-model",
                "language": language[:32],
                "kind": "text",
                "text": final_text,
                "mime_type": "text/plain",
                "data_base64": "",
                "status": "usable",
                "copy_run_id": copy_run_id,
                "verify_run_id": verify_run_id,
                "quality_run_id": req.quality_run_id,
            }
            conn.execute(
                "INSERT INTO operations_assets VALUES(?,?,?,?,?,?,?)",
                (
                    asset_id,
                    shop,
                    candidate_id,
                    json.dumps(fields, ensure_ascii=False),
                    now,
                    now,
                    None,
                ),
            )
            call_id = run.get("call_record_id")
            if call_id:
                conn.execute(
                    "UPDATE operations_work_model_calls SET human_review=1, human_changes_json=? WHERE shop_id=? AND id=?",
                    (
                        json.dumps(
                            human_changes + [f"采纳质检结果为素材 {asset_id}"],
                            ensure_ascii=False,
                        ),
                        shop,
                        call_id,
                    ),
                )
            conn.commit()
            asset_row = conn.execute(
                "SELECT * FROM operations_assets WHERE shop_id=? AND id=?", (shop, asset_id)
            ).fetchone()

        result = self.view(asset_row)
        result["replayed"] = False
        result["human_changes"] = human_changes
        return result

    def adopt_campaign_run(self, shop: str, candidate_id: str, req: Any) -> dict[str, Any]:
        """把投放计划助手的建议采纳为一条本地计划（状态固定 draft）。

        服务端判定的准入条件：

        - 计划助手运行必须成功，且其选用的素材 ID 属于本次输入集合且仍属同店；
        - 候选事实版本必须仍是当前版本（事实变了要重新生成计划）；
        - 写入的 ``status`` 恒为 ``draft``——模型不能把它写成 manual_active，
          更不能因此创建上架审批或触发任何平台执行器；
        - ``execution_notes`` 存的是**人工执行说明**，不是平台回执。

        重复采纳同一运行幂等回放原计划，不新增第二条。
        """
        with self.store._lock, self.store._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            cand_row = conn.execute(
                "SELECT * FROM operations_candidates WHERE shop_id=? AND id=?",
                (shop, candidate_id),
            ).fetchone()
            if not cand_row:
                raise HTTPException(404, f"未找到商品候选：{candidate_id}")
            cand_data = json.loads(cand_row["data_json"])
            candidate_digest = hashlib.sha256(
                f"{cand_row['data_json']}:{cand_row['updated_at']}".encode()
            ).hexdigest()

            run_row = conn.execute(
                "SELECT * FROM operations_work_model_runs WHERE shop_id=? AND id=?",
                (shop, req.run_id),
            ).fetchone()
            if not run_row:
                raise HTTPException(404, f"未找到投放计划运行记录：{req.run_id}")
            run = self.run_view(run_row)
            if run.get("module") != "prepare_campaign":
                raise HTTPException(422, "只能采纳投放计划助手模块的运行记录")
            if candidate_id not in (run.get("input_object_ids") or []):
                raise HTTPException(422, "该运行记录不属于此商品候选")
            if run.get("status") != "succeeded" or not run.get("output"):
                raise HTTPException(422, "不能采纳未成功或无输出的运行记录")

            linkage = run.get("context") or {}
            if str(linkage.get("candidate_digest") or "") != candidate_digest:
                raise HTTPException(
                    409,
                    "版本冲突：计划所依据的商品事实已发生变化，请重新生成投放计划。",
                )

            output = run["output"]
            allowed_ids = {str(x) for x in (linkage.get("asset_ids") or [])}
            selected = [str(x) for x in (output.get("selected_asset_ids") or [])]
            for asset_id in selected:
                if asset_id not in allowed_ids:
                    raise HTTPException(
                        422,
                        f"模型引用的素材不在本次输入集合内：{asset_id}",
                    )
                asset_row = conn.execute(
                    "SELECT 1 FROM operations_assets WHERE shop_id=? AND id=?",
                    (shop, asset_id),
                ).fetchone()
                if not asset_row:
                    raise HTTPException(
                        422, f"模型引用的素材不存在或不属于当前店铺：{asset_id}"
                    )

            # 幂等：同一运行已采纳过则回放原计划
            existing = conn.execute(
                "SELECT * FROM operations_campaigns WHERE shop_id=? AND candidate_id=? AND "
                "json_extract(data_json,'$.plan_run_id')=? LIMIT 1",
                (shop, candidate_id, req.run_id),
            ).fetchone()
            if existing:
                view = self.view(existing)
                view["replayed"] = True
                return view

            human_changes: list[str] = []
            name = str(req.name or "").strip() or str(output.get("name") or "").strip()
            if not name:
                name = f"{cand_data.get('name') or '商品'} 投放计划"
            objective = str(output.get("objective") or "").strip()
            budget_note = str(output.get("budget_note") or "").strip()
            checklist = [str(x) for x in (output.get("execution_checklist") or []) if str(x).strip()]
            tracking_fields = [str(x) for x in (output.get("tracking_fields") or []) if str(x).strip()]
            hypotheses = [str(x) for x in (output.get("audience_hypotheses") or []) if str(x).strip()]
            missing = [str(x) for x in (output.get("missing") or []) if str(x).strip()]
            platform = str(output.get("platform") or "other")
            if platform not in ("tiktok", "shopee", "other"):
                platform = "other"

            notes_lines = []
            if objective:
                notes_lines.append(f"目标：{objective}")
            if budget_note:
                notes_lines.append(f"预算说明：{budget_note}")
            if hypotheses:
                notes_lines.append("受众假设：" + "；".join(hypotheses))
            if checklist:
                notes_lines.append("执行清单：" + "；".join(checklist))
            if tracking_fields:
                notes_lines.append("跟踪字段：" + "、".join(tracking_fields))
            if missing:
                notes_lines.append("待补信息：" + "；".join(missing))
            if req.notes:
                notes_lines.append("人工备注：" + req.notes.strip())
                human_changes.append("追加人工备注")

            execution_notes = str(req.execution_notes or "").strip()
            fields = {
                "name": name[:200],
                "candidate_id": candidate_id,
                "asset_ids": selected,
                "platform": platform,
                "notes": "\n".join(notes_lines)[:20000],
                # 采纳的计划恒为草稿：模型与客户端都不能把它标成已启用。
                "status": "draft",
                "execution_notes": execution_notes[:20000],
                "plan_run_id": req.run_id,
            }
            campaign_id = uuid.uuid4().hex
            now = datetime.now(timezone.utc).isoformat()
            conn.execute(
                "INSERT INTO operations_campaigns(id,shop_id,candidate_id,data_json,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?)",
                (
                    campaign_id,
                    shop,
                    candidate_id,
                    json.dumps(fields, ensure_ascii=False),
                    now,
                    now,
                ),
            )
            call_id = run.get("call_record_id")
            if call_id:
                conn.execute(
                    "UPDATE operations_work_model_calls SET human_review=1, human_changes_json=? WHERE shop_id=? AND id=?",
                    (
                        json.dumps(
                            human_changes + [f"采纳投放计划建议为本地计划 {campaign_id}"],
                            ensure_ascii=False,
                        ),
                        shop,
                        call_id,
                    ),
                )
            conn.commit()
            campaign_row = conn.execute(
                "SELECT * FROM operations_campaigns WHERE shop_id=? AND id=?",
                (shop, campaign_id),
            ).fetchone()

        result = self.view(campaign_row)
        result["replayed"] = False
        result["human_changes"] = human_changes
        return result


def mount_operations(app: FastAPI, workflow: WorkflowStore, *, provider: ChatProvider | None = None):
    draft_provider = provider if provider is not None else build_provider()
    from shopkeeper.workflow.operations_limits import OperationsBodyLimit
    from shopkeeper.workflow.work_model_runtime import WorkModelRuntime
    from shopkeeper.workflow.work_model_routes import mount_work_model_routes

    app.add_middleware(OperationsBodyLimit)
    operations = OperationsStore(workflow)
    runtime = WorkModelRuntime(workflow, provider=draft_provider, store=operations)
    operations.runtime = runtime
    mount_work_model_routes(app, operations, runtime)

    def date_range(start, end):
        if start and end and start > end:
            raise HTTPException(422, '起始日期不能晚于结束日期')

    def shop(value):
        if not value or not value.strip():
            raise HTTPException(422, 'shop_id 必填')
        try:
            return require_shop_id(value)
        except ShopError as exc:
            raise HTTPException(422, exc.message) from exc

    def register(resource):
        @app.get(f'{PREFIX}/{resource}')
        def listing(shop_id: str, limit: int = Query(50, ge=1, le=100), offset: int = Query(0, ge=0)):
            return operations.list(resource,shop(shop_id),limit,offset)

        @app.get(f'{PREFIX}/{resource}/{{record_id}}')
        def detail(record_id: str, shop_id: str):
            with workflow._connect() as conn:
                return operations.view(operations.get(conn,resource,shop(shop_id),record_id))

        @app.post(f'{PREFIX}/{resource}')
        def create(body: dict, shop_id: str):
            scope = shop(shop_id)
            if resource == 'assets':
                try:
                    return operations.asset(scope, Asset.model_validate(body))
                except ValidationError as exc:
                    raise HTTPException(422,exc.errors(include_input=False, include_context=False)) from exc
            return operations.save(resource,scope,body)

        @app.patch(f'{PREFIX}/{resource}/{{record_id}}')
        def update(record_id: str, body: dict, shop_id: str):
            if resource == 'assets':
                raise HTTPException(405,'素材不可覆盖，请上传新版本')
            return operations.save(resource,shop(shop_id),body,record_id)

        @app.delete(f'{PREFIX}/{resource}/{{record_id}}')
        def delete(record_id: str, shop_id: str):
            return operations.delete(resource,shop(shop_id),record_id)

    for resource in MODELS:
        register(resource)

    @app.get(f'{PREFIX}/assets/{{record_id}}/content')
    def content(record_id: str, shop_id: str):
        with workflow._connect() as conn:
            raw = operations.get(conn,'assets',shop(shop_id),record_id,include_content=True)
            row = operations.view(raw)
            blob = raw['content']
        headers = {'X-Content-Type-Options':'nosniff', 'Cache-Control':'private, no-store', 'Content-Security-Policy':"default-src 'none'; sandbox"}
        if row['kind'] == 'text':
            return Response(row['text'],media_type='text/plain',headers=headers)
        return Response(blob,media_type=row['mime_type'],headers=headers)

    @app.get(f'{PREFIX}/drafts')
    def drafts(shop_id: str, limit: int = Query(50, ge=1, le=100), offset: int = Query(0, ge=0)):
        scope = shop(shop_id)
        with workflow._connect() as conn:
            where = "shop_id=? AND kind IN ('listing','video_script')"
            total = conn.execute(f'SELECT count(*) FROM drafts WHERE {where}',(scope,)).fetchone()[0]
            rows = conn.execute(f'SELECT id,title,kind,status,source,created_at FROM drafts WHERE {where} ORDER BY created_at DESC,id LIMIT ? OFFSET ?',(scope,limit,offset)).fetchall()
        return {'items':[dict(r) for r in rows], 'total':total}

    @app.post(f'{PREFIX}/assets/from-draft')
    def asset_from_draft(body: DraftImport, shop_id: str):
        body = body.model_dump()
        scope = shop(shop_id)
        draft_id = str(body.get('draft_id') or '').strip()
        language = str(body.get('language') or 'en').strip().lower()
        if not draft_id or language not in {'en','th'}:
            raise HTTPException(422, 'draft_id 必填，language 只能 en 或 th')
        draft = workflow.get_draft(draft_id)
        if not draft or str(draft.get('shop_id') or 'default') != scope or draft.get('kind') not in {'listing','video_script'}:
            raise HTTPException(404, '草稿不存在于当前店铺')
        payload = draft.get('payload') if isinstance(draft.get('payload'), dict) else {}
        text = str(payload.get(f'body_{language}') or draft.get(f'body_{language}') or '')
        title = str(payload.get(f'title_{language}') or draft.get('title') or '')
        if not text and not title:
            raise HTTPException(422, '草稿没有可导入的文本素材')
        try:
            item = Asset(name=body.get('name') or f'{draft.get("title") or "agent draft"} ({language})', candidate_id=body.get('candidate_id'), source=f'agent-draft:{draft_id}', draft_source=str(draft.get('source') or 'unknown'), language=language, kind='text', text=(title+'\n'+text).strip())
        except ValidationError as exc:
            raise HTTPException(422,exc.errors(include_input=False,include_context=False)) from exc
        return operations.asset(scope,item)

    @app.post(f'{PREFIX}/candidates/extract-url')
    def candidate_extract_url(body: ExtractUrlBody, shop_id: str):
        shop(shop_id)
        return extract_candidate_from_url(body.url)

    @app.post(f'{PREFIX}/candidates/{{candidate_id}}/quick-list')
    def candidate_quick_list(candidate_id: str, shop_id: str):
        scope = shop(shop_id)
        with workflow._connect() as conn:
            cand_row = operations.get(conn, 'candidates', scope, candidate_id)
        cand_data = json.loads(cand_row['data_json'])
        cand_name = str(cand_data.get('name') or '未命名商品').strip()
        cand_facts = str(cand_data.get('facts') or '').strip()
        clean_specs = re.sub(r'起订量?:\s*\d+', '', cand_facts).replace(' | ', ' ').strip()
        item = {
            'name': cand_name,
            'specs': clean_specs,
        }
        from shopkeeper.workflow.drafts import DraftError, generate_drafts

        try:
            created = generate_drafts(workflow, draft_provider, 'listing', [item], shop_id=scope)
        except DraftError as exc:
            raise HTTPException(409, str(exc)) from exc
        if not created:
            raise HTTPException(500, '未能生成 Listing 草稿。')
        draft = created[0]

        return {
            'ok': True,
            'candidate_id': candidate_id,
            'draft_id': draft['id'],
            'title': draft.get('title') or cand_name,
            'approved': False,
            'status': draft['status'],
            'source': draft['source'],
            'degraded': bool(draft.get('degraded')),
            'qa': draft.get('qa', {}),
            'message': (
                'Listing 草稿未通过质检，请修改候选商品事实后重新生成。'
                if draft['status'] == 'needs_revision'
                else 'Listing 草稿已生成，请查看内容与质检结果后人工审批。'
            ),
        }

    @app.post(f'{PREFIX}/metrics')
    def metric(body: Metric, shop_id: str):
        return operations.metric(shop(shop_id),body)

    @app.get(f'{PREFIX}/metrics')
    def metrics(shop_id: str, limit: int = Query(50,ge=1,le=100), offset: int = Query(0,ge=0), campaign_id: str | None = None, start_date: date | None = None, end_date: date | None = None, source: Literal['manual','csv','mock'] | None = None):
        date_range(start_date,end_date)
        return operations.list('metrics',shop(shop_id),limit,offset,campaign_id,start_date,end_date,source)

    @app.delete(f'{PREFIX}/metrics/{{record_id}}')
    def delete_metric(record_id: str, shop_id: str):
        return operations.delete('metrics',shop(shop_id),record_id)

    @app.get(f'{PREFIX}/dashboard')
    def dashboard(shop_id: str, campaign_id: str | None = None, start_date: date | None = None, end_date: date | None = None, source: Literal['manual','csv','mock'] | None = None):
        date_range(start_date,end_date)
        return operations.dashboard(shop(shop_id),campaign_id,start_date,end_date,source)

    @app.get(f'{PREFIX}/work-model-calls')
    def work_model_calls(shop_id: str, module: str | None = None, limit: int = Query(50, ge=1, le=100), offset: int = Query(0, ge=0)):
        from shopkeeper.workflow.work_models import MODULES

        if module is not None and module not in MODULES:
            raise HTTPException(422, '未知工作模型')
        return operations.list_calls(shop(shop_id), module, limit, offset)
