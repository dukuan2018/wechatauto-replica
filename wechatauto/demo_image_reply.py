# -*- coding: utf-8 -*-
"""监听所有会话图片，识别二维码并自动回复。"""
from __future__ import annotations

import argparse
import sys
import threading
import time

from wechatauto import MediaDownloader, WeChat, WeChatDB
from wechatauto.demo_group_qr_reply import decode_qr


def is_incoming_image(msg, self_wxid: str = "") -> bool:
    """判断消息是否为其他人发来的图片。"""
    if getattr(msg, "type", "") != "image":
        return False
    if getattr(msg, "attr", "") == "self":
        return False
    sender_wxid = getattr(msg, "sender_wxid", "") or ""
    return not self_wxid or sender_wxid != self_wxid


def message_key(msg, chat) -> tuple:
    """生成跨分片稳定的消息去重键。"""
    return (
        getattr(chat, "who", "") or "",
        getattr(msg, "sort_seq", None),
        getattr(msg, "local_id", None),
        getattr(msg, "shard", None),
    )


def format_qr_reply(texts: list[str], detected: bool = True) -> str:
    """生成二维码识别回复正文。"""
    if not detected or not texts:
        return "收到，未识别到二维码"
    return "识别到二维码内容：\n" + "\n".join(texts)


def group_sender_name(msg, chat) -> str:
    """优先使用群成员列表中的显示名，作为 @ 成员的名称。"""
    sender_wxid = getattr(msg, "sender_wxid", "") or ""
    if sender_wxid:
        try:
            members = list(chat.GetGroupMembers())
            for member in members:
                if member.get("username") == sender_wxid:
                    display = (member.get("remark") or member.get("nick_name")
                               or sender_wxid)
                    same_name = [m for m in members
                                 if (m.get("remark") or m.get("nick_name")
                                     or m.get("username")) == display]
                    return display if len(same_name) == 1 else ""
        except Exception:
            pass
    return (getattr(msg, "sender", "")
            or getattr(msg, "sender_remark", "")
            or sender_wxid)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="监听所有好友和群聊收到的图片，识别二维码并回复")
    parser.add_argument("--account", required=True,
                        help="微信数据目录名，例如 wxid_xxx_abcd")
    parser.add_argument("--reply", default="收到",
                        help="未识别到二维码时的回复内容，默认：收到")
    parser.add_argument("--out", default=None,
                        help="图片临时保存目录，默认使用系统临时目录")
    parser.add_argument("--cache-wait", type=float, default=5.0,
                        help="等待图片缓存的秒数，默认 5")
    args = parser.parse_args()

    db = WeChatDB(account=args.account)
    wx = WeChat(db=db)
    media = MediaDownloader(db, save_dir=args.out)
    self_wxid = db.get_self_info().get("username", "")
    lock = threading.Lock()
    handled: set[tuple] = set()

    def on_message(msg, chat) -> None:
        if not is_incoming_image(msg, self_wxid):
            return
        key = message_key(msg, chat)
        with lock:
            if key in handled:
                return
            handled.add(key)
        conversation = getattr(chat, "_wxid", None) or chat.who
        group = conversation.endswith("@chatroom")
        try:
            deadline = time.monotonic() + max(0.0, args.cache_wait)
            image_path = None
            while time.monotonic() < deadline:
                image_path = media.download_image(
                    conversation, msg.local_id, save_dir=args.out,
                    tier="best", shard=getattr(msg, "shard", None))
                if image_path:
                    break
                time.sleep(0.5)
            texts = decode_qr(image_path) if image_path else []
            reply = format_qr_reply(texts, detected=bool(texts))
            if not texts and args.reply:
                reply = args.reply + "，未识别到二维码"
            if group:
                sender = group_sender_name(msg, chat)
                if not sender:
                    print("群图片未找到发送者，跳过艾特：%s" % key,
                          file=sys.stderr, flush=True)
                    return
                result = chat.SendMsg(reply, at=sender)
            else:
                sender = (getattr(msg, "sender", "")
                          or getattr(msg, "sender_wxid", "")
                          or "未知发送者")
                result = chat.SendMsg(reply)
            if result:
                print("已回复 %s（会话：%s）：%s" %
                      (sender, getattr(chat, "nickname", chat.who), reply),
                      flush=True)
            else:
                print("发送回复失败（会话：%s）：%s" % (
                    getattr(chat, "nickname", chat.who),
                    result.get("message", result)),
                    file=sys.stderr, flush=True)
        except Exception as exc:
            print("回复图片失败（会话：%s，消息：%s）：%s" % (
                getattr(chat, "nickname", chat.who), key, exc),
                file=sys.stderr, flush=True)

    result = wx.AddListenAll(on_message, discover=True)
    if not result:
        raise RuntimeError(result.get("message", result))
    print("正在监听所有会话的图片消息，账号：%s；按 Ctrl+C 停止。" % self_wxid,
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
