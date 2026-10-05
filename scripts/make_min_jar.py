#!/usr/bin/env python3
"""make_min_jar.py —— 生成一个**最小合法**的 spider jar（几百字节）。

## 为什么需要

用户网络带宽很小（VPN / 跨境链路），而标准 spider jar 有 **915KB**
—— 客户端 `parseJar()` 会同步下载它，直接超时，导致「拉取配置失败」。
实测：配置只有 2.8KB 的 `/safe` 能加载，18KB 的 `/t4` 就超时。

如果客户端只是**要求 spider 字段存在**（哪怕用不到 jar 里的类），
那么把它指向一个几百字节的最小 dex jar 就能解决问题。

## 做什么

手写一个结构合法、**不含任何类**的 `classes.dex`，打进 jar：

    [0x00] dex header (112B)
    [0x70] map_list: size=1, entry{ type=0x0000(HEADER_ITEM), size=1, offset=0 }

dex 规范要求的校验：header 里
  - checksum  = adler32(除 checksum 字段外的全部字节)
  - signature = sha1(除 signature 字段外的全部字节)

用法：
  python scripts/make_min_jar.py public/spider-min.jar
"""
from __future__ import annotations

import hashlib
import io
import os
import struct
import sys
import zipfile
import zlib


def build_min_dex() -> bytes:
    """构造一个不含任何类的最小合法 dex。"""
    header_size = 0x70                      # dex header 固定 112 字节
    map_off = header_size                   # map_list 紧接 header
    # map_list = size(uint32) + N * entry{ type:uint16, unused:uint16, size:uint32, offset:uint32 }
    map_list = struct.pack("<I", 1) + struct.pack("<HHII", 0x0000, 0, 1, 0)
    file_size = header_size + len(map_list)

    # header 组装（先留空 checksum/signature，最后回填）
    header = b""
    header += b"dex\n035\0"                              # magic
    header += b"\x00" * 4                                # checksum   (稍后填)
    header += b"\x00" * 20                               # signature  (稍后填)
    header += struct.pack("<I", file_size)               # file_size
    header += struct.pack("<I", header_size)             # header_size
    header += struct.pack("<I", 0x12345678)              # endian_tag
    header += struct.pack("<II", 0, 0)                   # link_size, link_off
    header += struct.pack("<II", map_off, 0)             # map_off, string_ids_size
    header += struct.pack("<II", 0, 0)                   # string_ids_off, type_ids_size
    header += struct.pack("<II", 0, 0)                   # type_ids_off, proto_ids_size
    header += struct.pack("<II", 0, 0)                   # proto_ids_off, field_ids_size
    header += struct.pack("<II", 0, 0)                   # field_ids_off, method_ids_size
    header += struct.pack("<II", 0, 0)                   # method_ids_off, class_defs_size
    header += struct.pack("<II", 0, 0)                   # class_defs_off, data_size
    header += struct.pack("<II", 0, 0)                   # data_off, (padding)
    header = header[:header_size]
    assert len(header) == header_size, len(header)

    body = bytearray(header + map_list)

    # signature = sha1(从 signature 之后的全部字节)
    sig = hashlib.sha1(bytes(body[32:])).digest()
    body[12:32] = sig
    # checksum = adler32(从 checksum 之后的全部字节)
    ck = zlib.adler32(bytes(body[12:])) & 0xFFFFFFFF
    body[8:12] = struct.pack("<I", ck)
    return bytes(body)


def build_jar(dex: bytes) -> bytes:
    """把 dex 打包成 jar（zip）。MANIFEST 可选，保持最小。"""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("classes.dex", dex)
    return buf.getvalue()


def main() -> int:
    out = sys.argv[1] if len(sys.argv) > 1 else "public/spider-min.jar"
    dex = build_min_dex()
    jar = build_jar(dex)
    os.makedirs(os.path.dirname(out) or ".", exist_ok=True)
    with open(out, "wb") as f:
        f.write(jar)
    print(f"[min-jar] dex={len(dex)}B  jar={len(jar)}B  -> {out}")
    # 自检：dex 头部 magicsz 与 file_size 自洽
    assert dex[:8] == b"dex\n035\0"
    assert struct.unpack("<I", dex[32:36])[0] == len(dex)
    print("[min-jar] 自检通过：magic 正确、file_size 自洽")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
