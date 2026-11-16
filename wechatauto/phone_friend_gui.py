# -*- coding: utf-8 -*-
"""手机号批量添加微信好友的 Windows 桌面界面。"""
from __future__ import annotations

import ctypes
import io
import queue
import threading
import time
import urllib.parse
import uuid
import webbrowser
from ctypes import wintypes
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable, Dict, List, Optional, Set, Tuple

import psutil
import tkinter as tk
from tkinter import messagebox, scrolledtext, ttk
from PIL import Image, ImageDraw, ImageOps, ImageTk
import win32gui
import win32process

from wechatauto.db import WeChatDB, auto_detect_db_dir
from wechatauto.phone_friend import PhoneAddFriendOps, collect_phones, mask_phone
from wechatauto.uia_driver import WeChatUIA
from wechatauto.wechat_auth import (
    AuthUser, WechatAuthClient, WechatAuthError, WechatQuotaError,
)


# 桌面端发布版本；每次发布新版本时只递增小版本号。
DESKTOP_VERSION = "v1.8"
DISCLAIMER_TEXT = (
    "本软件为辅助工具。好友查找和申请操作通过识别并操作电脑上的微信客户端界面完成，"
    "不调用微信好友添加接口。\n\n"
    "不同微信账号的状态和可操作情况可能不同，"
    "注册时间、使用频率、历史行为等因素都可能影响好友申请结果，本软件不能保证避免微信平台的风控或账号限制。\n\n"
    "请遵守微信相关规则及适用法律，合理控制好友申请频率，不要短时间内大量添加。"
    "如出现“操作频繁”、安全验证或其他限制提示，请立即停止操作，并按微信客户端提示处理，"
    "不要继续重试。软件已配置发送间隔与异常停止规则，检测到频率限制或异常时会尝试停止任务；"
    "由于客户端界面和网络状态可能变化，自动检测不能替代人工判断，也不构成规避风控的保证。\n\n"
    "请确认你有权使用所填手机号及申请语。使用者应自行评估并承担使用本软件带来的账号、"
    "数据及其他后果。"
)


class TaskStopped(Exception):
    """用户终止了当前批量任务。"""


class _MousePoint(ctypes.Structure):
    """低级鼠标钩子使用的屏幕坐标。"""

    _fields_ = (("x", wintypes.LONG), ("y", wintypes.LONG))


