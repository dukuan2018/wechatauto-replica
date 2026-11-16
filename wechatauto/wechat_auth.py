# -*- coding: utf-8 -*-
"""RuoYi-Lxz 微信扫码登录接口客户端。"""
from __future__ import annotations

import base64
import json
import os
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Dict, Optional

import qrcode
from io import BytesIO


@dataclass(frozen=True)
class QrLogin:
    """网页 OAuth 授权票据与授权链接。"""

    ticket: str
    image: str


@dataclass(frozen=True)
class AuthUser:
    """已认证的 RuoYi 用户基本资料。"""

    token: str
    nickname: str
    avatar: str = ""


class WechatAuthError(RuntimeError):
    """登录接口不可用或返回的数据无效。"""


class WechatQuotaError(WechatAuthError):
    """额度不足，不能继续执行添加操作。"""


class WechatAuthClient:
    """调用 RuoYi 服务号网页 OAuth 接口。"""

    def __init__(self, base_url: Optional[str] = None, timeout: float = 5.0):
        self.base_url = (base_url or os.environ.get(
            "WECHAT_AUTO_API_BASE_URL", "https://discount.yidaw.cn/prod-api")).rstrip("/")
        self.timeout = timeout

    def create_qr(self) -> QrLogin:
        result = self._request("GET", "/wxMpAuthUrl")
        data = self._data(result)
        url = str(data.get("url") or "").strip()
        ticket = str(data.get("state") or "").strip()
        if url and not ticket:
            query = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
            ticket = (query.get("state") or [""])[0]
        if not ticket or not url:
            raise WechatAuthError("/wxMpAuthUrl 未返回授权 url 或 state")
        return QrLogin(ticket=ticket, image=url)

    def poll(self, ticket: str) -> Dict[str, Any]:
        query = urllib.parse.urlencode({"state": ticket})
        result = self._request("GET", "/wxMpLoginStatus?" + query)
        return self._data(result)

    def user_info(self, token: str) -> Dict[str, Any]:
        result = self._request("GET", "/getInfo", token=token)
        data = self._data(result)
        return data.get("user") or data

    def helper_user_info(self, token: str) -> Dict[str, Any]:
        result = self._request("GET", "/wechat/helper/userInfo", token=token)
        return self._data(result)

    def client_config(self, token: str) -> Dict[str, Any]:
        result = self._request("GET", "/wechat/helper/clientConfig", token=token)
        return self._data(result)

    def report_phone_result(self, token: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        try:
            result = self._request("POST", "/wechat/helper/record", token=token,
                                   body=payload)
        except WechatAuthError as exc:
            if "服务暂不可用" not in str(exc):
                raise
            result = self._request("POST", "/wechat/helper/record", token=token,
                                   body=payload)
        return self._data(result)

    def logout(self, token: str) -> None:
        self._request("POST", "/logout", token=token)

    def qr_image_bytes(self, source: str) -> bytes:
        """把服务号 OAuth 授权 URL 生成为桌面端显示的二维码。"""
        if source.startswith("https://open.weixin.qq.com/connect/oauth2/authorize"):
            qr = qrcode.QRCode(box_size=8, border=3)
            qr.add_data(source)
            qr.make(fit=True)
            image = qr.make_image(fill_color="black", back_color="white").convert("RGB")
            buffer = BytesIO()
            image.save(buffer, format="PNG")
            return buffer.getvalue()
        if source.startswith("data:image/"):
            try:
                header, payload = source.split(",", 1)
                if ";base64" not in header:
                    raise ValueError("QR image data URI must use base64")
                return base64.b64decode(payload, validate=True)
            except (ValueError, base64.binascii.Error) as exc:
                raise WechatAuthError("二维码图片数据无效") from exc
        url = urllib.parse.urljoin(self.base_url + "/", source)
        if urllib.parse.urlsplit(url).scheme not in ("http", "https"):
            raise WechatAuthError("二维码图片地址必须使用 HTTP 或 HTTPS")
        request = urllib.request.Request(url, headers={"Accept": "image/*"})
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                content_type = response.headers.get("Content-Type", "")
                payload = response.read(3 * 1024 * 1024 + 1)
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise WechatAuthError("二维码图片加载失败：%s" % exc) from exc
        if len(payload) > 3 * 1024 * 1024:
            raise WechatAuthError("二维码图片超过 3 MB")
        if content_type and not content_type.lower().startswith("image/"):
            raise WechatAuthError("二维码地址未返回图片")
        return payload

    def avatar_bytes(self, source: str) -> bytes:
        return self.qr_image_bytes(source)

    def qr_code_bytes(self, content: str) -> bytes:
        """Encode arbitrary text as a local PNG QR code."""
        value = str(content or "").strip()
        if not value:
            raise WechatAuthError("二维码内容为空")
        qr = qrcode.QRCode(box_size=8, border=3)
        qr.add_data(value)
        qr.make(fit=True)
        image = qr.make_image(fill_color="black", back_color="white").convert("RGB")
        buffer = BytesIO()
        image.save(buffer, format="PNG")
        return buffer.getvalue()

    def _request(self, method: str, path: str,
                 token: Optional[str] = None,
                 body: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        headers = {"Accept": "application/json"}
        payload = None
        if body is not None or method == "POST":
            headers["Content-Type"] = "application/json"
            payload = json.dumps(body or {}).encode("utf-8")
        if token:
            headers["Authorization"] = "Bearer " + token
        request = urllib.request.Request(
            self.base_url + path, data=payload, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                payload = response.read(1024 * 1024 + 1)
        except urllib.error.HTTPError as exc:
            detail = exc.read(4096).decode("utf-8", errors="replace")
            try:
                error_payload = json.loads(detail)
            except (TypeError, json.JSONDecodeError):
                error_payload = {}
            message = str(error_payload.get("msg") or detail or
                          "HTTP %s" % exc.code)
            if exc.code == 409 or "额度不足" in message:
                raise WechatQuotaError(message) from exc
            raise WechatAuthError("登录接口 HTTP %s：%s" % (exc.code, detail)) from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise WechatAuthError(
                "登录服务暂不可用，请确认 RuoYi-Lxz 登录接口已启动：%s" % exc) from exc
        if len(payload) > 1024 * 1024:
            raise WechatAuthError("登录接口返回内容过大")
        try:
            result = json.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise WechatAuthError("登录接口没有返回有效 JSON") from exc
        if not isinstance(result, dict):
            raise WechatAuthError("登录接口返回格式无效")
        code = result.get("code", 200)
        if str(code) not in ("200", "0", "None"):
            message = str(result.get("msg") or "登录接口请求失败")
            if str(code) == "409" or "额度不足" in message:
                raise WechatQuotaError(message)
            raise WechatAuthError(message)
        return result

    @staticmethod
    def _data(result: Dict[str, Any]) -> Dict[str, Any]:
        data = result.get("data", result)
        if not isinstance(data, dict):
            raise WechatAuthError("登录接口 data 字段格式无效")
        return data

    def resolve_user(self, token: str, payload: Dict[str, Any]) -> AuthUser:
        if payload.get("fullServiceAuthorized") is not True:
            raise WechatAuthError(
                "后台尚未确认“使用完整服务”授权，请部署新版后台接口后重新扫码。")
        user_data = payload.get("user")
        if not isinstance(user_data, dict):
            raise WechatAuthError(
                "微信已扫码，尚未完成个人信息授权。请点击微信页面右下角的“使用完整服务”。")
        nickname = str(user_data.get("nickName") or user_data.get("nickname")
                       or user_data.get("userName") or "").strip()
        avatar = str(user_data.get("avatar") or user_data.get("avatarUrl")
                      or user_data.get("headImgUrl") or "").strip()
        if not token:
            raise WechatAuthError("扫码结果缺少登录凭证")
        if not nickname or not avatar:
            raise WechatAuthError(
                "微信已扫码，尚未完成个人信息授权。请点击微信页面右下角的“使用完整服务”。")
        return AuthUser(token=token, nickname=nickname, avatar=avatar)


__all__ = ["AuthUser", "QrLogin", "WechatAuthClient", "WechatAuthError",
           "WechatQuotaError"]
