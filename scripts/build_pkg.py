#!/usr/bin/env python3
"""构建函数计算代码包（纯 Python，无平台相关二进制）。

用法：python3 scripts/build_pkg.py
产物：build/ticket-archiver.zip
"""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BUILD = ROOT / "build"
PKG = BUILD / "pkg"
ZIP = BUILD / "ticket-archiver.zip"

# aiohttp 只被异步客户端使用，同步调用下改为可选，省掉 10MB 依赖
AIOHTTP_OPTIONAL = '''try:
    import aiohttp
    from aiohttp import ClientTimeout
except ImportError:  # 同步客户端不需要 aiohttp
    aiohttp = None

    class ClientTimeout(object):
        def __init__(self, *a, **kw):
            pass
'''


def _find(pattern: str):
    return list(PKG.rglob(pattern))


def build() -> Path:
    shutil.rmtree(BUILD, ignore_errors=True)
    PKG.mkdir(parents=True)

    subprocess.run(
        [sys.executable, "-m", "pip", "install", "-q", "--no-compile",
         "--target", str(PKG), "-r", str(ROOT / "fc" / "requirements.txt")],
        check=True,
    )

    # 1) 删掉所有平台相关二进制：改用纯 Python 回退
    for pattern in ("*.so", "*.pyd", "*.dylib", "*.dll"):
        for path in _find(pattern):
            path.unlink()

    # 2) 去掉用不到的重依赖（numpy 仅搜索/向量场景使用）
    for rel in ("numpy", "tablestore/ots_sdk", "tablestore/native", "crc32c"):
        shutil.rmtree(PKG / rel, ignore_errors=True)
    for dist in PKG.glob("*.dist-info"):
        shutil.rmtree(dist, ignore_errors=True)
    for cache in _find("__pycache__"):
        shutil.rmtree(cache, ignore_errors=True)

    # 3) 纯 Python CRC32C
    shutil.copy(ROOT / "vendor" / "crc32c.py", PKG / "crc32c.py")

    # 4) aiohttp 改为可选
    conn = PKG / "tablestore" / "connection.py"
    source = conn.read_text()
    patched, n = re.subn(r"import aiohttp\nfrom aiohttp import ClientTimeout\n",
                         AIOHTTP_OPTIONAL, source, count=1)
    if n != 1:
        raise SystemExit("tablestore/connection.py 结构与预期不符，patch 失败")
    conn.write_text(patched)

    # 5) 入口
    shutil.copy(ROOT / "fc" / "index.py", PKG / "index.py")

    with zipfile.ZipFile(ZIP, "w", zipfile.ZIP_DEFLATED) as zf:
        for path in sorted(PKG.rglob("*")):
            if path.is_file():
                zf.write(path, path.relative_to(PKG))

    size = ZIP.stat().st_size / 1024 / 1024
    print(f"built {ZIP} ({size:.1f} MiB)")
    return ZIP


if __name__ == "__main__":
    build()