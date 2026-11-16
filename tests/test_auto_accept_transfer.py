import unittest

from wechatauto.demo_auto_accept_transfer import parse_incoming_transfer


INCOMING = """
<msg><appmsg><type>2000</type><wcpayinfo>
<paysubtype>1</paysubtype>
<feedesc><![CDATA[￥54.00]]></feedesc>
<transferid><![CDATA[transfer-1]]></transferid>
<receiver_username><![CDATA[wxid_me]]></receiver_username>
</wcpayinfo></appmsg></msg>
"""


class AutoAcceptTransferTests(unittest.TestCase):
    def test_accepts_incoming_transfer_for_self(self):
        self.assertEqual(
            parse_incoming_transfer(INCOMING, "wxid_me", "wxid_sender"),
            {
                "transfer_id": "transfer-1",
                "transaction_id": "",
                "amount": "￥54.00",
                "receiver": "wxid_me",
            },
        )

    def test_rejects_own_echo_and_wrong_subtype(self):
        self.assertIsNone(parse_incoming_transfer(INCOMING, "wxid_me", "wxid_me"))
        own = INCOMING.replace("<paysubtype>1</paysubtype>",
                               "<paysubtype>3</paysubtype>")
        self.assertIsNone(parse_incoming_transfer(own, "wxid_me", "wxid_sender"))


if __name__ == "__main__":
    unittest.main()
