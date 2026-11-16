# -*- coding: utf-8 -*-
"""通过微信主搜索框按手机号查找用户并发送好友申请。"""
from __future__ import annotations

import re
import time
import unicodedata
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence

from wechatauto import rhythm
from wechatauto.group_friend import GroupAddFriendOps, PROFILE_POP_CLS
from wechatauto.labels import _result

PHONE_RE = re.compile(r"^1[3-9]\d{9}$")
NETWORK_HINTS = ("网络查找手机/QQ号", "搜索网络结果")
SEARCH_ROW_CLS = "mmui::XTableCell"
ADD_FRIEND_WINDOW_CLS = "mmui::AddFriendWindow"


def normalize_phone(value: object) -> Optional[str]:
    """把常见中国大陆手机号写法整理成 11 位数字，非法值返回 ``None``。"""
    text = unicodedata.normalize("NFKC", str(value or "")).strip()
    text = re.sub(r"[\s()（）.·_-]+", "", text)
    if text.startswith("+86"):
        text = text[3:]
    elif text.startswith("0086"):
        text = text[4:]
    elif text.startswith("86") and len(text) == 13:
        text = text[2:]
    return text if PHONE_RE.fullmatch(text) else None


def collect_phones(values: Iterable[object]) -> List[str]:
    """拆分、校验并去重手机号，同时保持输入顺序。"""
    out: List[str] = []
    seen = set()
    for value in values:
        for item in re.split(r"[,，;；\s]+", str(value or "")):
            phone = normalize_phone(item)
            if phone and phone not in seen:
                seen.add(phone)
                out.append(phone)
    return out


def read_phone_file(path: str) -> List[str]:
    """读取手机号文本文件；支持 UTF-8、UTF-8 BOM 和 GB18030。"""
    raw = Path(path).read_bytes()
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = raw.decode("gb18030")
    return collect_phones([text])


def mask_phone(phone: str) -> str:
    """生成适合终端日志展示的手机号掩码。"""
    phone = str(phone or "")
    if len(phone) == 11 and phone.isdigit():
        return phone[:3] + "****" + phone[-4:]
    return phone


