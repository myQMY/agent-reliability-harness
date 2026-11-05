"""终态判分单测：正例 / 反例、策略检查器、告知检查与固定失败枚举。"""
from __future__ import annotations

import unittest

from env import db, tools
from eval import grader

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

TASK = {
    "id": "T",
    "user_utterances": ["退款"],
    "initial_db": INITIAL,
    "target_db": {
        "orders": [{"id": "O-100", "refunded_cents": 2500}],
        "audit_log": [
            {
                "ref_key": "O-100",
                "action": "refund",
                "amount_cents": 2500,
                "message": "",
            },
            {
                "ref_key": "C-1",
                "action": "notify",
                "amount_cents": 0,
                "message": "订单 O-100 已退款 2500 分",
            },
        ],
    },
    "policy_checks": [
        "no_over_refund",
        "single_refund_per_order",
        "notify_after_refund",
    ],
    "required_disclosures": ["O-100", "2500"],
}

FINAL = "订单 O-100 已为您退款 2500 分。"
CORRECT = [
    ("refund_order", {"order_id": "O-100", "amount_cents": 2500}),
    ("notify_customer", {"customer_id": "C-1", "message": "订单 O-100 已退款 2500 分"}),
]


def ok_run(**overrides):
    info = {"termination": "final", "errors": [], "final_text": FINAL}
    info.update(overrides)
    return info


def state_after(actions):
    template = db.build_template(INITIAL)
    conn = db.reset(template)
    for name, args in actions:
        tools.call(name, args, conn)
    state = db.dump_state(conn)
    conn.close()
    template.close()
    return state


class TestGrade(unittest.TestCase):
    def test_positive_case(self):
        result = grader.grade(state_after(CORRECT), TASK, ok_run())
        self.assertEqual(result["passed"], 1)
        self.assertEqual(result["failure_type"], "none")

    def test_negative_over_refund_is_policy_violation(self):
        state = state_after(
            [
                ("refund_order", {"order_id": "O-100", "amount_cents": 9999}),
                ("notify_customer", {"customer_id": "C-1", "message": "订单 O-100 已退款"}),
            ]
        )
        result = grader.grade(state, TASK, ok_run())
        self.assertEqual(result["passed"], 0)
        self.assertEqual(result["failure_type"], "policy_violation")

    def test_negative_missing_disclosure(self):
        result = grader.grade(
            state_after(CORRECT), TASK, ok_run(final_text="办好了")
        )
        self.assertEqual(result["passed"], 0)
        self.assertEqual(result["failure_type"], "intent_misread")
        self.assertTrue(
            any(r.startswith("missing_disclosures") for r in result["reasons"])
        )

    def test_termination_drives_failure_type(self):
        base = db.normalize_target({}, INITIAL)
        task = dict(TASK, target_db={}, required_disclosures=[])
        for termination, expected in (
            ("json_failure", "output_format"),
            ("max_steps", "loop_or_timeout"),
        ):
            with self.subTest(termination=termination):
                result = grader.grade(
                    base, task, ok_run(termination=termination, final_text="")
                )
                self.assertEqual(result["passed"], 0)
                self.assertEqual(result["failure_type"], expected)

    def test_wrong_tool_and_bad_args_attribution(self):
        untouched = db.normalize_target({}, INITIAL)
        for error, expected in (("wrong_tool", "wrong_tool"), ("bad_args", "bad_args")):
            with self.subTest(error=error):
                result = grader.grade(
                    untouched, TASK, ok_run(errors=[error], final_text=FINAL)
                )
                self.assertEqual(result["failure_type"], expected)

    def test_policy_checkers_individually(self):
        over = {
            "customers": [],
            "orders": [{"status": "shipped", "total_cents": 100, "refunded_cents": 150}],
            "audit_log": [],
        }
        self.assertFalse(grader.POLICY_CHECKERS["no_over_refund"](over))

        cancelled = {
            "customers": [],
            "orders": [{"status": "cancelled", "total_cents": 100, "refunded_cents": 10}],
            "audit_log": [],
        }
        self.assertFalse(grader.POLICY_CHECKERS["no_refund_after_cancel"](cancelled))

        twice = {
            "customers": [],
            "orders": [],
            "audit_log": [
                {"ref_key": "O-100", "action": "refund", "message": "", "amount_cents": 1},
                {"ref_key": "O-100", "action": "refund", "message": "", "amount_cents": 1},
            ],
        }
        self.assertFalse(grader.POLICY_CHECKERS["single_refund_per_order"](twice))

        no_notify = {
            "customers": [],
            "orders": [],
            "audit_log": [
                {"ref_key": "O-100", "action": "refund", "message": "", "amount_cents": 1}
            ],
        }
        self.assertFalse(grader.POLICY_CHECKERS["notify_after_refund"](no_notify))

        notified = {
            "customers": [],
            "orders": [],
            "audit_log": [
                {"ref_key": "O-100", "action": "refund", "message": "", "amount_cents": 1},
                {"ref_key": "C-1", "action": "notify", "message": "订单 O-100 已退款", "amount_cents": 0},
            ],
        }
        self.assertTrue(grader.POLICY_CHECKERS["notify_after_refund"](notified))

    def test_failure_types_are_fixed_enum(self):
        self.assertEqual(
            set(grader.FAILURE_TYPES),
            {
                "intent_misread",
                "wrong_tool",
                "bad_args",
                "policy_violation",
                "loop_or_timeout",
                "output_format",
                "unknown",
            },
        )
