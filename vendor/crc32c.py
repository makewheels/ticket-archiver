"""纯 Python CRC32C（Castagnoli）。

tablestore SDK 依赖 crc32c C 扩展，但函数计算里只用到 `crc32c.crc32c()`，
用纯 Python 实现替换掉二进制依赖，保证代码包与平台无关。
"""

_POLY = 0x82F63B78
_TABLE = []
for _i in range(256):
    _c = _i
    for _ in range(8):
        _c = (_c >> 1) ^ _POLY if _c & 1 else _c >> 1
    _TABLE.append(_c)


def crc32c(data) -> int:
    crc = 0xFFFFFFFF
    table = _TABLE
    for b in data:
        crc = table[(crc ^ b) & 0xFF] ^ (crc >> 8)
    return crc ^ 0xFFFFFFFF