class _MouseHookData(ctypes.Structure):
    """Windows ``MSLLHOOKSTRUCT`` 对应结构。"""

    _fields_ = (
        ("pt", _MousePoint),
        ("mouseData", wintypes.DWORD),
        ("flags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.c_size_t),
    )


@dataclass(frozen=True)
class LoggedAccount:
    """一个已登录微信主窗口及其数据库账号。"""

    account: str
    wxid: str
    nickname: str
    hwnd: int
    pid: int

    @property
    def display(self) -> str:
        name = self.nickname or self.wxid or self.account
        return "%s  |  %s" % (name, self.wxid or self.account)


@dataclass(frozen=True)
class TaskConfig:
    """界面提交后不可变的一次任务配置。"""

    account: LoggedAccount
    message: str
    label: str
    interval: float
    phones: Tuple[str, ...]


def parse_task_values(message: str, label: str, interval: str,
                      phone_text: str) -> Tuple[str, str, float, Tuple[str, ...]]:
    """校验界面配置并生成适合任务线程使用的值。"""
    label = (label or "").strip()
    try:
        seconds = float(str(interval).strip())
    except ValueError as exc:
        raise ValueError("间隔时间必须是数字") from exc
    if seconds < 0:
        raise ValueError("间隔时间不能小于 0 秒")
    phones = tuple(collect_phones([phone_text]))
    if not phones:
        raise ValueError("没有检测到有效的中国大陆手机号")
    return message or "", label, seconds, phones


def _process_accounts(pid: int, db_root: Path) -> Set[str]:
    """读取微信进程当前实际打开的账号数据库目录。"""
    accounts = set()
    for item in psutil.Process(pid).open_files():
        try:
            parts = Path(item.path).resolve().relative_to(db_root).parts
        except ValueError:
            continue
        if len(parts) >= 3 and parts[1].lower() == "db_storage":
            accounts.add(parts[0])
    return accounts


def _account_for_process(pid: int, db_root: Path,
                         account_names: Set[str]) -> Optional[str]:
    """按微信进程内的 wxid 把主窗口绑定到账号目录。

    ``psutil.Process.open_files()`` 在部分 Windows 权限配置下会返回
    ``AccessDenied``，但微信自身的配置对象仍可读出当前进程 wxid。优先使用
    这个进程级标识，避免把多个微信窗口错误地判成“没有登录账号”。
    """
    try:
        from wechatauto.db import extract_master_key_from_cfg
        extracted = extract_master_key_from_cfg(pid)
        wxid = (extracted[2] if extracted else "") or ""
    except Exception:
        wxid = ""
    wxid = wxid.strip()
    if wxid:
        exact = [name for name in account_names
                 if name == wxid or name.startswith(wxid + "_")]
        if exact:
            return sorted(exact)[0]
    try:
        accounts = _process_accounts(pid, db_root)
    except (psutil.Error, OSError):
        accounts = set()
    return sorted(accounts)[0] if len(accounts) == 1 else None


def _is_wechat_main_window(title: str, class_name: str) -> bool:
    """按精确标题和 Qt 主窗类名识别微信窗口，不依赖当前显示/最小化状态。"""
    return (title.strip() in ("微信", "Weixin")
            and class_name.startswith("Qt51514QWindowIcon"))


def discover_logged_accounts() -> List[LoggedAccount]:
    """枚举已登录微信主窗口，并读取每个窗口对应的昵称。"""
    db_dir = auto_detect_db_dir()
    if not db_dir:
        return []
    root = Path(db_dir).resolve()
    pid_accounts: Dict[int, Set[str]] = {}
    try:
        account_names = {
            item.name for item in root.iterdir()
            if item.is_dir() and (item / "db_storage").is_dir()
        }
    except OSError:
        account_names = set()
    windows: List[Tuple[int, int, int]] = []

    def accounts_for(pid: int) -> Set[str]:
        if pid not in pid_accounts:
            try:
                pid_accounts[pid] = _process_accounts(pid, root)
            except (psutil.Error, OSError):
                pid_accounts[pid] = set()
        return pid_accounts[pid]

    def collect(hwnd, _extra):
        title = (win32gui.GetWindowText(hwnd) or "").strip()
        cls = win32gui.GetClassName(hwnd) or ""
        # 重启后微信可能把主窗留在隐藏/最小化状态；这时 IsWindowVisible 及窗口
        # 矩形尺寸不能代表登录状态。用主窗类名、精确标题和进程 wxid 绑定账号。
        if not _is_wechat_main_window(title, cls):
            return True
        _tid, pid = win32process.GetWindowThreadProcessId(hwnd)
        try:
            left, top, right, bottom = win32gui.GetWindowRect(hwnd)
            area = max(0, right - left) * max(0, bottom - top)
        except Exception:
            area = 0
        windows.append((int(hwnd), int(pid), area))
        return True

    win32gui.EnumWindows(collect, None)
    found: Dict[Tuple[str, int], LoggedAccount] = {}
    for hwnd, pid, _area in sorted(windows, key=lambda item: -item[2]):
        account = _account_for_process(pid, root, account_names)
        if not account:
            # 保留旧的多账号枚举作为最后兜底；只有进程级 wxid 和文件句柄都不可读
            # 时才会走到这里，避免把一个窗口绑定到多个历史账号目录。
            candidates = accounts_for(pid)
        else:
            candidates = {account}
        for account in candidates:
            key = (account, hwnd)
            if key in found:
                continue
            wxid = account.rsplit("_", 1)[0]
            nickname = ""
            try:
                info = WeChatDB(db_dir=str(root), account=account).get_self_info()
                wxid = info.get("username") or wxid
                nickname = info.get("nick_name") or ""
            except Exception:
                pass
            found[key] = LoggedAccount(
                account=account, wxid=wxid, nickname=nickname,
                hwnd=hwnd, pid=pid)
    return sorted(found.values(), key=lambda item: (item.nickname, item.wxid))


class TaskController:
    """在线程之间协调运行、暂停、继续和停止。"""

    def __init__(self):
        self._run = threading.Event()
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._active = False

    def activate(self) -> None:
        with self._lock:
            self._active = True
            self._stop.clear()
            self._run.set()

    def pause(self) -> bool:
        with self._lock:
            if not self._active or self._stop.is_set() or not self._run.is_set():
                return False
            self._run.clear()
            return True

    def resume(self) -> bool:
        with self._lock:
            if not self._active or self._stop.is_set() or self._run.is_set():
                return False
            self._run.set()
            return True

    def stop(self) -> None:
        self._stop.set()
        self._run.set()

    def finish(self) -> None:
        with self._lock:
            self._active = False
            self._run.set()

    def checkpoint(self) -> None:
        """在每次界面动作前阻塞暂停状态，或抛出停止信号。"""
        while not self._stop.is_set():
            if self._run.wait(0.15):
                return
        raise TaskStopped()

    @property
    def stopped(self) -> bool:
        return self._stop.is_set()

    @property
    def active(self) -> bool:
        with self._lock:
            return self._active


class PhysicalMouseMonitor:
    """用低级鼠标钩子识别物理移动，并忽略脚本注入事件。"""

    WH_MOUSE_LL = 14
    WM_MOUSEMOVE = 0x0200
    WM_QUIT = 0x0012
    LLMHF_INJECTED = 0x00000001

    def __init__(self, on_move: Callable[[], None]):
        self.on_move = on_move
        self._enabled = False
        self._armed_at = 0.0
        self._last_notice = 0.0
        self._hook = None
        self._thread_id = 0
        self._thread: Optional[threading.Thread] = None
        self._ready = threading.Event()
        self.error = ""
        self._callback_ref = None
        self._user32 = None

    def start(self) -> bool:
        if self._thread and self._thread.is_alive():
            return not self.error
        self._thread = threading.Thread(target=self._run, daemon=True,
                                        name="physical-mouse-monitor")
        self._thread.start()
        self._ready.wait(2.0)
        return bool(self._hook) and not self.error

    def arm(self, delay: float = 1.0) -> None:
        self._armed_at = time.monotonic() + max(0.0, delay)
        self._enabled = True

    def disarm(self) -> None:
        self._enabled = False

    def stop(self) -> None:
        self.disarm()
        if self._thread_id and self._user32 is not None:
            self._user32.PostThreadMessageW(
                self._thread_id, self.WM_QUIT, 0, 0)

    def _run(self) -> None:
        result_type = ctypes.c_ssize_t
        callback_type = ctypes.WINFUNCTYPE(
            result_type, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM)
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        user32.SetWindowsHookExW.argtypes = (
            ctypes.c_int, callback_type, wintypes.HINSTANCE, wintypes.DWORD)
        user32.SetWindowsHookExW.restype = wintypes.HANDLE
        user32.CallNextHookEx.argtypes = (
            wintypes.HANDLE, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM)
        user32.CallNextHookEx.restype = result_type
        user32.UnhookWindowsHookEx.argtypes = (wintypes.HANDLE,)
        user32.UnhookWindowsHookEx.restype = wintypes.BOOL
        user32.PostThreadMessageW.argtypes = (
            wintypes.DWORD, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)
        user32.PostThreadMessageW.restype = wintypes.BOOL
        kernel32.GetModuleHandleW.argtypes = (wintypes.LPCWSTR,)
        kernel32.GetModuleHandleW.restype = wintypes.HMODULE
        self._user32 = user32
        self._thread_id = int(kernel32.GetCurrentThreadId())

        def callback(code, message, data_ptr):
            if code >= 0 and message == self.WM_MOUSEMOVE:
                try:
                    data = ctypes.cast(
                        data_ptr, ctypes.POINTER(_MouseHookData)).contents
                    injected = bool(data.flags & self.LLMHF_INJECTED)
                    now = time.monotonic()
                    if (self._enabled and not injected and now >= self._armed_at
                            and now - self._last_notice >= 0.5):
                        self._last_notice = now
                        self.on_move()
                except Exception:
                    pass
            return user32.CallNextHookEx(self._hook, code, message, data_ptr)

        self._callback_ref = callback_type(callback)
        module = kernel32.GetModuleHandleW(None)
        self._hook = user32.SetWindowsHookExW(
            self.WH_MOUSE_LL, self._callback_ref, module, 0)
        if not self._hook:
            self.error = "Windows 低级鼠标钩子安装失败（错误码 %s）" % (
                ctypes.get_last_error(),)
            self._ready.set()
            return
        self._ready.set()
        msg = wintypes.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))
        user32.UnhookWindowsHookEx(self._hook)
        self._hook = None


