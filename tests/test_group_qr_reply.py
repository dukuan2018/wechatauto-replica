import os
import tempfile
import unittest

import cv2

from wechatauto.demo_group_qr_reply import decode_qr, format_reply


class GroupQrReplyTests(unittest.TestCase):
    def test_formats_reply_with_sender_name_without_mention(self):
        self.assertEqual(
            format_reply("\u5357\u74dc\u7c7d", ["https://yspapp.cn/GjY"]),
            "\u4f60\u597d\uff1a\u5357\u74dc\u7c7d\n"
            "\u8bc6\u522b\u5230\u4e8c\u7ef4\u7801\u5185\u5bb9\uff1a\n"
            "https://yspapp.cn/GjY",
        )

    def test_decodes_small_qr_image(self):
        payload = "https://example.com/qr-test"
        image = cv2.QRCodeEncoder_create().encode(payload)
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "qr.png")
            self.assertTrue(cv2.imwrite(path, image))
            self.assertEqual(decode_qr(path), [payload])


if __name__ == "__main__":
    unittest.main()
