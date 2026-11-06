"""生成版本化扩展任务集 ``tasks/mvp_30.jsonl``（设计期工具，不是运行时依赖）。

构造规则（详见 tasks/README.md）：

* 前 10 行**逐字节**取自 ``mvp_10.jsonl``，含 7 条脚本固有缺陷任务，一个都不剔除；
* 后 20 行是新增的独立任务，脚本**全部正确**（不含坏 JSON / 错工具 / 缺参数 /
  超退 / 死循环），因此它们的成败只由「注入故障是否被护栏恢复」决定，
  是真正携带信息的测量位点；
* 20 条覆盖 3 笔订单、2 位客户、不同退款金额、单轮 / 多轮、2~5 个不等的
  脚本步数（= 不同数量的故障注入 cursor），以及读、退款、取消三类动作；
* 每条任务使用同一份 ``initial_db`` 快照（每个 trial 独立 reset），但
  ``task_id`` 进入 ``SeededBackend`` 的 SHA256 RNG 派生，随机流彼此独立。

纪律：``mvp_30.jsonl`` 是**版本化 fixture**，提交后不得再生成或修改；
``--check`` 只做一致性校验，不写文件。既有全部 W3 留档继续使用
``mvp_10.jsonl``，本文件不改动它们。

    python tasks/build_mvp_30.py            # 设计期生成（会覆盖产物）
    python tasks/build_mvp_30.py --check   # 只读校验，供守门测试调用
"""
from __future__ import annotations

import argparse
import copy
import json
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "tasks" / "mvp_10.jsonl"
TARGET = ROOT / "tasks" / "mvp_30.jsonl"

_BASE_LINES = SOURCE.read_text(encoding="utf-8").splitlines()
_INITIAL_DB = json.loads(_BASE_LINES[0])["initial_db"]

ORDERS: dict[str, dict[str, Any]] = {
    "O-100": {"customer_id": "C-1", "status": "shipped", "total_cents": 4200},
    "O-101": {"customer_id": "C-1", "status": "delivered", "total_cents": 8800},
    "O-102": {"customer_id": "C-2", "status": "shipped", "total_cents": 15000},
}
CUSTOMERS: dict[str, dict[str, str]] = {
    "C-1": {"name": "林悦", "tier": "standard"},
    "C-2": {"name": "陈默", "tier": "gold"},
}

REFUND_POLICIES = ["no_over_refund", "single_refund_per_order", "notify_after_refund"]
CANCEL_POLICIES = [
    "no_refund_after_cancel",
    "no_over_refund",
    "single_refund_per_order",
    "notify_after_refund",
]

# (id, order_id, amount_cents, prelude, multi_turn)
# prelude 是在退款之前插入的读工具，用来改变脚本长度与故障注入位点数量。
REFUND_SPECS: tuple[tuple[str, str, int, tuple[str, ...], bool], ...] = (
    ("T11", "O-100", 1200, ("get_order",), False),
    ("T12", "O-101", 3000, ("get_order", "get_customer"), False),
    ("T13", "O-102", 4500, ("get_order",), False),
    ("T14", "O-100", 800, (), False),
    ("T15", "O-101", 6000, ("list_orders",), False),
    ("T16", "O-102", 9000, ("get_customer",), False),
    ("T17", "O-100", 2000, (), True),
    ("T18", "O-101", 1500, ("get_order",), False),
    ("T19", "O-102", 2000, ("get_order", "get_customer"), False),
    ("T20", "O-100", 3600, (), False),
    ("T21", "O-101", 4200, ("list_orders",), False),
    ("T22", "O-102", 12000, (), True),
)

# (id, kind, ref)
READ_SPECS: tuple[tuple[str, str, str], ...] = (
    ("T23", "get_order", "O-100"),
    ("T24", "get_customer", "C-2"),
    ("T25", "list_orders", "C-1"),
    ("T26", "get_order_customer", "O-102"),
)

# (id, order_id, prelude)
CANCEL_SPECS: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("T27", "O-101", ()),
    ("T28", "O-100", ("get_order",)),
    ("T29", "O-102", ()),
    ("T30", "O-101", ("get_customer",)),
)


