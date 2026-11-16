import unittest

from wechatauto.phone_friend import (
    PhoneAddFriendOps,
    collect_phones,
    mask_phone,
    normalize_phone,
)
from wechatauto.group_friend import append_remark, find_risk_control_text


class PhoneParsingTests(unittest.TestCase):
    def test_normalizes_mainland_phone(self):
        self.assertEqual(normalize_phone("+86 138-0013-8000"), "13800138000")
        self.assertEqual(normalize_phone("0086 13900139000"), "13900139000")

    def test_rejects_invalid_and_deduplicates_in_order(self):
        self.assertIsNone(normalize_phone("12345"))
        self.assertEqual(
            collect_phones(["13800138000, 13900139000", "+86 13800138000"]),
            ["13800138000", "13900139000"],
        )

    def test_masks_phone_for_logs(self):
        self.assertEqual(mask_phone("13800138000"), "138****8000")

    def test_appends_phone_to_existing_remark_once(self):
        self.assertEqual(
            append_remark("杨龙  ℡:15287270097", "13800138000"),
            "杨龙  ℡:15287270097 13800138000",
        )
        self.assertEqual(
            append_remark("杨龙 13800138000", "13800138000"),
            "杨龙 13800138000",
        )

    def test_detects_explicit_risk_control_text(self):
        self.assertEqual(
            find_risk_control_text(["添加好友操作频繁，请稍后再试"]),
            "添加好友操作频繁，请稍后再试",
        )
        self.assertIsNone(find_risk_control_text(["好友申请已发送"]))


class FakePhoneOps(PhoneAddFriendOps):
    def __init__(self):
        self.sent = 0
        self.closed = 0
        self.request_args = []

    def search_phone(self, phone, timeout=5.0):
        if phone == "13900139000":
            return {"ok": False, "reason": "no-result"}
        return {"ok": True, "reason": "ok", "row": object()}

    def open_phone_card(self, phone, row, timeout=6.0):
        return {"ok": True, "reason": "ok", "hwnd": 1, "card": object()}

    def card_state(self, card):
        return {"ok": True, "reason": "ok", "add": True, "names": []}

    def send_request(self, message=None, card_window=None, remark_suffix=None,
                     label=None):
        self.sent += 1
        self.request_args.append((message, remark_suffix, label))
        return {"ok": True, "reason": "ok", "sent": True}

    def close_transients(self):
        self.closed += 1

    def _clear_search(self):
        pass


class PhoneAddFriendTests(unittest.TestCase):
    def test_add_button_waits_for_stable_fresh_position(self):
        class Rect:
            def __init__(self, left, top, right, bottom):
                self.left = left
                self.top = top
                self.right = right
                self.bottom = bottom

        class AddButton:
            def __init__(self, rect):
                self.BoundingRectangle = Rect(*rect)

        class StableOps(PhoneAddFriendOps):
            add_button_settle_delay = 0.6
            add_button_poll_interval = 0.2
            add_button_stable_samples = 4
            add_button_wait_timeout = 3.0

            def __init__(self):
                self.positions = iter([
                    (100, 200, 260, 245),
                    (100, 200, 260, 245),
                    (100, 200, 260, 245),
                    (100, 200, 260, 245),
                    (100, 330, 260, 375),
                    (100, 330, 260, 375),
                    (100, 330, 260, 375),
                    (100, 330, 260, 375),
                    (100, 330, 260, 375),
                ])
                self.clicked = None
                self.now = 0.0

            def _monotonic(self):
                return self.now

            def _wait_for_layout(self, seconds):
                self.now += seconds

            def _fresh_add_button(self, card):
                return AddButton(next(self.positions))

            def _click(self, hwnd, ctrl, **kwargs):
                self.clicked = (hwnd, kwargs)
                return {"ok": True, "reason": "ok"}

        ops = StableOps()
        result = ops._activate_add_button(7, object(), object())

        self.assertTrue(result["ok"])
        self.assertEqual(result["activation"], "stable-coordinate")
        self.assertEqual(
            ops.clicked,
            (7, {"at": (180, 352), "raise_it": False}),
        )

    def test_dry_run_does_not_send(self):
        ops = FakePhoneOps()
        result = ops.add_phone("13800138000", dry_run=True)
        self.assertEqual(result["reason"], "would-add")
        self.assertEqual(ops.sent, 0)

    def test_one_failure_does_not_stop_later_numbers(self):
        ops = FakePhoneOps()
        results = ops.add_many(
            ["13800138000", "13900139000", "13700137000"],
            dry_run=False, interval=0,
        )
        self.assertEqual(len(results), 3)
        self.assertFalse(results[1]["ok"])
        self.assertTrue(results[2]["added"])
        self.assertEqual(ops.sent, 2)
        self.assertEqual(
            ops.request_args[0], (None, "13800138000", "直充2"))

    def test_maps_wechat_missing_user_message(self):
        class MissingUserOps(PhoneAddFriendOps):
            def _find(self, *args, **kwargs):
                return None

            def _find_by_aid(self, *args, **kwargs):
                return None

            def _names(self, *args, **kwargs):
                return ["添加朋友", "无法找到该用户，请检查你填写的账号是否正确。"]

        state = MissingUserOps().card_state(object())
        self.assertFalse(state["ok"])
        self.assertTrue(state["reason"].startswith("not-found"))

    def test_rechecks_card_once_after_loading_delay(self):
        class DelayedCardOps(PhoneAddFriendOps):
            def __init__(self):
                self.add_lookups = 0
                self.waits = 0

            def _find(self, card, names, **kwargs):
                if names == ("添加到通讯录",):
                    self.add_lookups += 1
                    if self.add_lookups == 2:
                        return object()
                return None

            def _find_by_aid(self, *_args):
                return None

            def _names(self, *_args):
                return ["添加朋友"]

            def _wait_for_card_load_retry(self):
                self.waits += 1

        ops = DelayedCardOps()
        state = ops.card_state(object())

        self.assertTrue(state["ok"])
        self.assertTrue(state["add"])
        self.assertEqual(ops.waits, 1)
        self.assertEqual(ops.add_lookups, 2)

    def test_unknown_card_retries_only_once(self):
        class StillLoadingOps(PhoneAddFriendOps):
            def __init__(self):
                self.waits = 0

            def _find(self, *_args, **_kwargs):
                return None

            def _find_by_aid(self, *_args):
                return None

            def _names(self, *_args):
                return ["添加朋友"]

            def _wait_for_card_load_retry(self):
                self.waits += 1

        ops = StillLoadingOps()
        state = ops.card_state(object())

        self.assertFalse(state["ok"])
        self.assertTrue(state["reason"].startswith("unknown-card"))
        self.assertEqual(ops.waits, 1)

if __name__ == "__main__":
    unittest.main()
