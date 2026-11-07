"""群聊 @ 输入失败时应明确停止，不能误报为成员未找到。"""
import unittest
from unittest.mock import Mock, patch

from wechatauto.guia import WeChatGUI, VK_V


class AtMemberInputTests(unittest.TestCase):
    def setUp(self):
        self.gui = object.__new__(WeChatGUI)
        self.gui.ensure_visible = Mock(return_value=True)
        self.gui.open_chat = Mock(return_value=True)
        self.gui.get_input_box = Mock(return_value=(300, 800, 1000, 950))
        self.gui.focus_input = Mock(return_value=True)
        self.gui._input_box_has_text = Mock(side_effect=[False, False])
        self.gui.set_clipboard = Mock(return_value=True)
        self.gui._input = Mock()
        self.gui.right_pane_left = 300
        self.gui.render_w = 1000
        self.gui.render_h = 1000
        self.gui.origin_x = 100
        self.gui.origin_y = 100
        self.gui.ocr = Mock(return_value=[])
        self.gui.wx_click = Mock()
        self.gui.input_text = Mock(return_value=True)
        self.gui.click_send = Mock()
        patcher = patch("wechatauto.guia.time.sleep")
        patcher.start()
        self.addCleanup(patcher.stop)
        patcher = patch("wechatauto.guia.rhythm.point", return_value=(400, 300))
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_open_chat_failure_stops_before_typing(self):
        self.gui.open_chat.return_value = False

        result = self.gui.at_member("张三", "请看一下", who="测试群")

        self.assertFalse(result.is_success)
        self.gui.get_input_box.assert_not_called()
        self.gui.set_clipboard.assert_not_called()

    def test_pastes_at_before_searching_member_popup(self):
        self.gui._input_box_has_text.side_effect = [False, True]
        self.gui.ocr.return_value = [("张三", 400, 300, 40, 20)]

        result = self.gui.at_member("张三", "请看一下", who="测试群")

        self.assertTrue(result.is_success)
        self.gui.set_clipboard.assert_called_once_with("@")
        self.gui._input.key.assert_called_once_with(VK_V, ctrl=True)
        self.gui.ocr.assert_called_once()
        self.gui.click_send.assert_called_once()

    def test_reports_missing_at_input_separately(self):
        result = self.gui.at_member("张三", "请看一下", who="测试群")

        self.assertFalse(result.is_success)
        self.assertIn("未确认 @ 已输入", result["message"])
        self.gui.ocr.assert_not_called()
        self.gui.click_send.assert_not_called()


if __name__ == "__main__":
    unittest.main()
