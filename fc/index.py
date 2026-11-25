#!/usr/bin/env python3
"""ticket-archiver 云函数版（阿里云函数计算 FC 3.0）

流程：QQ 邮箱 IMAP → 找 12306 发票邮件 → 下载 .zip/.ofd/.pdf
     → 解析出发票号与全部要素（OFD 走 XBRL；纯 PDF 走文本抽取）
     → 按发票号查 Tablestore，已入库则跳过（幂等）
     → 拆包后把 pdf / ofd 分别写入 OSS
     → 元数据写入 Tablestore.invoices
     → 有新发票时推送飞书

运行时身份：FC 注入的 RAM 角色，只授予 train-invoice-mint 对象读写
和 ticket-archiver/invoices 表读写。
"""

from __future__ import annotations

import base64
import email
import hashlib
import hmac
import imaplib
import io
import json
import os
import re
import ssl
import time
import urllib.parse
import urllib.request
import zipfile
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

from pypdf import PdfReader
from tablestore import Direction, INF_MAX, INF_MIN, OTSClient, Row

SENDER = "12306@rails.com.cn"


# ────────────────────────── OSS（V1 签名，零依赖） ──────────────────────────

class Oss:
    def __init__(self, bucket: str, endpoint: str, ak: str, sk: str, token: str = ""):
        self.bucket = bucket
        self.endpoint = endpoint
        self.ak = ak
        self.sk = sk
        self.token = token

    @staticmethod
    def _httpdate() -> str:
        return datetime.now(timezone.utc).strftime("%a, %d %b %Y %H:%M:%S GMT")

    def _sign(self, verb: str, key: str, content_md5: str = "", content_type: str = "",
              date: str = "") -> dict:
        oss_headers = {}
        if self.token:
            oss_headers["x-oss-security-token"] = self.token
        canon = "".join(f"{k}:{v}\n" for k, v in sorted(oss_headers.items()))
        resource = f"/{self.bucket}/{key}" if key else f"/{self.bucket}/"
        sts = f"{verb}\n{content_md5}\n{content_type}\n{date}\n{canon}{resource}"
        sig = base64.b64encode(
            hmac.new(self.sk.encode(), sts.encode("utf-8"), hashlib.sha1).digest()
        ).decode()
        return {"Authorization": f"OSS {self.ak}:{sig}", "Date": date, **oss_headers}

    def _url(self, key: str) -> str:
        return f"https://{self.bucket}.{self.endpoint}/{urllib.parse.quote(key)}"

    def exists(self, key: str) -> bool:
        req = urllib.request.Request(self._url(key), method="HEAD", headers=self._sign("HEAD", key, date=self._httpdate()))
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return r.status == 200
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return False
            raise

    def put(self, key: str, data: bytes, content_type: str = "application/octet-stream") -> None:
        md5 = base64.b64encode(hashlib.md5(data).digest()).decode()
        date = self._httpdate()
        headers = self._sign("PUT", key, content_md5=md5, content_type=content_type, date=date)
        headers.update({"Content-MD5": md5, "Content-Type": content_type, "Content-Length": str(len(data))})
        req = urllib.request.Request(self._url(key), data=data, method="PUT", headers=headers)
        with urllib.request.urlopen(req, timeout=120) as r:
            if r.status not in (200, 201):
                raise RuntimeError(f"OSS PUT {key} -> {r.status}")


# ────────────────────────── Tablestore ──────────────────────────

def ots_client() -> OTSClient:
    return OTSClient(
        os.environ.get("OTS_ENDPOINT", "https://train-invoice.cn-beijing.ots.aliyuncs.com"),
        os.environ["ALIBABA_CLOUD_ACCESS_KEY_ID"],
        os.environ["ALIBABA_CLOUD_ACCESS_KEY_SECRET"],
        os.environ.get("OTS_INSTANCE", "train-invoice"),
        sts_token=os.environ.get("ALIBABA_CLOUD_SECURITY_TOKEN") or None,
        enable_native=False,
    )


def already_archived(client: OTSClient, invoice_no: str) -> bool:
    table = os.environ.get("OTS_TABLE", "invoices")
    _, row, _ = client.get_row(table, [("invoice_no", invoice_no)], columns_to_get=["invoice_no"])
    return row is not None and len(row.primary_key) > 0


