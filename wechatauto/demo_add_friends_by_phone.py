# -*- coding: utf-8 -*-
"""从命令行按手机号批量发送微信好友申请。"""
from __future__ import annotations

import argparse
import csv
import os
import time
from pathlib import Path
from typing import Dict, List, Optional, Set

import psutil
import win32gui
import win32process

from wechatauto.db import auto_detect_db_dir
from wechatauto.phone_friend import (
    PhoneAddFriendOps,
    collect_phones,
    mask_phone,
    read_phone_file,
)
from wechatauto.uia_driver import WeChatUIA


def _process_accounts(pid: int, db_root: Path) -> Set[str]:
    """读取一个微信进程当前实际打开的账号数据库目录。"""
    accounts = set()
    for item in psutil.Process(pid).open_files():
        try:
            parts = Path(item.path).resolve().relative_to(db_root).parts
        except ValueError:
            continue
        if len(parts) >= 3 and parts[1].lower() == "db_storage":
            accounts.add(parts[0])
    return accounts


def _account_matches(actual: str, requested: str) -> bool:
    """同时接受完整账号目录名和不带四位目录后缀的 wxid。"""
    if actual == requested:
        return True
    return actual.rsplit("_", 1)[0] == requested


def find_account_window(account: str, hwnd: Optional[int] = None) -> int:
    """按进程打开的数据库目录找到对应微信主窗口。"""
    db_dir = auto_detect_db_dir()
    if not db_dir:
        raise RuntimeError("未找到微信数据库根目录")
    root = Path(db_dir).resolve()
    pid_accounts: Dict[int, Set[str]] = {}

    def accounts_for(pid: int) -> Set[str]:
        if pid not in pid_accounts:
            try:
                pid_accounts[pid] = _process_accounts(pid, root)
            except (psutil.Error, OSError):
                pid_accounts[pid] = set()
        return pid_accounts[pid]

    if hwnd:
        if not win32gui.IsWindow(hwnd):
            raise RuntimeError("指定的 hwnd 不存在：%s" % hwnd)
        _tid, pid = win32process.GetWindowThreadProcessId(hwnd)
        if not any(_account_matches(item, account) for item in accounts_for(pid)):
            raise RuntimeError("指定窗口与账号目录不匹配，为避免用错微信号已停止")
        return int(hwnd)

    candidates = []

    def collect(candidate, _extra):
        if not win32gui.IsWindowVisible(candidate):
            return True
        title = (win32gui.GetWindowText(candidate) or "").strip()
        cls = win32gui.GetClassName(candidate) or ""
        if title not in ("微信", "Weixin") or not cls.startswith("Qt51514QWindowIcon"):
            return True
        _tid, pid = win32process.GetWindowThreadProcessId(candidate)
        if any(_account_matches(item, account) for item in accounts_for(pid)):
            left, top, right, bottom = win32gui.GetWindowRect(candidate)
            candidates.append((max(0, right - left) * max(0, bottom - top), candidate))
        return True

    win32gui.EnumWindows(collect, None)
    if not candidates:
        raise RuntimeError("没有找到账号 %s 对应的微信主窗口，请确认该账号已登录" % account)
    candidates.sort(reverse=True)
    return int(candidates[0][1])


def _write_csv(path: str, results: List[Dict]) -> None:
    """把完整手机号和逐条执行结果写入 CSV。"""
    fields = ("phone", "ok", "reason", "step", "added", "dry_run")
    with open(path, "w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(results)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="按手机号批量查找微信用户并发送好友申请")
    parser.add_argument("--account", required=True,
                        help="微信账号目录名，例如 wxid_xxx_abcd")
    parser.add_argument("--phones", nargs="*", default=[],
                        help="手机号列表，可用空格、逗号或分号分隔")
    parser.add_argument("--file", help="手机号文本文件，每行一个或用逗号分隔")
    parser.add_argument("--message", default=None,
                        help="好友申请留言；省略则保留微信默认文案")
    parser.add_argument("--label", default="直充2",
                        help="好友标签，默认按“直充2”唯一匹配“南瓜直充2”")
    parser.add_argument("--interval", type=float, default=3.0,
                        help="两个号码之间的等待秒数，默认 3")
    parser.add_argument("--hwnd", type=int,
                        help="明确指定微信主窗口句柄，程序仍会校验账号归属")
    parser.add_argument("--output", help="把完整结果写入 CSV 文件")
    parser.add_argument("--go", action="store_true",
                        help="真正发送好友申请；不加时只查看资料卡")
    args = parser.parse_args()

    phones = collect_phones(args.phones)
    if args.file:
        phones = collect_phones([*phones, *read_phone_file(args.file)])
    if not phones:
        parser.error("请通过 --phones 或 --file 提供至少一个有效手机号")

    hwnd = find_account_window(args.account, args.hwnd)
    uia = WeChatUIA(hwnd=hwnd)
    if not uia.ensure_materialized(timeout=8.0):
        raise RuntimeError("微信 UIA 控件树未成功激活")
    ops = PhoneAddFriendOps(uia=uia)
    mode = "发送申请" if args.go else "试运行"
    print("账号窗口 hwnd=%s；模式：%s；共 %d 个手机号。" %
          (hwnd, mode, len(phones)), flush=True)

    results = []
    for index, phone in enumerate(phones):
        if index:
            time.sleep(max(0.0, args.interval))
        masked = mask_phone(phone)
        print("[%d/%d] 正在处理 %s ..." % (index + 1, len(phones), masked),
              flush=True)
        result = ops.add_phone(phone, message=args.message, dry_run=not args.go,
                               label=args.label)
        results.append(result)
        if result.get("reason") == "already-friend":
            text = "已是好友"
        elif result.get("reason") == "would-add":
            text = "可添加（试运行未发送）"
        elif result.get("added"):
            text = "好友申请已发送"
        else:
            text = "失败：%s" % result.get("reason", "未知原因")
        print("[%d/%d] %s：%s" % (index + 1, len(phones), masked, text),
              flush=True)

    if args.output:
        output = os.path.abspath(args.output)
        _write_csv(output, results)
        print("完整结果已写入：%s" % output, flush=True)
    sent = sum(bool(item.get("added")) for item in results)
    failed = sum(not item.get("ok") for item in results)
    print("处理完成：发送 %d，失败 %d，其余为已是好友或试运行结果。" %
          (sent, failed), flush=True)


if __name__ == "__main__":
    main()