class PhoneAddFriendOps(GroupAddFriendOps):
    """手机号加好友；资料卡判断和申请发送复用群成员加好友实现。"""

    card_load_retry_delay = 1.0

    def _wait_for_card_load_retry(self) -> None:
        time.sleep(self.card_load_retry_delay)

    def card_state(self, card) -> Dict:
        """资料卡按钮尚未加载时等待一次，再识别好友状态。"""
        state = super().card_state(card)
        names = state.get("names", [])
        explicitly_missing = any("无法找到该用户" in name for name in names)
        if (not state.get("ok")
                and state.get("reason", "").startswith("unknown-card")
                and not explicitly_missing):
            self._wait_for_card_load_retry()
            state = super().card_state(card)
        if not state.get("ok") and any(
                "无法找到该用户" in name for name in state.get("names", [])):
            state["reason"] = "not-found：无法找到该用户，请检查手机号是否正确"
        return state

    def _clear_search(self) -> None:
        win = self.main_window()
        if win is None:
            return
        box = self.uia._search_box(win, expand=False)
        if box is not None:
            try:
                self.uia._set_text(box, "")
            except Exception:
                pass

    def close_transients(self) -> None:
        """关闭本流程遗留的资料卡或好友申请框。"""
        for _hwnd, ctrl in self._top_windows():
            try:
                cls = ctrl.ClassName or ""
            except Exception:
                continue
            if cls == PROFILE_POP_CLS or any(
                    key in cls for key in ("VerifyFriend", "AddFriend", "FriendRequest")):
                try:
                    ctrl.SendKeys("{Esc}")
                except Exception:
                    pass
        rhythm.nap(0.3)

    @staticmethod
    def _row_sort_key(ctrl):
        try:
            rect = ctrl.BoundingRectangle
            return rect.top, rect.left
        except Exception:
            return 1 << 30, 1 << 30

    def _network_rows(self, phone: str) -> List[object]:
        """确认底部手机号精确匹配后，返回上方“网络查找”动作行。"""
        lst = self.uia._find_search_list(timeout=3.0)
        if lst is None:
            return []
        action_rows = []
        exact_phone_seen = False
        network_seen = False
        for ctrl in lst.GetChildren():
            try:
                cls = ctrl.ClassName or ""
                name = (ctrl.Name or "").strip()
            except Exception:
                continue
            if NETWORK_HINTS[0] in name:
                action_rows.append(ctrl)
                continue
            if NETWORK_HINTS[1] in name:
                network_seen = True
                continue
            if network_seen and cls == SEARCH_ROW_CLS and normalize_phone(name) == phone:
                exact_phone_seen = True
        if not exact_phone_seen:
            return []
        return sorted(action_rows, key=self._row_sort_key)

    def search_phone(self, phone: str, timeout: float = 5.0) -> Dict:
        """在主搜索框中输入手机号，并返回精确匹配的网络搜索结果行。"""
        phone = normalize_phone(phone)
        if not phone:
            return _result(False, "invalid-phone：不是有效的中国大陆手机号")
        self.close_transients()
        if not self.uia.ensure_window():
            return _result(False, "no-uia：UIA 树不可用（微信未登录/未热激活）")
        if not self.uia.back_to_chat_tab():
            return _result(False, "no-chat-page：无法返回微信聊天主页")
        win = self.main_window()
        if win is None:
            return _result(False, "no-window：找不到微信主窗口")
        self._raise(win.NativeWindowHandle)
        box = self.uia._search_box(win, expand=True)
        if box is None:
            return _result(False, "no-search-box：找不到微信主搜索框")
        if not self.uia._set_text(box, phone):
            clicked = self._click(win.NativeWindowHandle, box)
            if not clicked["ok"] or not self._type_text(box, phone):
                return _result(False, "type-failed：手机号没有写入搜索框")

        deadline = time.monotonic() + max(0.5, timeout)
        rows: List[object] = []
        while time.monotonic() < deadline:
            rows = self._network_rows(phone)
            if rows:
                break
            rhythm.nap(0.2)
        if not rows:
            return _result(False, "no-result：没有出现匹配手机号的网络搜索入口")
        return _result(True, "ok", phone=phone, row=rows[0], rows=rows)

    def open_phone_card(self, phone: str, row, timeout: float = 6.0) -> Dict:
        """点击手机号网络结果并等待用户资料卡。"""
        win = self.main_window()
        if win is None:
            return _result(False, "no-window：找不到微信主窗口")
        try:
            rect = row.BoundingRectangle
            x = (rect.left + rect.right) // 2
            y = (rect.top + rect.bottom) // 2
        except Exception:
            return _result(False, "no-rect：手机号搜索结果没有可点击区域")
        owner = self._owner_at(x, y)
        allowed = {win.NativeWindowHandle}
        allowed.update(hwnd for hwnd, _ctrl in self._top_windows())
        if owner and owner not in allowed:
            return _result(False, "occluded：手机号搜索结果被其他窗口遮挡")
        clicked = self._click(owner or win.NativeWindowHandle, row, raise_it=False)
        if not clicked["ok"]:
            return clicked
        pop = self._wait_top(ADD_FRIEND_WINDOW_CLS, timeout=timeout)
        if pop is None:
            return _result(False, "no-card：微信没有打开“添加朋友”资料窗口")
        return _result(True, "ok", phone=phone, hwnd=pop[0], card=pop[1])

    def add_phone(self, phone: str, message: Optional[str] = None,
                  dry_run: bool = True, label: Optional[str] = "直充2") -> Dict:
        """处理一个手机号；试运行只读取资料卡，不发送好友申请。"""
        normalized = normalize_phone(phone)
        if not normalized:
            return _result(False, "invalid-phone：不是有效的中国大陆手机号",
                           phone=str(phone or ""), step="validate")
        try:
            searched = self.search_phone(normalized)
            if not searched["ok"]:
                searched.update(phone=normalized, step="search", added=False)
                return searched
            opened = self.open_phone_card(normalized, searched["row"])
            if not opened["ok"]:
                opened.update(phone=normalized, step="card", added=False)
                return opened
            state = self.card_state(opened["card"])
            state.update(phone=normalized, step="card", added=False,
                         dry_run=bool(dry_run))
            if not state["ok"]:
                return state
            if state["reason"] == "already-friend":
                return state
            if dry_run:
                state.update(reason="would-add", would_add=True)
                return state
            sent = self.send_request(
                message, card_window=(opened["hwnd"], opened["card"]),
                remark_suffix=normalized, label=label)
            sent.update(phone=normalized, step="send", dry_run=False,
                        added=bool(sent.get("ok")))
            return sent
        except Exception as exc:
            return _result(False, "exception：%s" % exc, phone=normalized,
                           step="exception", added=False)
        finally:
            self.close_transients()
            self._clear_search()

    def add_many(self, phones: Sequence[str], message: Optional[str] = None,
                 dry_run: bool = True, interval: float = 3.0,
                 label: Optional[str] = "直充2") -> List[Dict]:
        """逐个处理手机号；单个失败不会中断后续号码。"""
        results = []
        targets = collect_phones(phones)
        for index, phone in enumerate(targets):
            if index:
                time.sleep(max(0.0, interval))
            results.append(self.add_phone(
                phone, message=message, dry_run=dry_run, label=label))
        return results


__all__ = [
    "PhoneAddFriendOps",
    "collect_phones",
    "mask_phone",
    "normalize_phone",
    "read_phone_file",
]
