"""6 个业务工具：3 个读、3 个带副作用。

工具层只做最基本的数据存在性与参数检查，故意不执行业务策略：
策略由 grader 在终态上判定。若工具层提前拦截，「策略违规」这一类失败
根本无法出现，基线也就没有可归因的样本（对应设计文档「故意留坑」）。

call() 返回 (observation, error)，error 只取 wrong_tool / bad_args / None。
"""
from __future__ import annotations

import sqlite3
from typing import Any

TOOL_SCHEMAS: dict[str, dict[str, tuple[str, ...]]] = {
    "get_customer": {"required": ("customer_id",)},
    "get_order": {"required": ("order_id",)},
    "list_customer_orders": {"required": ("customer_id",)},
    "refund_order": {"required": ("order_id", "amount_cents")},
    "cancel_order": {"required": ("order_id",)},
    "notify_customer": {"required": ("customer_id", "message")},
}

WRONG_TOOL = "wrong_tool"
BAD_ARGS = "bad_args"

# 会写坏领域库状态的动作。运行时幂等键护栏、判分器的 state_write 事件、
# SeededBackend 的 duplicate_call 故障全部以此集合为唯一事实来源，
# 避免三处各写一份而在新增工具时漏改。
WRITE_TOOLS = frozenset({"refund_order", "cancel_order", "notify_customer"})

Observation = dict[str, Any]


def validate_args(name: str, args: Any) -> str | None:
    """只校验参数 schema，不判定工具名是否存在。

    工具层与 W3 运行时护栏共用这一份规则：护栏的价值不在于「多查了什么」，
    而在于把同样的检查从「工具层报错后放任动作丢失」前移到「拦下并触发重发」。
    未知工具名返回 None（放行），由调用方决定是否判定 wrong_tool。
    """
    schema = TOOL_SCHEMAS.get(name)
    if schema is None:
        return None
    if not isinstance(args, dict):
        return BAD_ARGS
    for field in schema["required"]:
        if field not in args or args[field] is None:
            return BAD_ARGS
    if name == "refund_order" and (
        not isinstance(args["amount_cents"], int)
        or isinstance(args["amount_cents"], bool)
    ):
        return BAD_ARGS
    if name == "notify_customer" and not isinstance(args["message"], str):
        return BAD_ARGS
    return None


def _validate(name: str, args: Any) -> str | None:
    if name not in TOOL_SCHEMAS:
        return WRONG_TOOL
    return validate_args(name, args)


def _fetchone(conn: sqlite3.Connection, sql: str, params: tuple) -> dict | None:
    record = conn.execute(sql, params).fetchone()
    return dict(record) if record is not None else None


def _write_log(
    conn: sqlite3.Connection,
    ref_key: str,
    action: str,
    amount_cents: int = 0,
    message: str = "",
) -> None:
    conn.execute(
        "INSERT INTO audit_log (ref_key, action, amount_cents, message) "
        "VALUES (?, ?, ?, ?)",
        (ref_key, action, amount_cents, message),
    )


def call(
    name: str, args: Any, conn: sqlite3.Connection
) -> tuple[Observation, str | None]:
    """执行一个工具调用；返回可 JSON 序列化的观察与错误类型。"""
    error = _validate(name, args)
    if error is not None:
        return {"ok": False, "error": error, "tool": name, "args": args}, error

    if name == "get_customer":
        row = _fetchone(conn, "SELECT * FROM customers WHERE id = ?", (args["customer_id"],))
        if row is None:
            return {"ok": False, "error": BAD_ARGS, "tool": name}, BAD_ARGS
        return {"ok": True, "customer": row}, None

    if name == "get_order":
        row = _fetchone(conn, "SELECT * FROM orders WHERE id = ?", (args["order_id"],))
        if row is None:
            return {"ok": False, "error": BAD_ARGS, "tool": name}, BAD_ARGS
        return {"ok": True, "order": row}, None

    if name == "list_customer_orders":
        records = conn.execute(
            "SELECT * FROM orders WHERE customer_id = ? ORDER BY id",
            (args["customer_id"],),
        ).fetchall()
        return {"ok": True, "orders": [dict(record) for record in records]}, None

    if name == "refund_order":
        order_id, amount = args["order_id"], args["amount_cents"]
        row = _fetchone(conn, "SELECT * FROM orders WHERE id = ?", (order_id,))
        if row is None:
            return {"ok": False, "error": BAD_ARGS, "tool": name}, BAD_ARGS
        conn.execute(
            "UPDATE orders SET refunded_cents = refunded_cents + ? WHERE id = ?",
            (amount, order_id),
        )
        _write_log(conn, str(order_id), "refund", amount)
        conn.commit()
        new_value = row["refunded_cents"] + amount
        return {"ok": True, "order_id": order_id, "refunded_cents": new_value}, None

    if name == "cancel_order":
        order_id = args["order_id"]
        row = _fetchone(conn, "SELECT * FROM orders WHERE id = ?", (order_id,))
        if row is None:
            return {"ok": False, "error": BAD_ARGS, "tool": name}, BAD_ARGS
        conn.execute("UPDATE orders SET status = 'cancelled' WHERE id = ?", (order_id,))
        _write_log(conn, str(order_id), "cancel")
        conn.commit()
        return {"ok": True, "order_id": order_id, "status": "cancelled"}, None

    if name == "notify_customer":
        message = args["message"]
        _write_log(conn, str(args["customer_id"]), "notify", 0, message)
        conn.commit()
        return {"ok": True, "customer_id": args["customer_id"], "message": message}, None

    return {"ok": False, "error": WRONG_TOOL, "tool": name}, WRONG_TOOL
