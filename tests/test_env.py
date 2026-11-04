"""领域环境单测：重置隔离、终态规范化、工具副作用与错误类型。"""
from __future__ import annotations

import unittest

from env import db, tools

INITIAL = {
    "customers": [
        {"id": "C-1", "name": "林悦", "email": "a@example.com", "tier": "standard"}
    ],
    "orders": [
        {
            "id": "O-100",
            "customer_id": "C-1",
            "status": "shipped",
            "total_cents": 4200,
            "refunded_cents": 0,
        },
        {
            "id": "O-101",
            "customer_id": "C-1",
            "status": "delivered",
            "total_cents": 8800,
            "refunded_cents": 0,
        },
    ],
    "audit_log": [],
}


class TestTemplateAndReset(unittest.TestCase):
    def test_each_trial_starts_from_same_snapshot(self):
        template = db.build_template(INITIAL)
        first = db.reset(template)
        tools.call(
            "refund_order", {"order_id": "O-100", "amount_cents": 500}, first
        )
        self.assertEqual(db.dump_state(first)["orders"][0]["refunded_cents"], 500)

        second = db.reset(template)
        self.assertEqual(db.dump_state(second)["orders"][0]["refunded_cents"], 0)
        self.assertEqual(db.dump_state(second)["audit_log"], [])
        self.assertEqual(db.dump_state(template), db.dump_state(second))
        for conn in (first, second, template):
            conn.close()

    def test_dump_sorts_rows_and_hides_autoincrement(self):
        template = db.build_template(INITIAL)
        conn = db.reset(template)
        tools.call(
            "refund_order", {"order_id": "O-101", "amount_cents": 100}, conn
        )
        state = db.dump_state(conn)
        self.assertEqual([row["id"] for row in state["orders"]], ["O-100", "O-101"])
        self.assertNotIn("seq", state["audit_log"][0])
        conn.close()
        template.close()

    def test_normalize_target_inherits_missing_tables_and_columns(self):
        normalized = db.normalize_target(
            {"orders": [{"id": "O-100", "refunded_cents": 2500}]}, INITIAL
        )
        self.assertEqual(len(normalized["customers"]), 1)
        self.assertEqual(normalized["orders"][0]["status"], "shipped")
        self.assertEqual(normalized["orders"][0]["total_cents"], 4200)
        self.assertEqual(normalized["orders"][0]["refunded_cents"], 2500)
        self.assertEqual(normalized["orders"][1]["refunded_cents"], 0)

    def test_normalize_target_overrides_audit_log(self):
        normalized = db.normalize_target(
            {"audit_log": [{"ref_key": "O-100", "action": "refund"}]}, INITIAL
        )
        self.assertEqual(len(normalized["audit_log"]), 1)
        self.assertEqual(normalized["audit_log"][0]["amount_cents"], 0)
        self.assertEqual(normalized["audit_log"][0]["message"], "")


class TestTools(unittest.TestCase):
    def setUp(self):
        self.template = db.build_template(INITIAL)
        self.conn = db.reset(self.template)

    def tearDown(self):
        self.conn.close()
        self.template.close()

    def test_refund_changes_state_and_writes_audit(self):
        observation, error = tools.call(
            "refund_order", {"order_id": "O-100", "amount_cents": 2500}, self.conn
        )
        self.assertIsNone(error)
        self.assertEqual(observation["refunded_cents"], 2500)
        state = db.dump_state(self.conn)
        self.assertEqual(state["orders"][0]["refunded_cents"], 2500)
        self.assertEqual(state["audit_log"][0]["action"], "refund")

    def test_env_deliberately_allows_repeated_refund(self):
        """工具层故意不拦重复退款；策略违规留给 grader 判定。"""
        for _ in range(2):
            tools.call(
                "refund_order",
                {"order_id": "O-100", "amount_cents": 2500},
                self.conn,
            )
        state = db.dump_state(self.conn)
        self.assertEqual(state["orders"][0]["refunded_cents"], 5000)
        self.assertEqual(len(state["audit_log"]), 2)

    def test_cancel_and_notify_have_side_effects(self):
        tools.call("cancel_order", {"order_id": "O-101"}, self.conn)
        tools.call(
            "notify_customer",
            {"customer_id": "C-1", "message": "已通知 O-101"},
            self.conn,
        )
        state = db.dump_state(self.conn)
        self.assertEqual(state["orders"][1]["status"], "cancelled")
        self.assertEqual(
            {row["action"] for row in state["audit_log"]}, {"cancel", "notify"}
        )

    def test_wrong_tool_and_bad_args(self):
        _, error = tools.call("refund_money", {"order_id": "O-100"}, self.conn)
        self.assertEqual(error, tools.WRONG_TOOL)
        _, error = tools.call("refund_order", {"order_id": "O-100"}, self.conn)
        self.assertEqual(error, tools.BAD_ARGS)
        _, error = tools.call("get_order", {"order_id": "NOPE"}, self.conn)
        self.assertEqual(error, tools.BAD_ARGS)

    def test_read_tools(self):
        observation, error = tools.call(
            "list_customer_orders", {"customer_id": "C-1"}, self.conn
        )
        self.assertIsNone(error)
        self.assertEqual(len(observation["orders"]), 2)

    def test_six_tools_registered(self):
        self.assertEqual(len(tools.TOOL_SCHEMAS), 6)