def count_invoices(client: OTSClient, cap: int = 2000) -> int:
    """统计已归档张数（只读主键，够用于卡片副标题）。"""
    table = os.environ.get("OTS_TABLE", "invoices")
    total = 0
    start = [("invoice_no", INF_MIN)]
    while total < cap:
        _, next_start, rows, _ = client.get_range(
            table, Direction.FORWARD, start, [("invoice_no", INF_MAX)],
            columns_to_get=["invoice_no"], limit=100)
        total += len(rows)
        if not rows or next_start is None:
            break
        start = next_start
    return total


def save_meta(client: OTSClient, meta: dict) -> None:
    table = os.environ.get("OTS_TABLE", "invoices")
    pk = [("invoice_no", meta["invoice_no"])]
    attrs = [(k, v) for k, v in meta.items() if k != "invoice_no" and v not in ("", None)]
    client.put_row(table, Row(pk, attrs))


# ────────────────────────── 飞书 ──────────────────────────

def _post_json(url: str, payload: dict, token: str = "") -> dict:
    headers = {"Content-Type": "application/json; charset=utf-8"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, data=json.dumps(payload, ensure_ascii=False).encode(),
                                 headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read().decode())


def _bj_now() -> datetime:
    return datetime.now(timezone(timedelta(hours=8)))


def _money(value) -> str:
    try:
        return f"¥{float(value):.2f}"
    except (TypeError, ValueError):
        return "—"


def build_card(items: list[dict], total: int) -> dict:
    """竖排卡片：一张发票两行，标题栏报时间，底部只留发送时间。"""
    now = _bj_now()
    blocks = []
    for meta in items[:10]:
        date_ = meta.get("travel_date") or meta.get("issue_date") or "—"
        route = f"{meta.get('from_station') or '—'} → {meta.get('to_station') or '—'}"
        kind = "退票" if meta.get("business_type") == "退" else "购票"
        detail = "　".join(x for x in (meta.get("train_no") or "", kind, _money(meta.get("fare"))) if x)
        blocks.append(f"**{date_}　{route}**\n{detail}")

    elements = [{"tag": "markdown", "content": "\n\n".join(blocks)}]
    if len(items) > 10:
        elements.append({"tag": "markdown",
                         "content": f"<font color='grey'>…另有 {len(items) - 10} 张未展示</font>"})
    elements.extend([
        {"tag": "hr"},
        {"tag": "markdown",
         "content": f"<font color='grey'>已归档到私有对象存储 · {now.strftime('%Y-%m-%d %H:%M')}</font>"},
    ])
    return {
        "schema": "2.0",
        "header": {
            "template": "blue",
            "title": {"tag": "plain_text", "content": f"🚄 12306 发票归档 · {now.strftime('%m-%d %H:%M')}"},
            "subtitle": {"tag": "plain_text", "content": f"本次 {len(items)} 张 · 累计 {total} 张"},
        },
        "body": {"elements": elements},
    }


def feishu_notify(items: list[dict], total: int) -> None:
    app_id = os.environ.get("FEISHU_APP_ID", "")
    app_secret = os.environ.get("FEISHU_APP_SECRET", "")
    chat_id = os.environ.get("FEISHU_CHAT_ID", "")
    if not (app_id and app_secret and chat_id):
        print("[warn] 未配置飞书，跳过通知")
        return
    token = _post_json("https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
                       {"app_id": app_id, "app_secret": app_secret}).get("tenant_access_token", "")
    if not token:
        print("[warn] 飞书 token 获取失败")
        return

    url = "https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type=chat_id"
    card = build_card(items, total)
    resp = _post_json(url, {"receive_id": chat_id, "msg_type": "interactive",
                            "content": json.dumps(card, ensure_ascii=False)}, token=token)
    if resp.get("code") == 0:
        return
    # 卡片被拒时退回纯文本，保证通知不丢
    print(f"[warn] 卡片发送失败 {resp.get('code')} {resp.get('msg')}，回退纯文本")
    lines = [f"{m.get('travel_date', '')} {m.get('from_station', '')}→{m.get('to_station', '')} "
             f"{m.get('train_no', '')} {_money(m.get('fare'))}" for m in items[:10]]
    text = f"12306 发票归档 {len(items)} 张（累计 {total}）\n" + "\n".join(lines)
    _post_json(url, {"receive_id": chat_id, "msg_type": "text",
                     "content": json.dumps({"text": text}, ensure_ascii=False)}, token=token)


