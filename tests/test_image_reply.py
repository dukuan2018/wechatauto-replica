import unittest
from types import SimpleNamespace

from wechatauto.demo_image_reply import (
    format_qr_reply,
    group_sender_name,
    is_incoming_image,
    message_key,
)


class ImageReplyTests(unittest.TestCase):
    def test_accepts_friend_image(self):
        msg = SimpleNamespace(type="image", attr="friend", sender_wxid="wxid_sender")
        self.assertTrue(is_incoming_image(msg, "wxid_me"))

    def test_skips_self_and_non_image(self):
        self.assertFalse(is_incoming_image(
            SimpleNamespace(type="image", attr="self", sender_wxid="wxid_me"),
            "wxid_me"))
        self.assertFalse(is_incoming_image(
            SimpleNamespace(type="text", attr="friend", sender_wxid="wxid_sender"),
            "wxid_me"))

    def test_message_key_includes_chat_and_shard(self):
        msg = SimpleNamespace(sort_seq=10, local_id=3, shard="message_1.db")
        chat = SimpleNamespace(who="wxid_sender")
        self.assertEqual(
            message_key(msg, chat),
            ("wxid_sender", 10, 3, "message_1.db"),
        )

    def test_formats_qr_reply_and_no_qr_reply(self):
        self.assertEqual(
            format_qr_reply(["https://yspapp.cn/GjY"]),
            "识别到二维码内容：\nhttps://yspapp.cn/GjY",
        )
        self.assertEqual(format_qr_reply([], detected=False), "收到，未识别到二维码")

    def test_group_sender_prefers_group_member_display_name(self):
        msg = SimpleNamespace(sender_wxid="wxid_sender", sender="旧昵称")
        chat = SimpleNamespace(GetGroupMembers=lambda: [{
            "username": "wxid_sender", "remark": "群备注", "nick_name": "昵称",
        }])
        self.assertEqual(group_sender_name(msg, chat), "群备注")

    def test_group_sender_skips_ambiguous_display_name(self):
        msg = SimpleNamespace(sender_wxid="wxid_sender", sender="旧昵称")
        chat = SimpleNamespace(GetGroupMembers=lambda: [
            {"username": "wxid_sender", "remark": "同名", "nick_name": "昵称"},
            {"username": "wxid_other", "remark": "同名", "nick_name": "另一个昵称"},
        ])
        self.assertEqual(group_sender_name(msg, chat), "")


if __name__ == "__main__":
    unittest.main()
