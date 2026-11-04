"""全链路 trace 的统一序列化出口与落盘。

所有产物（span / 结果行）都必须走 dumps()：
sort_keys + 固定分隔符 + UTF-8 + LF，保证同配置连跑两次逐字节一致。
"""
from __future__ import annotations

import json
from collections.abc import Iterable
from pathlib import Path
from typing import Any

Record = dict[str, Any]


def dumps(record: Record) -> str:
    return json.dumps(
        record,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def write_jsonl(path: Path, records: Iterable[Record]) -> None:
    payload = "".join(dumps(record) + "\n" for record in records)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload.encode("utf-8"))


def read_jsonl(path: Path) -> list[Record]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line
    ]
