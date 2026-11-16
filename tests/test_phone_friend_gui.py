import threading
import time
import unittest

from wechatauto.phone_friend_gui import (
    DESKTOP_VERSION,
    TaskController,
    TaskStopped,
    _account_for_process,
    _is_wechat_main_window,
    parse_task_values,
)


class PhoneFriendGuiTests(unittest.TestCase):
    def test_desktop_version_is_v1_6(self):
        self.assertEqual(DESKTOP_VERSION, "v1.6")

    def test_main_window_match_uses_exact_title_and_qt_class(self):
        self.assertTrue(_is_wechat_main_window("微信", "Qt51514QWindowIcon"))
        self.assertTrue(_is_wechat_main_window("Weixin", "Qt51514QWindowIcon"))
        self.assertFalse(_is_wechat_main_window("微信音视频通话", "Qt51514QWindowIcon"))
        self.assertFalse(_is_wechat_main_window("微信", "Qt51514WxTrayIconMessageWindowClass"))

    def test_account_for_process_uses_process_wxid_before_open_files(self):
        import wechatauto.db as db

        original = db.extract_master_key_from_cfg
        try:
            db.extract_master_key_from_cfg = lambda pid: (
                "00" * 32, 0, "wxid_od5pm0l069g312")
            account = _account_for_process(
                21724, None,
                {"wxid_od5pm0l069g312_215e", "wxid_old_a1b2"})
        finally:
            db.extract_master_key_from_cfg = original
        self.assertEqual(account, "wxid_od5pm0l069g312_215e")

    def test_parses_gui_values(self):
        message, label, interval, phones = parse_task_values(
            "申请语", "直充2", "5", "13800138000\n+86 13900139000")
        self.assertEqual(message, "申请语")
        self.assertEqual(label, "直充2")
        self.assertEqual(interval, 5.0)
        self.assertEqual(phones, ("13800138000", "13900139000"))

    def test_rejects_invalid_gui_values(self):
        with self.assertRaisesRegex(ValueError, "间隔"):
            parse_task_values("申请语", "直充2", "x", "13800138000")
        with self.assertRaisesRegex(ValueError, "手机号"):
            parse_task_values("申请语", "直充2", "5", "123")

    def test_allows_empty_message_and_label(self):
        message, label, interval, phones = parse_task_values(
            "", "", "5", "13800138000")
        self.assertEqual(message, "")
        self.assertEqual(label, "")
        self.assertEqual(interval, 5.0)
        self.assertEqual(phones, ("13800138000",))

    def test_controller_pauses_resumes_and_stops(self):
        controller = TaskController()
        controller.activate()
        self.assertTrue(controller.pause())
        passed = threading.Event()

        def wait_checkpoint():
            controller.checkpoint()
            passed.set()

        worker = threading.Thread(target=wait_checkpoint)
        worker.start()
        time.sleep(0.05)
        self.assertFalse(passed.is_set())
        self.assertTrue(controller.resume())
        worker.join(1)
        self.assertTrue(passed.is_set())
        controller.stop()
        with self.assertRaises(TaskStopped):
            controller.checkpoint()


if __name__ == "__main__":
    unittest.main()
