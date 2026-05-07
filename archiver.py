#!/usr/bin/env python3
"""
ticket-archiver - 12306火车电子发票自动归档工具

从QQ邮箱下载12306电子发票附件（OFD/PDF/ZIP），
自动解析中文站名、车次，
按「日期 出发站-到达站 车次」格式重命名，
上传到百度网盘归档。

使用：
    1. cp .env.example .env   # 填写邮箱配置
    2. pip install python-dotenv
    3. python3 archiver.py
"""

import imaplib
import email
import os
import zipfile
import re
import tempfile
import shutil
import subprocess
import sys

from dotenv import load_dotenv

# ── 加载环境变量 ──────────────────────────────────────────
load_dotenv()

EMAIL_ACCOUNT = os.environ.get("EMAIL_ACCOUNT", "")
EMAIL_AUTH_CODE = os.environ.get("EMAIL_AUTH_CODE", "")
EMAIL_IMAP_HOST = os.environ.get("EMAIL_IMAP_HOST", "imap.qq.com")
EMAIL_IMAP_PORT = int(os.environ.get("EMAIL_IMAP_PORT", "993"))
BDPAN_TARGET = os.environ.get("BDPAN_TARGET", "/apps/bdpan/12306_invoices")
LOCAL_TEMP = os.environ.get("LOCAL_TEMP", "/tmp/12306_invoices")

SENDER = "12306@rails.com.cn"


def fetch_invoice_emails():
    """从邮箱下载12306发票附件"""
    if not EMAIL_ACCOUNT or not EMAIL_AUTH_CODE:
        print("❌ 请在 .env 中配置 EMAIL_ACCOUNT 和 EMAIL_AUTH_CODE")
        sys.exit(1)

    print(f"🔌 连接邮箱 {EMAIL_ACCOUNT} ...")
    mail = imaplib.IMAP4_SSL(EMAIL_IMAP_HOST, EMAIL_IMAP_PORT)
    mail.login(EMAIL_ACCOUNT, EMAIL_AUTH_CODE)
    mail.select("INBOX")

    result, data = mail.search(None, f'(FROM "{SENDER}")')
    ids = data[0].split()
    print(f"📩 找到 {len(ids)} 封来自 12306 的邮件")

    records = []
    for mid in ids:
        result, data = mail.fetch(mid, "(RFC822)")
        raw = data[0][1]
        msg = email.message_from_bytes(raw)

        if not msg.is_multipart():
            continue

        for part in msg.walk():
            if part.get_content_disposition() != "attachment":
                continue
            fn = part.get_filename()
            if fn and fn.endswith((".zip", ".ofd")):
                records.append({
                    "filename": fn,
                    "data": part.get_payload(decode=True),
                    "ext": os.path.splitext(fn)[1],
                })

    mail.logout()
    print(f"📎 共提取 {len(records)} 个附件")
    return records


def parse_ofd_xbrl(ofd_path):
    """从OFD文件的XBRL结构化XML中提取字段

    XML标签说明:
        rai:DepartureStation     - 出发站（中文）
        rai:DestinationStation   - 到达站（中文）
        rai:TrainNumber          - 车次
        rai:TravelDate           - 乘车日期
    """
    info = {"date": "", "from_cn": "", "to_cn": "", "train": "", "name": ""}
    try:
        with zipfile.ZipFile(ofd_path) as z:
            for name in z.namelist():
                if "rai_issuer" in name and name.endswith(".xml"):
                    xml = z.read(name).decode("utf-8", errors="replace")
                    patterns = [
                        ("from_cn", r"rai:DepartureStation[^>]*>([^<]+)"),
                        ("to_cn", r"rai:DestinationStation[^>]*>([^<]+)"),
                        ("train", r"rai:TrainNumber[^>]*>([^<]+)"),
                        ("date", r"rai:TravelDate[^>]*>([^<]+)"),
                    ]
                    for key, pat in patterns:
                        m = re.search(pat, xml)
                        if m:
                            info[key] = m.group(1).strip()
                    break
    except Exception:
        pass
    return info


