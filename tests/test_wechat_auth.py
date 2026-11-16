import json
import unittest
from unittest.mock import patch

from wechatauto.wechat_auth import (
    WechatAuthClient, WechatAuthError, WechatQuotaError,
)


class Response:
    def __init__(self, payload, content_type="application/json"):
        self.payload = payload
        self.headers = {"Content-Type": content_type}

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self, _limit=-1):
        return self.payload


class WechatAuthTests(unittest.TestCase):
    def test_default_backend_url_uses_production_api_prefix(self):
        client = WechatAuthClient()
        self.assertEqual(client.base_url, "https://discount.yidaw.cn/prod-api")

    def test_create_qr_uses_service_account_oauth_url_and_generates_qr(self):
        payload = json.dumps({
            "code": 200,
            "url": "https://open.weixin.qq.com/connect/oauth2/authorize?appid=a&state=state-1#wechat_redirect",
            "state": "state-1",
        }).encode()
        client = WechatAuthClient("http://localhost:8080")
        with patch("wechatauto.wechat_auth.urllib.request.urlopen",
                   return_value=Response(payload)) as open_url:
            qr = client.create_qr()
            image = client.qr_image_bytes(qr.image)

        self.assertEqual(qr.ticket, "state-1")
        self.assertTrue(image.startswith(b"\x89PNG\r\n\x1a\n"))
        request = open_url.call_args.args[0]
        self.assertEqual(request.full_url, "http://localhost:8080/wxMpAuthUrl")
        self.assertEqual(request.get_method(), "GET")

    def test_poll_uses_backend_oauth_state_status_endpoint(self):
        client = WechatAuthClient("http://localhost:8080")
        with patch("wechatauto.wechat_auth.urllib.request.urlopen",
                   return_value=Response(b'{"code":200,"status":"WAITING"}')) as open_url:
            result = client.poll("state-1")

        request = open_url.call_args.args[0]
        self.assertEqual(
            request.full_url,
            "http://localhost:8080/wxMpLoginStatus?state=state-1")
        self.assertEqual(result["status"], "WAITING")

    def test_resolves_confirmed_user_from_ruoyi_user_fields(self):
        client = WechatAuthClient("http://localhost:8080")
        user = client.resolve_user("token-1", {
            "fullServiceAuthorized": True,
            "user": {"nickName": "南瓜", "avatar": "/profile/avatar.png"}
        })
        self.assertEqual(user.token, "token-1")
        self.assertEqual(user.nickname, "南瓜")
        self.assertEqual(user.avatar, "/profile/avatar.png")

    def test_resolve_user_does_not_substitute_existing_backend_profile(self):
        client = WechatAuthClient("http://localhost:8080")
        with patch.object(client, "user_info") as user_info:
            with self.assertRaisesRegex(WechatAuthError, "使用完整服务"):
                client.resolve_user("token-1", {
                    "fullServiceAuthorized": True, "token": "token-1"
                })
        user_info.assert_not_called()

    def test_resolve_user_requires_backend_full_service_confirmation(self):
        client = WechatAuthClient("http://localhost:8080")
        with self.assertRaisesRegex(WechatAuthError, "新版后台接口"):
            client.resolve_user("token-1", {
                "token": "token-1",
                "user": {"nickName": "南瓜", "avatar": "/old.png"}
            })

    def test_get_info_reads_ruoyi_top_level_user(self):
        client = WechatAuthClient("http://localhost:8080")
        payload = b'{"code":200,"user":{"nickName":"Pumpkin","avatar":"/avatar.png"}}'
        with patch("wechatauto.wechat_auth.urllib.request.urlopen",
                   return_value=Response(payload)) as open_url:
            user = client.user_info("token-1")

        request = open_url.call_args.args[0]
        self.assertEqual(request.full_url, "http://localhost:8080/getInfo")
        self.assertEqual(request.get_header("Authorization"), "Bearer token-1")
        self.assertEqual(user["nickName"], "Pumpkin")

    def test_resolve_user_requires_full_wechat_profile(self):
        client = WechatAuthClient("http://localhost:8080")
        with self.assertRaisesRegex(WechatAuthError, "使用完整服务"):
            client.resolve_user("token-1", {
                "fullServiceAuthorized": True,
                "user": {"nickName": "南瓜", "avatar": ""}
            })
    def test_logout_uses_ruoyi_security_logout(self):
        client = WechatAuthClient("http://localhost:8080")
        with patch("wechatauto.wechat_auth.urllib.request.urlopen",
                   return_value=Response(b'{"code":200}')) as open_url:
            client.logout("token-1")

        request = open_url.call_args.args[0]
        self.assertEqual(request.full_url, "http://localhost:8080/logout")
        self.assertEqual(request.get_method(), "POST")
        self.assertEqual(request.get_header("Authorization"), "Bearer token-1")

    def test_helper_user_info_sends_token_without_wxid(self):
        client = WechatAuthClient("http://localhost:8080")
        payload = b'{"code":200,"data":{"balance":4}}'
        with patch("wechatauto.wechat_auth.urllib.request.urlopen",
                   return_value=Response(payload)) as open_url:
            info = client.helper_user_info("token-1")

        request = open_url.call_args.args[0]
        self.assertEqual(request.full_url, "http://localhost:8080/wechat/helper/userInfo")
        self.assertEqual(request.get_header("Authorization"), "Bearer token-1")
        self.assertEqual(info["balance"], 4)

    def test_client_config_uses_authenticated_helper_endpoint(self):
        client = WechatAuthClient("http://localhost:8080")
        payload = b'{"code":200,"data":{"version":"v1.6","customerWechat":"wx-test"}}'
        with patch("wechatauto.wechat_auth.urllib.request.urlopen",
                   return_value=Response(payload)) as open_url:
            config = client.client_config("token-1")

        request = open_url.call_args.args[0]
        self.assertEqual(request.full_url,
                         "http://localhost:8080/wechat/helper/clientConfig")
        self.assertEqual(request.get_header("Authorization"), "Bearer token-1")
        self.assertEqual(config["version"], "v1.6")

    def test_qr_code_bytes_generates_png_from_text(self):
        client = WechatAuthClient("http://localhost:8080")
        image = client.qr_code_bytes("customer support payload")
        self.assertTrue(image.startswith(b"\x89PNG\r\n\x1a\n"))

    def test_report_result_posts_idempotency_payload(self):
        client = WechatAuthClient("http://localhost:8080")
        body = {"requestId": "r-1", "sendStatus": 1}
        with patch("wechatauto.wechat_auth.urllib.request.urlopen",
                   return_value=Response(b'{"code":200,"data":{"remainingPoints":3}}')) as open_url:
            result = client.report_phone_result("token-1", body)

        request = open_url.call_args.args[0]
        self.assertEqual(request.full_url, "http://localhost:8080/wechat/helper/record")
        self.assertEqual(json.loads(request.data.decode("utf-8")), body)
        self.assertEqual(result["remainingPoints"], 3)

    def test_quota_conflict_is_reported_as_quota_error(self):
        client = WechatAuthClient("http://localhost:8080")
        payload = json.dumps({"code": 409, "msg": "额度不足，请联系客服"},
                             ensure_ascii=False).encode("utf-8")
        with patch("wechatauto.wechat_auth.urllib.request.urlopen",
                   return_value=Response(payload)):
            with self.assertRaises(WechatQuotaError):
                client.report_phone_result("token-1", {"requestId": "r-1"})

    def test_requires_ticket_and_qr_image(self):
        client = WechatAuthClient("http://localhost:8080")
        with patch("wechatauto.wechat_auth.urllib.request.urlopen",
                   return_value=Response(b'{"code":200,"url":"https://example.org"}')):
            with self.assertRaisesRegex(WechatAuthError, "state"):
                client.create_qr()

    def test_rejects_non_success_ruoyi_response(self):
        client = WechatAuthClient("http://localhost:8080")
        with patch("wechatauto.wechat_auth.urllib.request.urlopen",
                   return_value=Response(b'{"code":500,"msg":"service error"}')):
            with self.assertRaisesRegex(WechatAuthError, "service error"):
                client.poll("ticket-1")


if __name__ == "__main__":
    unittest.main()
