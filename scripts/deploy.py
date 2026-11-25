#!/usr/bin/env python3
"""部署 ticket-archiver 到阿里云函数计算。

前置：
  - ~/.config/ticket-archiver/deploy-credentials.json（0600，部署 RAM 用户 AK）
  - Infisical tools/prod /ticket-archiver 中有邮箱与飞书凭据
  - 已存在 RAM 角色 ticket-archiver-fc

用法：python3 scripts/deploy.py
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from build_pkg import build  # noqa: E402

REGION = "cn-beijing"
ACCOUNT = "1618784280874658"
FUNCTION = "ticket-archiver"
ROLE = f"acs:ram::{ACCOUNT}:role/ticket-archiver-fc"
CODE_BUCKET = "ticket-archiver-code"
CODE_OBJECT = "fc/ticket-archiver.zip"
INFISICAL_PROJECT = "79c61872-d992-44c7-a5ae-d3e70e004e7f"
SECRET_KEYS = ["EMAIL_ACCOUNT", "EMAIL_AUTH_CODE", "FEISHU_APP_ID", "FEISHU_APP_SECRET", "FEISHU_CHAT_ID"]


def sh(*args: str, env: dict | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(list(args), capture_output=True, text=True, env=env)


def secrets() -> dict:
    out = sh("infisical", "secrets-manager", "secrets", "get",
             "--projectId", INFISICAL_PROJECT, "--env", "prod", "--path", "/ticket-archiver",
             "--output", "json", "--domain=https://secrets.a4.fit")
    data = {s["secretKey"]: s["secretValue"] for s in json.loads(out.stdout)}
    return {k: data[k] for k in SECRET_KEYS}


def main() -> int:
    cred = json.loads((Path.home() / ".config/ticket-archiver/deploy-credentials.json").read_text())
    env = dict(os.environ,
               ALIBABA_CLOUD_ACCESS_KEY_ID=cred["access_key_id"],
               ALIBABA_CLOUD_ACCESS_KEY_SECRET=cred["access_key_secret"])

    zip_path = build()

    step = sh("aliyun", "ossutil", "cp", str(zip_path),
              f"oss://{CODE_BUCKET}/{CODE_OBJECT}", "-f", env=env)
    if step.returncode != 0:
        print(step.stdout, step.stderr)
        return 1

    fn_env = json.dumps(secrets(), ensure_ascii=False)
    common = ["aliyun", "fc"]
    args = ["--region", REGION, "--function-name", FUNCTION,
            "--runtime", "python3.12", "--handler", "index.handler",
            "--code", json.dumps({"ossBucketName": CODE_BUCKET, "ossObjectName": CODE_OBJECT}),
            "--role", ROLE, "--timeout", "600", "--memory-size", "512",
            "--internet-access", "true",
            "--description", "12306 发票归档：QQ 邮箱 → 解析 → OSS + Tablestore",
            "--environment-variables", fn_env]

    step = sh(*common, "update-function", *args, env=env)
    if step.returncode != 0:  # 首次部署
        step = sh(*common, "create-function", *args, env=env)
    if step.returncode != 0:
        print("deploy failed:", step.stdout, step.stderr)
        return 1

    trigger = json.dumps({"payload": "", "cronExpression": "0 0 12 * * *", "enable": True})
    sh(*common, "create-trigger", "--region", REGION, "--function-name", FUNCTION,
       "--trigger-name", "daily", "--trigger-type", "timer", "--trigger-config", trigger, env=env)

    print("deployed", FUNCTION)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())