#!/usr/bin/env python3
"""部署 ticket-archiver 到腾讯云云函数（SCF，ap-beijing）。

架构：计算在腾讯云 SCF，存储仍在阿里云（OSS 存文件、Tablestore 存元数据）。
因此函数环境变量里带一组阿里云 RAM 用户（ticket-archiver-scf）的 AK。

前置：
  - tccli 已配置（ap-beijing）
  - Infisical tools/prod /ticket-archiver 含邮箱、飞书、阿里云 AK
  - COS 桶 chat-develop-1253319037 用于代码包中转

用法：python3 scripts/deploy.py
"""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from build_pkg import build  # noqa: E402

REGION = "ap-beijing"
FUNCTION = "ticket-archiver"
NAMESPACE = "default"
COS_BUCKET = "chat-develop-1253319037"
COS_KEY = "ticket-archiver/code.zip"
INFISICAL_PROJECT = "79c61872-d992-44c7-a5ae-d3e70e004e7f"
SECRET_KEYS = [
    "EMAIL_ACCOUNT", "EMAIL_AUTH_CODE", "FEISHU_APP_ID", "FEISHU_APP_SECRET", "FEISHU_CHAT_ID",
    "ALIBABA_CLOUD_ACCESS_KEY_ID", "ALIBABA_CLOUD_ACCESS_KEY_SECRET",
]
DESCRIPTION = "12306 invoice archiver: QQ mail -> parse -> Aliyun OSS + Tablestore -> Feishu"


def tccli(*args: str) -> subprocess.CompletedProcess:
    """调用 tccli；装在本机 python 里就用模块方式，否则退回可执行文件。"""
    if importlib.util.find_spec("tccli"):
        cmd = [sys.executable, "-c", "from tccli.main import main; main()", *args]
    else:
        cmd = [shutil.which("tccli") or "tccli", *args]
    return subprocess.run(cmd, capture_output=True, text=True)


def secrets() -> dict:
    out = subprocess.run(
        ["infisical", "secrets-manager", "secrets", "get",
         "--projectId", INFISICAL_PROJECT, "--env", "prod", "--path", "/ticket-archiver",
         "--output", "json", "--domain=https://secrets.a4.fit"],
        capture_output=True, text=True,
    )
    data = {s["secretKey"]: s["secretValue"] for s in json.loads(out.stdout)}
    return {k: data[k] for k in SECRET_KEYS}


def main() -> int:
    zip_path = build()

    upload = tccli("cos", "upload", "--bucket", COS_BUCKET,
                   "--local_path", str(zip_path), "--cos_key", COS_KEY, "--region", REGION)
    if upload.returncode != 0:
        print("上传代码包失败:", upload.stdout, upload.stderr)
        return 1

    env = {"Variables": [{"Key": k, "Value": v} for k, v in secrets().items()]}
    common = {
        "FunctionName": FUNCTION, "Namespace": NAMESPACE,
        "Handler": "index.handler", "Runtime": "Python3.10",
        "MemorySize": 512, "Timeout": 600,
        "Environment": env,
        "Code": {"CosBucketName": COS_BUCKET, "CosObjectName": "/" + COS_KEY,
                 "CosBucketRegion": REGION},
    }

    params = dict(common, Description=DESCRIPTION)
    update = tccli("scf", "UpdateFunction", "--region", REGION,
                   "--cli-input-json", "file://" + _write_params(params))
    if update.returncode != 0:
        params_path = _write_params(dict(common, Description=DESCRIPTION), suffix="create")
        create = tccli("scf", "CreateFunction", "--region", REGION,
                       "--cli-input-json", "file://" + params_path)
        if create.returncode != 0:
            print("部署失败:", create.stdout, create.stderr)
            return 1

    trigger = tccli("scf", "CreateTrigger", "--region", REGION,
                    "--FunctionName", FUNCTION, "--TriggerName", "daily", "--Type", "timer",
                    "--TriggerDesc", "0 0 20 * * * *", "--Enable", "OPEN", "--Namespace", NAMESPACE)
    if trigger.returncode != 0 and "已存在" not in (trigger.stdout + trigger.stderr):
        print("触发器:", (trigger.stdout or trigger.stderr)[:200])

    print("deployed", FUNCTION, "->", "Tencent SCF", REGION)
    return 0


def _write_params(params: dict, suffix: str = "update") -> str:
    path = Path(f"/tmp/ticket-archiver-scf-{suffix}.json")
    path.write_text(json.dumps(params, ensure_ascii=False))
    os.chmod(path, 0o600)
    return str(path)


if __name__ == "__main__":
    raise SystemExit(main())