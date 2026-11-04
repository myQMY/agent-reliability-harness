"""领域库的模板快照、per-trial 重置与终态 dump。

只用标准库。所有连接都是内存 SQLite：模板由任务的 initial_db 建出且此后只读，
每个 trial 用 Connection.backup() 复制一份，保证 trial 之间零残留。

audit_log 的自增列 seq 不进入 dump，避免把它当成业务语义；
所有表按固定键排序后输出，保证逐字段 diff 与磁盘顺序无关。
"""
from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any, Iterable, Mapping

SCHEMA_PATH = Path(__file__).with_name("schema.sql")

TABLE_COLUMNS: dict[str, tuple[str, ...]] = {
    "customers": ("id", "name", "email", "tier"),
    "orders": ("id", "customer_id", "status", "total_cents", "refunded_cents"),
    "audit_log": ("ref_key", "action", "amount_cents", "message"),
}

SORT_KEYS: dict[str, tuple[str, ...]] = {
    "customers": ("id",),
    "orders": ("id",),
    "audit_log": ("ref_key", "action", "amount_cents", "message"),
}

_DEFAULTS: dict[str, Any] = {
    "refunded_cents": 0,
    "amount_cents": 0,
    "message": "",
}

State = dict[str, list[dict[str, Any]]]


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    return conn


def build_template(initial_db: Mapping[str, Iterable[Mapping[str, Any]]]) -> sqlite3.Connection:
    """按任务给出的初始库建出不可变模板。模板此后不再写入。"""
    conn = _connect()
    conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
    for table, columns in TABLE_COLUMNS.items():
        statement = (
            f"INSERT INTO {table} ({', '.join(columns)}) "
            f"VALUES ({', '.join('?' for _ in columns)})"
        )
        for row in initial_db.get(table, ()):
            conn.execute(
                statement,
                [row.get(col, _DEFAULTS.get(col, "")) for col in columns],
            )
    conn.commit()
    return conn


def reset(template: sqlite3.Connection) -> sqlite3.Connection:
    """从同一模板复制出本 trial 的独立库。"""
    conn = _connect()
    template.backup(conn)
    return conn


def _sorted_rows(rows: list[dict[str, Any]], table: str) -> list[dict[str, Any]]:
    rows.sort(key=lambda row: tuple(row[col] for col in SORT_KEYS[table]))
    return rows


def dump_state(conn: sqlite3.Connection) -> State:
    """把全库 dump 成可逐字段比对的结构。"""
    state: State = {}
    for table, columns in TABLE_COLUMNS.items():
        sql = f"SELECT {', '.join(columns)} FROM {table}"
        rows = [
            {col: record[col] for col in columns}
            for record in conn.execute(sql).fetchall()
        ]
        state[table] = _sorted_rows(rows, table)
    return state


def _rows(
    raw_db: Mapping[str, Iterable[Mapping[str, Any]]], table: str
) -> list[dict[str, Any]]:
    columns = TABLE_COLUMNS[table]
    return [
        {col: raw.get(col, _DEFAULTS.get(col, "")) for col in columns}
        for raw in raw_db.get(table, ())
    ]


def normalize_target(
    target_db: Mapping[str, Iterable[Mapping[str, Any]]],
    initial_db: Mapping[str, Iterable[Mapping[str, Any]]] | None = None,
) -> State:
    """把任务里的目标终态规范化成与 dump_state 完全相同的形状。

    任务只需写出发生变化的行；未提及的表 / 行从 initial_db 继承，
    audit_log 若出现则整体覆盖（初始通常为空）。
    """
    baseline: Mapping[str, Iterable[Mapping[str, Any]]] = initial_db or {}
    state: State = {
        table: _sorted_rows(_rows(baseline, table), table)
        for table in TABLE_COLUMNS
    }
    for table in TABLE_COLUMNS:
        if table not in target_db:
            continue
        raw_rows = list(target_db[table])
        columns = TABLE_COLUMNS[table]
        if table == "audit_log":
            state[table] = [
                {col: raw.get(col, _DEFAULTS.get(col, "")) for col in columns}
                for raw in raw_rows
            ]
        else:
            key = SORT_KEYS[table][0]
            positions = {row[key]: index for index, row in enumerate(state[table])}
            for raw in raw_rows:
                key_value = raw.get(key)
                if key_value in positions:
                    merged = dict(state[table][positions[key_value]])
                    for col in columns:
                        if col in raw:
                            merged[col] = raw[col]
                    state[table][positions[key_value]] = merged
                else:
                    state[table].append(
                        {col: raw.get(col, _DEFAULTS.get(col, "")) for col in columns}
                    )
        state[table] = _sorted_rows(state[table], table)
    return state