class ControlledPhoneAddFriendOps(PhoneAddFriendOps):
    """在每次会改变微信界面的动作前执行任务控制检查。"""

    def __init__(self, controller: TaskController, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.controller = controller

    def _click(self, *args, **kwargs):
        self.controller.checkpoint()
        return super()._click(*args, **kwargs)

    def _type_text(self, *args, **kwargs):
        self.controller.checkpoint()
        return super()._type_text(*args, **kwargs)

    def _wait_for_layout(self, seconds: float) -> None:
        deadline = time.monotonic() + max(0.0, seconds)
        while True:
            self.controller.checkpoint()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            time.sleep(min(0.1, remaining))

    def search_phone(self, *args, **kwargs):
        self.controller.checkpoint()
        return super().search_phone(*args, **kwargs)


class PhoneFriendApp:
    """手机号批量添加好友的 Tk 桌面应用。"""

    DEFAULT_MESSAGE = ""

    def __init__(self, root: tk.Tk):
        self.root = root
        self.events: queue.Queue = queue.Queue()
        self.controller = TaskController()
        self.monitor = PhysicalMouseMonitor(self._physical_mouse_moved)
        self.monitor_ok = False
        self.accounts: Dict[str, LoggedAccount] = {}
        self.worker: Optional[threading.Thread] = None
        self._input_widgets: List[object] = []
        self.auth_client = WechatAuthClient()
        self.auth_user: Optional[AuthUser] = None
        self.auth_token = ""
        self.auth_avatar_photo = None
        self.remaining_points: Optional[int] = None
        self.login_attempt = 0
        self.login_cancel: Optional[threading.Event] = None
        self.qr_dialog: Optional[tk.Toplevel] = None
        self.qr_photo = None
        self.start_pending = False

        self.account_var = tk.StringVar()
        self.label_var = tk.StringVar(value="")
        self.interval_var = tk.StringVar(value="5")
        self.count_var = tk.StringVar(value="有效手机号：0")
        self.status_var = tk.StringVar(value="空闲")

        self._build_ui()
        self._refresh_start_state()
        self.root.protocol("WM_DELETE_WINDOW", self._close)
        self.root.after(100, self._poll_events)
        self.root.after(200, self._start_monitor)
        self.root.after(250, self.refresh_accounts)

    def _build_ui(self) -> None:
        self.root.title("微信手机号批量添加好友 %s" % DESKTOP_VERSION)
        self.root.geometry("1160x740")
        self.root.minsize(980, 640)
        self.root.configure(bg="#f3f5f7")

        style = ttk.Style(self.root)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure("TFrame", background="#f3f5f7")
        style.configure("TLabel", background="#f3f5f7", foreground="#202124",
                        font=("Microsoft YaHei UI", 10))
        style.configure("Header.TLabel", font=("Microsoft YaHei UI", 14, "bold"))
        style.configure("Muted.TLabel", foreground="#6b7280")
        style.configure("TCombobox", padding=5)
        style.configure("TEntry", padding=6)
        style.configure("TSpinbox", padding=6)

        outer = ttk.Frame(self.root, padding=18)
        outer.pack(fill="both", expand=True)
        pane = ttk.Panedwindow(outer, orient="horizontal")
        pane.pack(fill="both", expand=True)

        left = ttk.Frame(pane, padding=(0, 0, 18, 0), width=420)
        right = ttk.Frame(pane, padding=(18, 0, 0, 0), width=680)
        pane.add(left, weight=4)
        pane.add(right, weight=6)

        left.columnconfigure(0, weight=1)
        left.columnconfigure(1, weight=0)
        left.rowconfigure(10, weight=1)
        ttk.Label(left, text="任务配置", style="Header.TLabel").grid(
            row=0, column=0, sticky="w", pady=(0, 14))
        auth_controls = ttk.Frame(left)
        auth_controls.grid(row=0, column=1, sticky="e", pady=(0, 14))
        self.login_button = tk.Button(
            auth_controls, text="微信扫码登录", command=self.open_qr_login,
            bg="#07a35a", fg="white", activebackground="#078a4d",
            activeforeground="white", relief="flat", padx=16, pady=8,
            cursor="hand2")
        self.login_button.grid(row=0, column=0)
        self.profile_button = tk.Button(
            auth_controls, text="", command=self.show_profile_menu,
            bg="#ffffff", fg="#202124", activebackground="#edf3f0",
            relief="flat", padx=8, pady=4, cursor="hand2")
        self.profile_button.grid(row=0, column=0)
        self.profile_button.grid_remove()
        self.quota_var = tk.StringVar(value="剩余额度：--")
        self.quota_label = ttk.Label(
            auth_controls, textvariable=self.quota_var, style="Muted.TLabel")
        self.quota_label.grid(row=0, column=1, padx=(8, 0))
        self.quota_label.grid_remove()

        ttk.Label(left, text="当前登录微信号").grid(
            row=1, column=0, columnspan=2, sticky="w")
        account_row = ttk.Frame(left)
        account_row.grid(row=2, column=0, columnspan=2, sticky="ew", pady=(6, 14))
        account_row.columnconfigure(0, weight=1)
        self.account_combo = ttk.Combobox(
            account_row, textvariable=self.account_var, state="readonly")
        self.account_combo.grid(row=0, column=0, sticky="ew")
        self.refresh_button = ttk.Button(
            account_row, text="刷新", width=7, command=self.refresh_accounts)
        self.refresh_button.grid(row=0, column=1, padx=(8, 0))

        ttk.Label(left, text="申请语").grid(
            row=3, column=0, columnspan=2, sticky="w")
        self.message_text = tk.Text(
            left, height=5, wrap="word", relief="solid", borderwidth=1,
            font=("Microsoft YaHei UI", 10), padx=9, pady=8,
            highlightthickness=0, undo=True)
        self.message_text.grid(
            row=4, column=0, columnspan=2, sticky="ew", pady=(6, 14))
        if self.DEFAULT_MESSAGE:
            self.message_text.insert("1.0", self.DEFAULT_MESSAGE)

        fields = ttk.Frame(left)
        fields.grid(row=5, column=0, columnspan=2, sticky="ew", pady=(0, 14))
        fields.columnconfigure(0, weight=3)
        fields.columnconfigure(1, weight=2)
        ttk.Label(fields, text="标签").grid(row=0, column=0, sticky="w")
        ttk.Label(fields, text="间隔时间（秒）").grid(
            row=0, column=1, sticky="w", padx=(12, 0))
        self.label_entry = ttk.Entry(fields, textvariable=self.label_var)
        self.label_entry.grid(row=1, column=0, sticky="ew", pady=(6, 0))
        self.interval_spin = ttk.Spinbox(
            fields, from_=0, to=3600, increment=1,
            textvariable=self.interval_var, width=10)
        self.interval_spin.grid(
            row=1, column=1, sticky="ew", padx=(12, 0), pady=(6, 0))

        phone_head = ttk.Frame(left)
        phone_head.grid(row=6, column=0, columnspan=2, sticky="ew")
        phone_head.columnconfigure(0, weight=1)
        ttk.Label(phone_head, text="手机号").grid(row=0, column=0, sticky="w")
        ttk.Label(phone_head, textvariable=self.count_var,
                  style="Muted.TLabel").grid(row=0, column=1, sticky="e")
        self.phone_text = tk.Text(
            left, wrap="none", relief="solid", borderwidth=1,
            font=("Consolas", 10), padx=9, pady=8,
            highlightthickness=0, undo=True)
        self.phone_text.grid(
            row=10, column=0, columnspan=2, sticky="nsew", pady=(6, 0))
        self.phone_text.bind("<KeyRelease>", self._update_phone_count)

        right.columnconfigure(0, weight=1)
        right.rowconfigure(2, weight=1)
        log_head = ttk.Frame(right)
        log_head.grid(row=0, column=0, sticky="ew", pady=(0, 14))
        log_head.columnconfigure(0, weight=1)
        ttk.Label(log_head, text="运行日志", style="Header.TLabel").grid(
            row=0, column=0, sticky="w")
        self.status_label = ttk.Label(
            log_head, textvariable=self.status_var, style="Muted.TLabel")
        self.status_label.grid(row=0, column=1, sticky="e")

        self.log_text = scrolledtext.ScrolledText(
            right, state="disabled", wrap="word", relief="solid", borderwidth=1,
            font=("Consolas", 9), padx=10, pady=9, highlightthickness=0,
            background="#ffffff", foreground="#202124")
        self.log_text.grid(row=2, column=0, sticky="nsew")
        self.log_text.tag_configure("info", foreground="#202124")
        self.log_text.tag_configure("success", foreground="#08783e")
        self.log_text.tag_configure("warning", foreground="#9a6700")
        self.log_text.tag_configure("error", foreground="#b42318")

        controls = ttk.Frame(right)
        controls.grid(row=3, column=0, sticky="ew", pady=(14, 0))
        controls.columnconfigure((0, 1, 2, 3), weight=1)
        self.start_button = tk.Button(
            controls, text="开始", command=self.start_task, width=9,
            bg="#07a35a", fg="white", activebackground="#078a4d",
            activeforeground="white", relief="flat", pady=9)
        self.stop_button = tk.Button(
            controls, text="停止", command=self.stop_task, width=9,
            bg="#d64545", fg="white", activebackground="#b93838",
            activeforeground="white", relief="flat", pady=9, state="disabled")
        self.pause_button = tk.Button(
            controls, text="暂停", command=self.pause_task, width=9,
            bg="#e8edf2", fg="#202124", activebackground="#dce3e9",
            relief="flat", pady=9, state="disabled")
        self.resume_button = tk.Button(
            controls, text="继续", command=self.resume_task, width=9,
            bg="#e8edf2", fg="#202124", activebackground="#dce3e9",
            relief="flat", pady=9, state="disabled")
        for column, button in enumerate((self.start_button, self.stop_button,
                                         self.pause_button, self.resume_button)):
            button.grid(row=0, column=column, sticky="ew",
                        padx=(0 if column == 0 else 8, 0))

        self._input_widgets = [
            self.account_combo, self.refresh_button, self.message_text,
            self.label_entry, self.interval_spin, self.phone_text,
        ]

    def show_disclaimer(self) -> None:
        dialog = tk.Toplevel(self.root)
        dialog.title("免责声明")
        dialog.geometry("620x430")
        dialog.minsize(520, 360)
        dialog.transient(self.root)
        dialog.columnconfigure(0, weight=1)
        dialog.rowconfigure(1, weight=1)

        ttk.Label(dialog, text="使用须知", style="Header.TLabel").grid(
            row=0, column=0, sticky="w", padx=20, pady=(18, 10))
        body = scrolledtext.ScrolledText(
            dialog, wrap="word", relief="solid", borderwidth=1,
            font=("Microsoft YaHei UI", 10), padx=12, pady=10,
            highlightthickness=0)
        body.grid(row=1, column=0, sticky="nsew", padx=20)
        body.insert("1.0", DISCLAIMER_TEXT)
        body.configure(state="disabled")
        ttk.Button(dialog, text="关闭", command=dialog.destroy).grid(
            row=2, column=0, sticky="e", padx=20, pady=14)
        self._center_dialog(dialog)
        dialog.grab_set()

    def show_profile_menu(self) -> None:
        menu = tk.Menu(self.root, tearoff=False)
        menu.add_command(label="免责声明", command=self.show_disclaimer)
        menu.add_command(label="联系客服", command=self.show_customer_service)
        menu.add_command(label="检查更新", command=self.check_for_updates)
        menu.add_separator()
        menu.add_command(label="退出登录", command=self.logout_user)
        x = self.profile_button.winfo_rootx()
        y = self.profile_button.winfo_rooty() + self.profile_button.winfo_height()
        try:
            menu.tk_popup(x, y)
        finally:
            menu.grab_release()

    def show_customer_service(self) -> None:
        self._load_client_settings("support")

    def check_for_updates(self) -> None:
        self._load_client_settings("update")

    def _load_client_settings(self, action: str) -> None:
        token = self.auth_token
        if not token:
            messagebox.showwarning("需要登录", "请先登录后再使用此功能。", parent=self.root)
            return

        def worker() -> None:
            try:
                config = self.auth_client.client_config(token)
                qr_data = b""
                if action == "support" and config.get("customerQrCode"):
                    qr_data = self.auth_client.qr_code_bytes(
                        str(config["customerQrCode"]))
                self.events.put(("client_settings", token, action, config, qr_data))
            except WechatAuthError as exc:
                self.events.put(("client_settings_error", token, action, str(exc)))

        threading.Thread(target=worker, daemon=True,
                         name="wechat-client-settings").start()

    def _handle_client_settings(self, action: str, config: Dict[str, object],
                                qr_data: bytes) -> None:
        if action == "support":
            self._show_customer_service(config, qr_data)
            return

        backend_version = str(config.get("version") or "").strip()
        if not backend_version:
            messagebox.showwarning("检查更新", "后台尚未配置桌面端当前版本。",
                                   parent=self.root)
            return
        if backend_version.casefold() == DESKTOP_VERSION.casefold():
            messagebox.showinfo("检查更新", "当前已是最新版本（%s）。" % DESKTOP_VERSION,
                                parent=self.root)
            return

        download_url = str(config.get("downloadUrl") or "").strip()
        parsed = urllib.parse.urlsplit(download_url)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            messagebox.showwarning(
                "发现新版本",
                "当前版本：%s\n最新版本：%s\n\n后台尚未配置有效的下载地址。" %
                (DESKTOP_VERSION, backend_version), parent=self.root)
            return
        if messagebox.askyesno(
                "发现新版本",
                "当前版本：%s\n最新版本：%s\n\n现在打开下载地址吗？" %
                (DESKTOP_VERSION, backend_version), parent=self.root):
            webbrowser.open(download_url)

    def _show_customer_service(self, config: Dict[str, object], qr_data: bytes) -> None:
        wechat_id = str(config.get("customerWechat") or "").strip()
        dialog = tk.Toplevel(self.root)
        dialog.title("联系客服")
        dialog.geometry("380x440")
        dialog.minsize(340, 380)
        dialog.resizable(False, False)
        dialog.transient(self.root)

        content = ttk.Frame(dialog, padding=20)
        content.pack(fill="both", expand=True)
        content.columnconfigure(0, weight=1)
        ttk.Label(content, text="客服微信", style="Header.TLabel").grid(
            row=0, column=0, pady=(0, 12))

        if qr_data:
            try:
                image = Image.open(io.BytesIO(qr_data)).convert("RGB")
                resampling = getattr(Image, "Resampling", Image)
                image.thumbnail((250, 250), resampling.LANCZOS)
                dialog.qr_photo = ImageTk.PhotoImage(image)
                ttk.Label(content, image=dialog.qr_photo).grid(
                    row=1, column=0, pady=(0, 12))
            except Exception:
                ttk.Label(content, text="客服二维码无法显示").grid(
                    row=1, column=0, pady=(0, 12))
        else:
            ttk.Label(content, text="暂未配置客服二维码").grid(
                row=1, column=0, pady=(8, 18))

        ttk.Label(content, text=wechat_id or "暂未配置客服微信号").grid(
            row=2, column=0, pady=(0, 10))
        copy_button = ttk.Button(content, text="复制微信号")
        copy_button.configure(command=lambda: self._copy_customer_wechat(
            dialog, wechat_id, copy_button))
        copy_button.grid(row=3, column=0, pady=(0, 8))
        if not wechat_id:
            copy_button.configure(state="disabled")
        ttk.Button(content, text="关闭", command=dialog.destroy).grid(
            row=4, column=0, pady=(4, 0))
        self._center_dialog(dialog)
        dialog.grab_set()

    def _copy_customer_wechat(self, dialog: tk.Toplevel, wechat_id: str,
                              button: ttk.Button) -> None:
        if not wechat_id:
            return
        dialog.clipboard_clear()
        dialog.clipboard_append(wechat_id)
        button.configure(text="已复制")

    def open_qr_login(self) -> None:
        if self.auth_user is not None or self.login_cancel is not None:
            return
        self.login_attempt += 1
        attempt = self.login_attempt
        cancel = threading.Event()
        self.login_cancel = cancel
        dialog = tk.Toplevel(self.root)
        self.qr_dialog = dialog
        dialog.title("微信扫码登录")
        dialog.geometry("380x520")
        dialog.minsize(380, 520)
        dialog.resizable(False, False)
        dialog.transient(self.root)
        dialog.protocol("WM_DELETE_WINDOW", self.cancel_qr_login)
        frame = ttk.Frame(dialog, padding=(20, 18, 20, 16))
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, text="微信扫码登录", style="Header.TLabel").pack(pady=(4, 8))
        self.qr_status_var = tk.StringVar(value="正在向登录服务申请二维码…")
        ttk.Label(frame, textvariable=self.qr_status_var,
                  style="Muted.TLabel").pack(pady=(0, 12))
        self.qr_image_label = ttk.Label(frame, text="二维码加载中", anchor="center")
        self.qr_image_label.pack(fill="both", expand=True, pady=(0, 10))
        ttk.Button(frame, text="取消", command=self.cancel_qr_login).pack(
            pady=(0, 0), ipadx=28, ipady=3)
        self._center_dialog(dialog)
        dialog.grab_set()
        dialog.lift()
        dialog.focus_force()
        threading.Thread(
            target=self._qr_login_worker, args=(attempt, cancel), daemon=True,
            name="wechat-qr-login").start()

    def _center_dialog(self, dialog: tk.Toplevel) -> None:
        """将弹窗定位到主应用窗口中心。"""
        try:
            self.root.update_idletasks()
            dialog.update_idletasks()
            width = dialog.winfo_width()
            height = dialog.winfo_height()
            parent_x = self.root.winfo_rootx()
            parent_y = self.root.winfo_rooty()
            parent_width = self.root.winfo_width()
            parent_height = self.root.winfo_height()
            x = parent_x + max(0, (parent_width - width) // 2)
            y = parent_y + max(0, (parent_height - height) // 2)
            dialog.geometry("%dx%d+%d+%d" % (width, height, x, y))
        except tk.TclError:
            # 主窗口正在关闭时，保留 Tk 的默认定位即可。
            pass

    def cancel_qr_login(self) -> None:
        self.login_attempt += 1
        if self.login_cancel is not None:
            self.login_cancel.set()
        self.login_cancel = None
        dialog, self.qr_dialog = self.qr_dialog, None
        self.qr_photo = None
        if dialog is not None:
            try:
                dialog.grab_release()
                dialog.destroy()
            except tk.TclError:
                pass

    def _qr_login_worker(self, attempt: int, cancel: threading.Event) -> None:
        try:
            qr = self.auth_client.create_qr()
            qr_bytes = self.auth_client.qr_image_bytes(qr.image)
            self.events.put(("auth_qr", attempt, qr_bytes))
            self.events.put(("auth_qr_status", attempt, "请使用微信扫描二维码"))
            while not cancel.is_set():
                data = self.auth_client.poll(qr.ticket)
                status = str(data.get("status") or data.get("state") or "WAITING").upper()
                if status in {"SCANNED", "SCANED", "CONFIRMED_SCANNED"}:
                    self.events.put(("auth_qr_status", attempt,
                                    "已扫码，请点击微信页面底部的“使用完整服务”授权个人信息"))
                elif status == "WAITING_FULL_SERVICE":
                    self.events.put(("auth_qr_status", attempt,
                                     str(data.get("msg") or "已扫码，请点击“使用完整服务”完成授权")))
                elif status in {"EXPIRED", "TIMEOUT"}:
                    raise WechatAuthError("二维码已过期，请重新扫码")
                elif status in {"FAILED", "ERROR"}:
                    raise WechatAuthError(str(data.get("msg") or "服务号授权失败"))
                elif status in {"CANCELLED", "CANCELED", "DENIED"}:
                    raise WechatAuthError("已取消本次扫码登录")
                elif status == "CONFIRMED":
                    token = str(data.get("token") or data.get("Authorization") or "").strip()
                    try:
                        user = self.auth_client.resolve_user(token, data)
                    except WechatAuthError as exc:
                        self.events.put(("auth_qr_status", attempt, str(exc)))
                        if cancel.wait(1.5):
                            return
                        continue
                    avatar_data = b""
                    if user.avatar:
                        try:
                            avatar_data = self.auth_client.avatar_bytes(user.avatar)
                        except WechatAuthError:
                            pass
                    self.events.put(("auth_success", attempt, user, avatar_data))
                    return
                if cancel.wait(1.5):
                    return
        except Exception as exc:
            if not cancel.is_set():
                self.events.put(("auth_error", attempt, str(exc)))

    def _apply_qr_image(self, attempt: int, payload: bytes) -> None:
        if attempt != self.login_attempt or self.qr_dialog is None:
            return
        try:
            image = Image.open(io.BytesIO(payload)).convert("RGB")
            resampling = getattr(Image, "Resampling", Image)
            image.thumbnail((270, 270), resampling.LANCZOS)
            self.qr_photo = ImageTk.PhotoImage(image)
            self.qr_image_label.configure(image=self.qr_photo, text="")
        except Exception as exc:
            self._finish_qr_error(attempt, "二维码图片无法显示：%s" % exc)

    @staticmethod
    def _avatar_image(payload: bytes, nickname: str):
        image = None
        if payload:
            try:
                image = Image.open(io.BytesIO(payload)).convert("RGB")
                resampling = getattr(Image, "Resampling", Image)
                image = ImageOps.fit(image, (40, 40), method=resampling.LANCZOS)
            except Exception:
                image = None
        if image is None:
            image = Image.new("RGB", (40, 40), "#d8eee3")
            draw = ImageDraw.Draw(image)
            draw.ellipse((1, 1, 39, 39), fill="#d8eee3")
            draw.text((12, 10), (nickname or "?")[:1], fill="#08783e")
        return ImageTk.PhotoImage(image)

    def _apply_auth_success(self, attempt: int, user: AuthUser,
                            avatar_data: bytes) -> None:
        if attempt != self.login_attempt:
            return
        self.auth_user = user
        self.auth_token = user.token
        self.remaining_points = None
        self.auth_avatar_photo = self._avatar_image(avatar_data, user.nickname)
        self.profile_button.configure(
            text="  " + user.nickname, image=self.auth_avatar_photo, compound="left")
        self.profile_button.grid()
        self.login_button.grid_remove()
        self.quota_var.set("剩余额度：读取中…")
        self.quota_label.grid()
        self.login_cancel = None
        dialog, self.qr_dialog = self.qr_dialog, None
        self.qr_photo = None
        if dialog is not None:
            try:
                dialog.grab_release()
                dialog.destroy()
            except tk.TclError:
                pass
        self._refresh_start_state()
        self._log("扫码登录成功：%s" % user.nickname, "success")
        threading.Thread(
            target=self._load_quota_after_login,
            args=(attempt, user.token),
            daemon=True,
            name="wechat-user-info").start()

    def _load_quota_after_login(self, attempt: int, token: str) -> None:
        """登录成功后只主动读取一次用户额度，首次读取由后台完成用户初始化。"""
        try:
            info = self.auth_client.helper_user_info(token)
            value = info.get("balance", info.get("remainingPoints"))
            if value is None:
                raise WechatAuthError("用户信息接口未返回剩余额度")
            self.events.put(("quota_balance", token, int(value), attempt))
        except (ValueError, TypeError) as exc:
            self.events.put(("quota_error", token, "剩余额度格式无效：%s" % exc, attempt))
        except WechatAuthError as exc:
            self.events.put(("quota_error", token, str(exc), attempt))

    def _finish_qr_error(self, attempt: int, message: str) -> None:
        if attempt != self.login_attempt:
            return
        self.login_cancel = None
        dialog, self.qr_dialog = self.qr_dialog, None
        self.qr_photo = None
        if dialog is not None:
            try:
                dialog.grab_release()
                dialog.destroy()
            except tk.TclError:
                pass
        self._log(message, "error")
        messagebox.showerror("微信扫码登录", message, parent=self.root)

    def logout_user(self) -> None:
        if self.auth_user is None or self.controller.active:
            return
        if not messagebox.askyesno("退出登录", "确定退出 %s 的登录吗？" % self.auth_user.nickname,
                                   parent=self.root):
            return
        token = self.auth_token
        nickname = self.auth_user.nickname
        self.auth_user = None
        self.auth_token = ""
        self.auth_avatar_photo = None
        self.remaining_points = None
        self.profile_button.configure(image="", text="")
        self.profile_button.grid_remove()
        self.login_button.grid()
        self.quota_label.grid_remove()
        self.quota_var.set("剩余额度：--")
        self._refresh_start_state()
        self._log("已退出登录：%s" % nickname, "warning")
        threading.Thread(target=self._logout_worker, args=(token,), daemon=True,
                         name="wechat-auth-logout").start()

    def _logout_worker(self, token: str) -> None:
        try:
            self.auth_client.logout(token)
        except WechatAuthError as exc:
            self.events.put(("log", "warning", "服务端退出登录未确认：%s" % exc))

    def _verify_auth_worker(self, config: TaskConfig, token: str) -> None:
        try:
            self.auth_client.user_info(token)
            info = self.auth_client.helper_user_info(token)
            remaining = int(info.get("balance", 0))
            if remaining < 1:
                self.events.put(("quota_insufficient", config, token, remaining))
                return
            self.events.put(("auth_verified", config, token, ""))
        except WechatQuotaError as exc:
            self.events.put(("quota_insufficient", config, token, str(exc)))
        except WechatAuthError as exc:
            self.events.put(("auth_verified", config, token, str(exc)))

    def _start_monitor(self) -> None:
        self.monitor_ok = self.monitor.start()
        if self.monitor_ok:
            self._log("人工鼠标移动监测已就绪。", "info")
        else:
            self._log("鼠标监测不可用：%s" % self.monitor.error, "error")

    def refresh_accounts(self) -> None:
        if self.controller.active:
            return
        self.refresh_button.configure(state="disabled")
        self.account_var.set("正在读取已登录账号...")
        self._log("正在扫描已登录微信账号...", "info")

        def worker():
            try:
                self.events.put(("accounts", discover_logged_accounts(), ""))
            except Exception as exc:
                self.events.put(("accounts", [], str(exc)))

        threading.Thread(target=worker, daemon=True,
                         name="wechat-account-scan").start()

    def _update_phone_count(self, _event=None) -> None:
        phones = collect_phones([self.phone_text.get("1.0", "end")])
        self.count_var.set("有效手机号：%d" % len(phones))

    def _selected_account(self) -> Optional[LoggedAccount]:
        return self.accounts.get(self.account_var.get())

    def start_task(self) -> None:
        if self.controller.active:
            return
        if self.auth_user is None or not self.auth_token:
            self.open_qr_login()
            return
        if not self.monitor_ok:
            messagebox.showerror("无法开始", "人工鼠标移动监测未启动，请重新打开界面。")
            return
        account = self._selected_account()
        if account is None:
            messagebox.showerror("无法开始", "请选择一个当前已登录的微信账号。")
            return
        try:
            message, label, interval, phones = parse_task_values(
                self.message_text.get("1.0", "end-1c"), self.label_var.get(),
                self.interval_var.get(), self.phone_text.get("1.0", "end"))
        except ValueError as exc:
            messagebox.showerror("配置错误", str(exc))
            return
        config = TaskConfig(account, message, label, interval, phones)
        self.start_pending = True
        self._refresh_start_state()
        self.status_var.set("校验登录状态…")
        self._log("正在校验登录状态…", "info")
        threading.Thread(
            target=self._verify_auth_worker,
            args=(config, self.auth_token), daemon=True,
            name="wechat-auth-check").start()

    def _begin_task(self, config: TaskConfig, token: str) -> None:
        self.controller.activate()
        self.monitor.arm(delay=1.2)
        self._set_state("running", "运行中")
        self._set_inputs_enabled(False)
        self._log("开始任务：账号 %s，共 %d 个手机号，间隔 %.1f 秒，标签“%s”。" %
                  (config.account.nickname or config.account.wxid,
                   len(config.phones), config.interval, config.label or "未设置"),
                  "info")
        self.worker = threading.Thread(
            target=self._run_task, args=(config, token), daemon=True,
            name="phone-friend-worker")
        self.worker.start()

    def pause_task(self) -> None:
        if self.controller.pause():
            self.monitor.disarm()
            self._set_state("paused", "已暂停")
            self._log("任务已手动暂停。", "warning")

    def resume_task(self) -> None:
        if self.controller.resume():
            self.monitor.arm(delay=1.2)
            self._set_state("running", "运行中")
            self._log("任务继续运行。", "info")

    def stop_task(self) -> None:
        if not self.controller.active:
            return
        self.monitor.disarm()
        self.controller.stop()
        self._set_state("stopping", "正在停止")
        self._log("收到停止指令，正在结束当前操作。", "warning")

    def _physical_mouse_moved(self) -> None:
        if self.controller.pause():
            self.monitor.disarm()
            self.events.put(("state", "paused", "检测到人工鼠标移动，任务已暂停。"))

    def _run_task(self, config: TaskConfig, token: str) -> None:
        sent = failed = skipped = 0
        ops = None
        finish_kind = "completed"
        finish_text = "任务完成"
        try:
            if not win32gui.IsWindow(config.account.hwnd):
                raise RuntimeError("所选微信窗口已经关闭，请刷新账号列表")
            uia = WeChatUIA(hwnd=config.account.hwnd)
            if not uia.ensure_materialized(timeout=8.0):
                raise RuntimeError("微信 UIA 控件树未成功激活")
            ops = ControlledPhoneAddFriendOps(
                self.controller, uia=uia,
                db=WeChatDB(account=config.account.account))

            for index, phone in enumerate(config.phones, 1):
                self.controller.checkpoint()
                info = self.auth_client.helper_user_info(token)
                remaining = int(info.get("balance", 0))
                if remaining < 1:
                    finish_kind = "quota"
                    finish_text = "剩余额度不足，请联系客服。"
                    self._queue_log(finish_text, "warning")
                    self.controller.stop()
                    break
                masked = mask_phone(phone)
                request_id = str(uuid.uuid4())
                self._queue_log("[%d/%d] 正在处理 %s" %
                                (index, len(config.phones), masked), "info")
                while True:
                    result = ops.add_phone(
                        phone, message=config.message or None, dry_run=False,
                        label=config.label or None)
                    if self.controller.stopped:
                        raise TaskStopped()
                    reason = result.get("reason", "未知原因")
                    if reason.startswith("occluded"):
                        self._queue_log(
                            "%s 的微信操作区域被其他窗口遮挡，任务已暂停；移开窗口后点击继续。"
                            % masked, "warning")
                        self.controller.pause()
                        self.monitor.disarm()
                        self.events.put(("task_state", "paused"))
                        self.controller.checkpoint()
                        continue
                    break

                if reason.startswith("send-unconfirmed"):
                    finish_kind = "error"
                    finish_text = "发送结果无法确认，为避免重复申请已终止任务。"
                    self._queue_log("%s：%s" % (masked, reason), "error")
                    self.controller.stop()
                    break

                added = bool(result.get("added"))
                report = {
                    "requestId": request_id,
                    "wxid": config.account.wxid,
                    "phone": phone,
                    "tag": config.label,
                    "applyMessage": config.message,
                    "sendStatus": 1 if added else 0,
                    "exceptionInfo": "" if added else str(reason)[:1000],
                }
                try:
                    response = self.auth_client.report_phone_result(token, report)
                except WechatQuotaError:
                    finish_kind = "quota"
                    finish_text = "申请已在微信发送，但额度扣减失败；任务已停止，请联系客服核对。"
                    self._queue_log(finish_text, "error")
                    self.controller.stop()
                    break
                except WechatAuthError as exc:
                    finish_kind = "error"
                    finish_text = "操作结果未能写入后台，任务已停止：%s" % exc
                    self._queue_log(finish_text, "error")
                    self.controller.stop()
                    break

                remaining_value = response.get("remainingPoints")
                try:
                    if remaining_value is not None:
                        self.events.put(("quota_balance", token,
                                         int(remaining_value), 0))
                except (TypeError, ValueError):
                    pass

                if result.get("risk_control"):
                    finish_kind = "risk"
                    finish_text = "检测到微信风控，任务已终止：%s" % reason
                    self._queue_log(finish_text, "error")
                    self.controller.stop()
                    break
                if added:
                    sent += 1
                    self._queue_log("%s：好友申请已发送，剩余额度 %s。" %
                                    (masked, response.get("remainingPoints", "未知")), "success")
                elif reason == "already-friend":
                    skipped += 1
                    self._queue_log("%s：已经是好友，已跳过。" % masked, "warning")
                else:
                    failed += 1
                    self._queue_log("%s：失败，%s" % (masked, reason), "error")

                if index < len(config.phones):
                    self._wait_interval(config.interval)
        except TaskStopped:
            if finish_kind == "completed":
                finish_kind = "stopped"
                finish_text = "任务已停止"
        except Exception as exc:
            finish_kind = "error"
            finish_text = "任务异常：%s" % exc
            self._queue_log(finish_text, "error")
        finally:
            if ops is not None:
                try:
                    ops.close_transients()
                    ops._clear_search()
                except Exception:
                    pass
            self.events.put(("done", finish_kind, finish_text,
                             sent, failed, skipped))

    def _wait_interval(self, seconds: float) -> None:
        remaining = max(0.0, seconds)
        while remaining > 0:
            self.controller.checkpoint()
            step = min(0.2, remaining)
            time.sleep(step)
            remaining -= step

    def _queue_log(self, text: str, level: str = "info") -> None:
        self.events.put(("log", level, text))

    def _log(self, text: str, level: str = "info") -> None:
        stamp = datetime.now().strftime("%H:%M:%S")
        self.log_text.configure(state="normal")
        self.log_text.insert("end", "[%s] %s\n" % (stamp, text), level)
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def _poll_events(self) -> None:
        try:
            while True:
                item = self.events.get_nowait()
                kind = item[0]
                if kind == "log":
                    self._log(item[2], item[1])
                elif kind == "accounts":
                    self._apply_accounts(item[1], item[2])
                elif kind == "auth_qr":
                    self._apply_qr_image(item[1], item[2])
                elif kind == "auth_qr_status":
                    if (item[1] == self.login_attempt and self.qr_dialog is not None):
                        self.qr_status_var.set(item[2])
                elif kind == "auth_success":
                    self._apply_auth_success(item[1], item[2], item[3])
                elif kind == "auth_error":
                    self._finish_qr_error(item[1], item[2])
                elif kind == "quota_balance":
                    token, balance, attempt = item[1], item[2], item[3]
                    if token == self.auth_token and (attempt == 0 or attempt == self.login_attempt):
                        self.remaining_points = balance
                        self.quota_var.set("剩余额度：%d" % balance)
                        self._log("当前剩余额度：%d。" % balance, "success")
                elif kind == "quota_error":
                    token, message, attempt = item[1], item[2], item[3]
                    if token == self.auth_token and (attempt == 0 or attempt == self.login_attempt):
                        self.quota_var.set("剩余额度：获取失败")
                        self._log("读取用户额度失败：%s" % message, "warning")
                elif kind == "client_settings":
                    token, action, config, qr_data = item[1:]
                    if token == self.auth_token:
                        self._handle_client_settings(action, config, qr_data)
                elif kind == "client_settings_error":
                    token, action, message = item[1:]
                    if token == self.auth_token:
                        title = "联系客服" if action == "support" else "检查更新"
                        messagebox.showerror(title, message, parent=self.root)
                elif kind == "auth_verified":
                    config, token, error = item[1], item[2], item[3]
                    self.start_pending = False
                    if error:
                        still_current = token == self.auth_token
                        if still_current:
                            self.auth_user = None
                            self.auth_token = ""
                            self.auth_avatar_photo = None
                            self.remaining_points = None
                            self.profile_button.configure(image="", text="")
                            self.profile_button.grid_remove()
                            self.login_button.grid()
                            self.quota_label.grid_remove()
                            self.quota_var.set("剩余额度：--")
                        self._refresh_start_state()
                        self.status_var.set("空闲")
                        self._log("登录已失效，请重新扫码登录：%s" % error, "error")
                        if still_current:
                            messagebox.showwarning("登录已失效", "请重新扫码登录后再开始任务。")
                    elif self.auth_user is not None and token == self.auth_token:
                        self._begin_task(config, token)
                    else:
                        self.status_var.set("空闲")
                        self._refresh_start_state()
                elif kind == "quota_insufficient":
                    self.start_pending = False
                    self.status_var.set("额度不足")
                    self._refresh_start_state()
                    self._log("剩余额度不足，请联系客服。", "warning")
                    messagebox.showwarning("额度不足", "剩余额度不足，请联系客服。", parent=self.root)
                elif kind == "state":
                    self._set_state(item[1], "已暂停")
                    self._log(item[2], "warning")
                elif kind == "task_state":
                    self._set_state(item[1], "已暂停")
                elif kind == "done":
                    self._task_done(*item[1:])
        except queue.Empty:
            pass
        self.root.after(100, self._poll_events)

    def _apply_accounts(self, accounts: List[LoggedAccount], error: str) -> None:
        self.refresh_button.configure(state="normal")
        self.accounts = {item.display: item for item in accounts}
        values = list(self.accounts)
        self.account_combo.configure(values=values)
        if values:
            preferred = next((value for value in values
                              if "wxid_cypd7iklidsh29" in value), values[0])
            self.account_var.set(preferred)
            self._log("已发现 %d 个登录账号。" % len(values), "success")
        else:
            self.account_var.set("")
            self._log(error or "没有发现已登录的微信主窗口。", "error")

    def _task_done(self, kind: str, text: str, sent: int,
                   failed: int, skipped: int) -> None:
        self.monitor.disarm()
        self.controller.finish()
        self._set_inputs_enabled(True)
        self._set_state("idle", "空闲")
        level = "success" if kind == "completed" else (
            "warning" if kind == "stopped" else "error")
        self._log("%s；已发送 %d，失败 %d，跳过 %d。" %
                  (text, sent, failed, skipped), level)
        if kind == "quota":
            messagebox.showwarning("额度不足", text, parent=self.root)

    def _set_inputs_enabled(self, enabled: bool) -> None:
        combo_state = "readonly" if enabled else "disabled"
        entry_state = "normal" if enabled else "disabled"
        self.account_combo.configure(state=combo_state)
        self.refresh_button.configure(state=entry_state)
        self.message_text.configure(state=entry_state)
        self.label_entry.configure(state=entry_state)
        self.interval_spin.configure(state=entry_state)
        self.phone_text.configure(state=entry_state)
        self.login_button.configure(state=entry_state)
        self.profile_button.configure(state=entry_state)
        if enabled:
            self._refresh_start_state()

    def _set_state(self, state: str, label: str) -> None:
        self.status_var.set(label)
        states = {
            "idle": ("normal", "disabled", "disabled", "disabled"),
            "running": ("disabled", "normal", "normal", "disabled"),
            "paused": ("disabled", "normal", "disabled", "normal"),
            "stopping": ("disabled", "disabled", "disabled", "disabled"),
        }
        start, stop, pause, resume = states.get(state, states["idle"])
        if state == "idle" and self.start_pending:
            start = "disabled"
        self.start_button.configure(state=start)
        self.stop_button.configure(state=stop)
        self.pause_button.configure(state=pause)
        self.resume_button.configure(state=resume)

    def _refresh_start_state(self) -> None:
        if not self.controller.active:
            self.start_button.configure(
                state="normal" if not self.start_pending else "disabled")

    def _close(self) -> None:
        self.controller.stop()
        if self.login_cancel is not None:
            self.login_cancel.set()
        self.monitor.stop()
        self.root.destroy()


def main() -> None:
    """启动桌面界面。"""
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass
    root = tk.Tk()
    PhoneFriendApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