# ────────────────────────── 邮件 ──────────────────────────

def fetch_attachments() -> list[dict]:
    account = os.environ["EMAIL_ACCOUNT"]
    code = os.environ["EMAIL_AUTH_CODE"]
    host = os.environ.get("EMAIL_IMAP_HOST", "imap.qq.com")
    port = int(os.environ.get("EMAIL_IMAP_PORT", "993"))

    mail = imaplib.IMAP4_SSL(host, port, timeout=60, ssl_context=ssl.create_default_context())
    mail.login(account, code)
    mail.select("INBOX")
    status, data = mail.search(None, f'(FROM "{SENDER}")')
    ids = data[0].split() if status == "OK" else []
    print(f"[info] 12306 邮件 {len(ids)} 封")

    out: list[dict] = []
    for mid in ids:
        status, data = mail.fetch(mid, "(RFC822)")
        if status != "OK" or not data or not data[0]:
            continue
        msg = email.message_from_bytes(data[0][1])
        mail_date = ""
        try:
            mail_date = parsedate_to_datetime(msg.get("Date")).astimezone().strftime("%Y-%m-%d")
        except Exception:
            pass
        for part in msg.walk():
            if part.get_content_disposition() != "attachment":
                continue
            fn = part.get_filename() or ""
            ext = os.path.splitext(fn)[1].lower()
            if ext not in (".zip", ".ofd", ".pdf"):
                continue
            payload = part.get_payload(decode=True)
            if payload:
                out.append({"filename": fn, "data": payload, "ext": ext, "mail_date": mail_date})
    mail.logout()
    print(f"[info] 附件 {len(out)} 个")
    return out


# ────────────────────────── 解析 ──────────────────────────

def parse_ofd(data: bytes) -> dict:
    """OFD 内 rai_issuer_*.xml 是 XBRL，字段最全。"""
    info = {}
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            target = next((n for n in z.namelist() if "rai_issuer" in n and n.endswith(".xml")), None)
            if not target:
                return info
            xml = z.read(target).decode("utf-8", "replace")
    except Exception:
        return info

    fields = {
        "invoice_no": "ElectronicInvoiceRailwayETicketNumber",
        "issue_date": "DateOfIssue",
        "business_type": "TypeOfBusiness",
        "from_station": "DepartureStation",
        "to_station": "DestinationStation",
        "train_no": "TrainNumber",
        "travel_date": "TravelDate",
        "depart_time": "DepartureTime",
        "seat_level": "SeatLevel",
        "carriage": "Carriage",
        "seat": "Seat",
        "fare": "Fare",
        "tax_rate": "TaxRate",
        "tax_amount": "TaxAmount",
        "amount_excl_tax": "TotalAmountExcludingTax",
        "eticket_no": "ETicketNumber",
        "passenger_name": "Name",
        "id_masked": "IdNumber",
        "original_invoice": "NumberOfOriginalInvoice",
    }
    for key, tag in fields.items():
        m = re.search(rf"{tag}[^>]*>([^<]+)<", xml)
        if m and m.group(1).strip():
            info[key] = m.group(1).strip()
    return info


def parse_pdf(data: bytes) -> dict:
    """纯 PDF（ofd2pdflib 转出）走文本抽取；字段不全时留空。"""
    try:
        text = "\n".join((p.extract_text() or "") for p in PdfReader(io.BytesIO(data)).pages)
    except Exception:
        return {}
    info = {}
    patterns = {
        "invoice_no": r"发票号码[:：]\s*(\d{15,25})",
        "issue_date": r"开票日期[:：]\s*(\d{4})年(\d{1,2})月(\d{1,2})日",
        "travel_date": r"(\d{4})年(\d{1,2})月(\d{1,2})日\s*\n?\s*\d{1,2}:\d{2}开",
        "fare": r"票价[:：]\s*[￥¥]\s*([\d.]+)",
        "eticket_no": r"电子客票号[:：]\s*(\d+)",
        "id_masked": r"(\d{6}\*{4}\d{4})",
        "passenger_name": r"\d{6}\*{4}\d{4}\s+([\u4e00-\u9fa5]{2,10})",
        "train_no": r"\b([GDCZTKY]\d{1,4})\b",
    }
    for key, pat in patterns.items():
        m = re.search(pat, text)
        if m:
            if key in ("issue_date", "travel_date"):
                info[key] = f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
            else:
                info[key] = m.group(1).strip()
    return info


