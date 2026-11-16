# -*- coding: utf-8 -*-
"""监听私聊转账，并在确认收款人是当前账号后自动点击收款。"""
from __future__ import annotations

import argparse
import re
import sys
import threading
import time

from wechatauto import WeChat, WeChatDB


def _tag(xml: str, name: str) -> str:
    """读取微信 XML 中的简单文本标签，兼容 CDATA 和普通文本。"""
    if not isinstance(xml, str):
        return ""
    match = re.search(
        r"<%s>\s*(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?\s*</%s>" % (name, name),
        xml,
        flags=re.IGNORECASE | re.DOTALL,
    )
    return (match.group(1) if match else "").strip()


def parse_incoming_transfer(content: str, self_wxid: str,
                            sender_wxid: str = "") -> dict | None:
    """只识别发给当前账号、等待接收的私聊转账。"""
    if not isinstance(content, str) or "<wcpayinfo" not in content:
        return None
    if _tag(content, "type") != "2000":
        return None
    if _tag(content, "paysubtype") != "1":
        return None
    receiver = _tag(content, "receiver_username")
    if not self_wxid or receiver != self_wxid:
        return None
    if sender_wxid and sender_wxid == self_wxid:
        return None
    transfer_id = _tag(content, "transferid")
    transaction_id = _tag(content, "transcationid") or _tag(content, "transactionid")
    if not transfer_id and not transaction_id:
        return None
    return {
        "transfer_id": transfer_id,
        "transaction_id": transaction_id,
        "amount": _tag(content, "feedesc"),
        "receiver": receiver,
    }


def _same_transfer_received(db: WeChatDB, who: str, transfer: dict) -> bool:
    target = transfer.get("transfer_id") or transfer.get("transaction_id")
    if not target:
        return False
    for row in db.get_messages(who, limit=80):
        content = row.get("content") or ""
        if target not in content:
            continue
        if _tag(content, "paysubtype") == "3":
            return True
    return False


def main() -> None:
    parser = argparse.ArgumentParser(
        description="自动接收发给当前微信账号的私聊转账")
    parser.add_argument("--account", required=True,
                        help="微信数据目录名，例如 wxid_xxx_abcd")
    parser.add_argument("--timeout", type=float, default=12.0,
                        help="每笔转账等待界面按钮的秒数，默认 12")
    args = parser.parse_args()

    db = WeChatDB(account=args.account)
    wx = WeChat(db=db)
    self_wxid = db.get_self_info().get("username", "")
    lock = threading.Lock()
    attempted: set[str] = set()

    def on_message(msg, chat) -> None:
        # 群转账不自动处理，避免多人群聊中的误收款或权限问题。
        who = getattr(chat, "who", "") or ""
        if not who or who.endswith("@chatroom"):
            return
        transfer = parse_incoming_transfer(
            getattr(msg, "content", ""),
            self_wxid,
            getattr(msg, "sender_wxid", ""),
        )
        if not transfer:
            return
        key = transfer.get("transfer_id") or transfer.get("transaction_id")
        with lock:
            if key in attempted:
                return
            attempted.add(key)
        amount = transfer.get("amount") or "未知金额"
        print("检测到来自 %s 的转账：%s，正在打开收款界面…" % (
            getattr(msg, "sender", "") or getattr(msg, "sender_wxid", "") or who,
            amount), flush=True)
        try:
            result = wx._gui.accept_transfer(
                who=who, transfer_id=transfer.get("transfer_id"),
                timeout=args.timeout)
            if not result:
                print("自动收款失败：%s" % result.get("message", result),
                      file=sys.stderr, flush=True)
                return
            confirmed = False
            deadline = time.monotonic() + 8.0
            while time.monotonic() < deadline:
                if _same_transfer_received(db, who, transfer):
                    confirmed = True
                    break
                time.sleep(0.5)
            if confirmed:
                print("已自动接收转账：%s / %s" % (who, amount), flush=True)
            else:
                print("已点击收款，但数据库暂未确认入账：%s" % who,
                      file=sys.stderr, flush=True)
        except Exception as exc:
            print("自动收款异常：%s" % exc, file=sys.stderr, flush=True)

    wx.AddListenAll(on_message, discover=True)
    print("正在监听私聊转账，账号：%s；按 Ctrl+C 停止。" % self_wxid,
          flush=True)
    wx.StartListening()
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        wx.StopListening()


if __name__ == "__main__":
    main()
