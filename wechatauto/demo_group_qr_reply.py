# -*- coding: utf-8 -*-
"""Listen to group images, decode QR codes, and reply with their text."""
from __future__ import annotations

import argparse
import sys
import threading
import time

import cv2

from wechatauto import MediaDownloader, WeChat, WeChatDB


def decode_qr(image_path: str) -> list[str]:
    image = cv2.imread(image_path)
    if image is None:
        return []
    height, width = image.shape[:2]
    if min(height, width) < 320:
        scale = max(2, 320 // min(height, width))
        image = cv2.resize(image, None, fx=scale, fy=scale,
                           interpolation=cv2.INTER_NEAREST)
    margin = max(12, min(image.shape[:2]) // 20)
    image = cv2.copyMakeBorder(image, margin, margin, margin, margin,
                               cv2.BORDER_CONSTANT, value=(255, 255, 255))
    detector = cv2.QRCodeDetector()
    try:
        ok, texts, _points, _straight = detector.detectAndDecodeMulti(image)
        if ok:
            return list(dict.fromkeys(text for text in texts if text))
    except (AttributeError, cv2.error):
        pass
    text, _points, _straight = detector.detectAndDecode(image)
    return [text] if text else []


def format_reply(sender_name: str, texts: list[str]) -> str:
    greeting = "\u4f60\u597d\uff1a" + (sender_name or "")
    heading = "\u8bc6\u522b\u5230\u4e8c\u7ef4\u7801\u5185\u5bb9\uff1a"
    return greeting + "\n" + heading + "\n" + "\n".join(texts)


def main() -> None:
    parser = argparse.ArgumentParser(description="Decode QR codes in group images and reply")
    parser.add_argument("groups", nargs="*", help="Group names or group usernames")
    parser.add_argument("--all-groups", action="store_true", help="Listen to all groups")
    parser.add_argument("--account", help="Exact WeChat account directory name")
    parser.add_argument("--out", default=None, help="Directory for decoded image files")
    parser.add_argument("--cache-wait", type=float, default=5.0)
    args = parser.parse_args()
    if args.all_groups and args.groups:
        parser.error("--all-groups cannot be combined with group names")
    if not args.all_groups and not args.groups:
        parser.error("Specify one or more groups, or use --all-groups")

    db = WeChatDB(account=args.account)
    wx = WeChat(db=db)
    media = MediaDownloader(db, save_dir=args.out)
    self_wxid = db.get_self_info().get("username", "")
    callback_lock = threading.Lock()
    handled: set[tuple[str, int]] = set()

    def on_message(msg, chat) -> None:
        group_user = getattr(chat, "_wxid", None) or chat.who
        if not group_user.endswith("@chatroom"):
            return
        if getattr(msg, "type", "") != "image" or getattr(msg, "attr", "") != "friend":
            return
        if getattr(msg, "sender_wxid", "") == self_wxid:
            return

        key = (group_user, getattr(msg, "sort_seq", None) or msg.local_id)
        with callback_lock:
            if key in handled:
                return
            handled.add(key)
            try:
                deadline = time.monotonic() + max(0.0, args.cache_wait)
                image_path = None
                while True:
                    image_path = media.download_image(
                        group_user, msg.local_id, save_dir=args.out,
                        tier="best", shard=getattr(msg, "shard", None),
                    )
                    if image_path or time.monotonic() >= deadline:
                        break
                    time.sleep(0.5)
                if not image_path:
                    print("No cached image; skipped: %s / %s" % (
                        chat.nickname, msg.local_id), flush=True)
                    return

                texts = decode_qr(image_path)
                if not texts:
                    print("No QR code detected: %s / %s" % (
                        chat.nickname, msg.local_id), flush=True)
                    return

                sender_name = (getattr(msg, "sender", "")
                               or getattr(msg, "sender_remark", ""))
                reply = format_reply(sender_name, texts)
                result = chat.SendMsg(reply)
                print("Replied in %s: %s" % (chat.nickname, result), flush=True)
            except Exception as exc:
                print("Image processing failed in %s / %s: %s" % (
                    chat.nickname, msg.local_id, exc), file=sys.stderr, flush=True)

    if args.all_groups:
        wx.AddListenAll(on_message, discover=True)
        print("Listening to all group chats.", flush=True)
    else:
        for raw in args.groups:
            group_name = db.get_nickname(raw) or raw if raw.endswith("@chatroom") else raw
            hits = [item for item in db.search_contact(group_name)
                    if item.get("username", "").endswith("@chatroom")]
            if not hits:
                parser.error("Could not resolve group: %s" % raw)
            result = wx.AddListenChat(group_name, on_message)
            print("Listen %s: %s" % (raw, result), flush=True)

    wx.StartListening()
    print("Running. Press Ctrl+C to stop.", flush=True)
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        wx.StopListening()


if __name__ == "__main__":
    main()