def split_attachment(att: dict) -> tuple[dict, dict]:
    """返回 (meta, payloads)，payloads 可能是 {'pdf': bytes, 'ofd': bytes}。"""
    payloads: dict[str, bytes] = {}
    meta: dict = {}
    if att["ext"] == ".ofd":
        payloads["ofd"] = att["data"]
        meta = parse_ofd(att["data"])
    elif att["ext"] == ".pdf":
        payloads["pdf"] = att["data"]
        meta = parse_pdf(att["data"])
    elif att["ext"] == ".zip":
        try:
            with zipfile.ZipFile(io.BytesIO(att["data"])) as z:
                for name in z.namelist():
                    low = name.lower()
                    if low.endswith(".pdf"):
                        payloads["pdf"] = z.read(name)
                    elif low.endswith(".ofd"):
                        payloads["ofd"] = z.read(name)
        except Exception:
            return {}, {}
        if "ofd" in payloads:
            meta = parse_ofd(payloads["ofd"])
        elif "pdf" in payloads:
            meta = parse_pdf(payloads["pdf"])
    return meta, payloads


def trip_folder(meta: dict, att: dict) -> str:
    """一趟车一个文件夹：日期 出发-到达 车次。"""
    date_ = meta.get("travel_date") or meta.get("issue_date") or att.get("mail_date") or "unknown"
    parts = [date_]
    if meta.get("from_station") and meta.get("to_station"):
        parts.append(f"{meta['from_station']}-{meta['to_station']}")
    if meta.get("train_no"):
        parts.append(meta["train_no"])
    return " ".join(parts)


# ────────────────────────── 入口 ──────────────────────────

def handler(event, context):
    started = time.time()
    prefix = os.environ.get("OSS_PREFIX", "invoices/")
    oss = Oss(
        bucket=os.environ.get("OSS_BUCKET", "train-invoice-archive"),
        endpoint=os.environ.get("OSS_ENDPOINT", "oss-cn-beijing.aliyuncs.com"),
        ak=os.environ["ALIBABA_CLOUD_ACCESS_KEY_ID"],
        sk=os.environ["ALIBABA_CLOUD_ACCESS_KEY_SECRET"],
        token=os.environ.get("ALIBABA_CLOUD_SECURITY_TOKEN", ""),
    )
    ots = ots_client()

    atts = fetch_attachments()
    new_items, skipped = [], 0
    seen: set[str] = set()

    for att in atts:
        meta, payloads = split_attachment(att)
        if not payloads:
            continue
        invoice_no = meta.get("invoice_no") or "sha1:" + hashlib.sha1(att["data"]).hexdigest()[:16]
        meta["invoice_no"] = invoice_no

        if invoice_no in seen or already_archived(ots, invoice_no):
            skipped += 1
            continue
        seen.add(invoice_no)

        folder = trip_folder(meta, att)
        keys = {}
        for fmt, ctype in (("pdf", "application/pdf"), ("ofd", "application/ofd")):
            if fmt not in payloads:
                continue
            key = f"{prefix}{folder}/{invoice_no}/{invoice_no}.{fmt}"
            oss.put(key, payloads[fmt], content_type=ctype)
            keys[fmt] = key

        meta.update({
            "oss_pdf_key": keys.get("pdf", ""),
            "oss_ofd_key": keys.get("ofd", ""),
            "formats": ",".join(sorted(payloads)),
            "source_file": att["filename"],
            "archived_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        })
        for numeric in ("fare", "tax_rate", "tax_amount", "amount_excl_tax"):
            if numeric in meta:
                try:
                    meta[numeric] = float(meta[numeric])
                except ValueError:
                    pass
        save_meta(ots, meta)
        new_items.append(meta)
        print(f"[ok] {invoice_no} {folder}")

    if new_items:
        try:
            total = count_invoices(ots)
        except Exception as exc:  # 统计失败不影响通知
            print("[warn] 统计累计张数失败:", type(exc).__name__)
            total = len(new_items)
        feishu_notify(new_items, total)

    return {
        "attachments": len(atts),
        "uploaded": len(new_items),
        "skipped": skipped,
        "elapsed": round(time.time() - started, 1),
        "invoice_nos": [m["invoice_no"] for m in new_items],
    }


if __name__ == "__main__":
    print(json.dumps(handler({}, None), ensure_ascii=False, indent=2))