def _dump(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _prelude_action(kind: str, order_id: str, customer_id: str) -> dict[str, Any]:
    if kind == "get_order":
        return {"tool": "get_order", "args": {"order_id": order_id}}
    if kind == "get_customer":
        return {"tool": "get_customer", "args": {"customer_id": customer_id}}
    if kind == "list_orders":
        return {
            "tool": "list_customer_orders",
            "args": {"customer_id": customer_id},
        }
    raise ValueError(f"未知 prelude 类型：{kind}")


def _base(task_id: str, utterances: list[str]) -> dict[str, Any]:
    return {
        "id": task_id,
        "user_utterances": utterances,
        "initial_db": copy.deepcopy(_INITIAL_DB),
    }


def _refund_task(spec: tuple[str, str, int, tuple[str, ...], bool]) -> dict[str, Any]:
    task_id, order_id, amount, prelude, multi_turn = spec
    order = ORDERS[order_id]
    customer_id = str(order["customer_id"])
    notify_text = f"订单 {order_id} 已退款 {amount} 分"

    if multi_turn:
        utterances = [
            f"帮我查一下 {order_id} 的状态。",
            f"那帮我退 {amount} 分吧。",
        ]
        script: list[Any] = [
            {"tool": "get_order", "args": {"order_id": order_id}},
            {"final": f"订单 {order_id} 当前状态是 {order['status']}。"},
        ]
    else:
        utterances = [f"我要退 {order_id} 这笔订单，退 {amount} 分。"]
        script = [
            _prelude_action(kind, order_id, customer_id) for kind in prelude
        ]

    script += [
        {"tool": "refund_order", "args": {"order_id": order_id, "amount_cents": amount}},
        {"tool": "notify_customer", "args": {"customer_id": customer_id, "message": notify_text}},
        {"final": f"订单 {order_id} 已为您退款 {amount} 分，{notify_text}。"},
    ]

    task = _base(task_id, utterances)
    task["target_db"] = {
        "orders": [{"id": order_id, "refunded_cents": amount}],
        "audit_log": [
            {"ref_key": order_id, "action": "refund", "amount_cents": amount, "message": ""},
            {"ref_key": customer_id, "action": "notify", "amount_cents": 0, "message": notify_text},
        ],
    }
    task["policy_checks"] = list(REFUND_POLICIES)
    task["required_disclosures"] = [order_id, str(amount)]
    task["script"] = script
    return task


def _read_task(spec: tuple[str, str, str]) -> dict[str, Any]:
    task_id, kind, ref = spec
    task = _base(task_id, [])
    # target_db 为空：只读任务不产生任何写操作，终态应与初始库完全一致。
    task["target_db"] = {}
    task["policy_checks"] = list(REFUND_POLICIES)

    if kind == "get_order":
        order = ORDERS[ref]
        task["user_utterances"] = [f"帮我查一下 {ref} 的状态。"]
        task["script"] = [
            {"tool": "get_order", "args": {"order_id": ref}},
            {"final": f"订单 {ref} 当前状态是 {order['status']}。"},
        ]
        task["required_disclosures"] = [ref, str(order["status"])]
    elif kind == "get_customer":
        customer = CUSTOMERS[ref]
        task["user_utterances"] = [f"查一下客户 {ref} 的信息。"]
        task["script"] = [
            {"tool": "get_customer", "args": {"customer_id": ref}},
            {"final": f"客户 {ref} {customer['name']}，等级 {customer['tier']}。"},
        ]
        task["required_disclosures"] = [ref, customer["tier"]]
    elif kind == "list_orders":
        count = sum(1 for row in ORDERS.values() if row["customer_id"] == ref)
        task["user_utterances"] = [f"列出客户 {ref} 的所有订单。"]
        task["script"] = [
            {"tool": "list_customer_orders", "args": {"customer_id": ref}},
            {"final": f"客户 {ref} 共有 {count} 笔订单。"},
        ]
        task["required_disclosures"] = [ref, str(count)]
    elif kind == "get_order_customer":
        order = ORDERS[ref]
        customer = CUSTOMERS[str(order["customer_id"])]
        task["user_utterances"] = [f"查一下 {ref} 的状态和所属客户。"]
        task["script"] = [
            {"tool": "get_order", "args": {"order_id": ref}},
            {"tool": "get_customer", "args": {"customer_id": order["customer_id"]}},
            {"final": f"订单 {ref} 状态 {order['status']}，客户等级 {customer['tier']}。"},
        ]
        task["required_disclosures"] = [ref, str(order["status"]), customer["tier"]]
    else:
        raise ValueError(f"未知只读任务类型：{kind}")
    return task


def _cancel_task(spec: tuple[str, str, tuple[str, ...]]) -> dict[str, Any]:
    task_id, order_id, prelude = spec
    customer_id = str(ORDERS[order_id]["customer_id"])
    script: list[Any] = [
        _prelude_action(kind, order_id, customer_id) for kind in prelude
    ]
    script += [
        {"tool": "cancel_order", "args": {"order_id": order_id}},
        {"final": f"订单 {order_id} 已取消。"},
    ]
    task = _base(task_id, [f"帮我取消 {order_id} 这笔订单。"])
    task["target_db"] = {
        "orders": [{"id": order_id, "status": "cancelled"}],
        "audit_log": [
            {"ref_key": order_id, "action": "cancel", "amount_cents": 0, "message": ""}
        ],
    }
    task["policy_checks"] = list(CANCEL_POLICIES)
    task["required_disclosures"] = [order_id, "已取消"]
    task["script"] = script
    return task


def build_bytes() -> bytes:
    """确定性产出 ``mvp_30.jsonl`` 的完整字节。"""
    generated = [
        _dump(_refund_task(spec)) for spec in REFUND_SPECS
    ] + [
        _dump(_read_task(spec)) for spec in READ_SPECS
    ] + [
        _dump(_cancel_task(spec)) for spec in CANCEL_SPECS
    ]
    prefix = SOURCE.read_bytes()
    if not prefix.endswith(b"\n"):
        prefix += b"\n"
    return prefix + ("\n".join(generated) + "\n").encode("utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check", action="store_true", help="只读校验产物与构造规则是否一致"
    )
    args = parser.parse_args(argv)
    expected = build_bytes()
    if args.check:
        if not TARGET.is_file() or TARGET.read_bytes() != expected:
            raise SystemExit("mvp_30.jsonl 与构造规则不一致（或文件缺失）")
        print(f"OK: {TARGET.name} 与构造规则逐字节一致（{len(expected)} bytes）")
        return 0
    TARGET.write_bytes(expected)
    print(f"已生成 {TARGET.name}（{len(expected)} bytes）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
