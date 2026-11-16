import unittest

from wechatauto.wx import _AllMessageChat, _resolve_wxid


class GlobalChatTargetTests(unittest.TestCase):
    def test_global_chat_factory_can_use_display_name_for_gui(self):
        created = []

        def factory(username, display="南瓜皮"):
            created.append((username, display))
            return {"gui_target": display, "db_username": username}

        observed = _AllMessageChat(
            "wxid_cypd7iklidsh29", "南瓜皮", factory=factory).chat
        self.assertEqual(observed["gui_target"], "南瓜皮")
        self.assertEqual(observed["db_username"], "wxid_cypd7iklidsh29")
        self.assertEqual(created, [("wxid_cypd7iklidsh29", "南瓜皮")])

    def test_skips_ambiguous_target_without_gui_search_name(self):
        chat = _AllMessageChat("wxid_same_name", "同名", factory=lambda _: None)
        result = chat.SendMsg("收到")
        self.assertFalse(result)
        self.assertIn("重名", result["message"])

    def test_resolves_alias_to_internal_username(self):
        class FakeDB:
            def search_contact(self, _name):
                return [{
                    "username": "wxid_sender",
                    "nick_name": "张三",
                    "remark": "",
                    "alias": "zhangsan_123",
                }]

        self.assertEqual(_resolve_wxid(FakeDB(), "zhangsan_123"), "wxid_sender")


if __name__ == "__main__":
    unittest.main()