def rename_attachments(records):
    """解析附件并生成中文文件名"""
    tmpdir = tempfile.mkdtemp()
    renamed = []

    for rec in records:
        raw_path = os.path.join(tmpdir, rec["filename"])
        with open(raw_path, "wb") as f:
            f.write(rec["data"])

        # 解析信息
        if rec["ext"] == ".zip":
            try:
                with zipfile.ZipFile(raw_path) as z:
                    for zname in z.namelist():
                        if zname.endswith(".ofd"):
                            ep = os.path.join(tmpdir, f"_{rec['filename']}.ofd")
                            with open(ep, "wb") as f:
                                f.write(z.read(zname))
                            info = parse_ofd_xbrl(ep)
                            break
                    else:
                        info = {}
            except Exception:
                info = {}
        elif rec["ext"] == ".ofd":
            info = parse_ofd_xbrl(raw_path)
        else:
            info = {}

        date_ = (info.get("date") or rec.get("mail_date", "")).replace("/", "-")
        fc = info.get("from_cn", "")
        tc = info.get("to_cn", "")
        tr = info.get("train", "")

        parts = [date_] if date_ else []
        if fc and tc:
            parts.append(f"{fc}-{tc}")
        if tr:
            parts.append(tr)

        new_name = " ".join(parts) + rec["ext"]
        renamed.append({"org": rec["filename"], "new": new_name, "path": raw_path})

    shutil.rmtree(tmpdir)
    renamed.sort(key=lambda x: x["new"])
    return renamed


def save_locally(renamed, dest_dir):
    """将重命名后的文件保存到本地临时目录"""
    os.makedirs(dest_dir, exist_ok=True)
    # 清理旧文件
    for f in os.listdir(dest_dir):
        os.remove(os.path.join(dest_dir, f))
    for r in renamed:
        shutil.copy2(r["path"], os.path.join(dest_dir, r["new"]))
    print(f"📁 本地临时文件 -> {dest_dir}")


def upload_to_baidupan(source_dir, target):
    """通过 bdpan CLI 上传到百度网盘"""
    import subprocess

    print(f"☁️  上传到百度网盘 {target} ...")

    # 清理并重建网盘目录
    subprocess.run(["bdpan", "rm", target, "-f"], capture_output=True)
    subprocess.run(["bdpan", "mkdir", target], capture_output=True)

    files = sorted(os.listdir(source_dir))
    for f in files:
        if not f.endswith((".zip", ".ofd")):
            continue
        local = os.path.join(source_dir, f)
        remote = f"{target}/{f}"
        result = subprocess.run(
            ["bdpan", "upload", local, remote],
            capture_output=True, text=True,
        )
        if result.returncode == 0:
            print(f"  ✓ {f}")
        else:
            print(f"  ✗ {f}: {result.stderr.strip()}")


def push_to_github():
    """初始化 git 并推送到 GitHub"""
    repo_dir = os.path.dirname(os.path.abspath(__file__))

    # 检查是否已有远程仓库
    result = subprocess.run(
        ["git", "remote", "get-url", "origin"],
        capture_output=True, text=True, cwd=repo_dir,
    )
    if result.returncode == 0:
        # 已有远程仓库，直接提交
        print(f"📤 推送到 {result.stdout.strip()}")
        subprocess.run(["git", "add", "-A"], cwd=repo_dir)
        subprocess.run(
            ["git", "commit", "--allow-empty", "-m", "update: ticket archiver"],
            capture_output=True, cwd=repo_dir,
        )
        subprocess.run(["git", "push"], cwd=repo_dir)
    else:
        # 创建新仓库
        print("🆕 创建 GitHub 仓库 ticket-archiver ...")
        subprocess.run(
            ["gh", "repo", "create", "ticket-archiver",
             "--public", "--push", "--source=.", "--remote=origin",
             "--description=12306火车电子发票自动归档：从QQ邮箱下载→解析中文站名→百度网盘"],
            cwd=repo_dir,
        )


def main():
    print("=" * 50)
    print("🚄 ticket-archiver - 12306发票自动归档")
    print("=" * 50)

    records = fetch_invoice_emails()
    if not records:
        print("😴 没有找到发票附件")
        return

    renamed = rename_attachments(records)

    print(f"\n📋 重命名结果（共 {len(renamed)} 个文件）：")
    for r in renamed:
        print(f"  {r['new']}")

    save_locally(renamed, LOCAL_TEMP)
    upload_to_baidupan(LOCAL_TEMP, BDPAN_TARGET)

    print(f"\n✅ 完成！共归档 {len(renamed)} 个发票文件到百度网盘")
    print(f"   📂 {BDPAN_TARGET}")


if __name__ == "__main__":
    main()
