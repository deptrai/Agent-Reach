# -*- coding: utf-8 -*-
"""Threads (Meta) — text post write lane via REST replay in the dedicated browser.

Threads has its own session (threads.com cookies, "continue with Meta" flow).
No read actions yet — the opencli adapter does not cover threads.com.
"""

from ._opencli_site import OpenCLISiteChannel
from .base import Action, Param


class ThreadsChannel(OpenCLISiteChannel):
    name = "threads"
    description = "Threads 文本发帖（须登录专用浏览器）"
    site = "threads"
    domains = ("threads.com", "threads.net")
    usage = "threads post (write lane only; no opencli adapter)"
    login_hint = "threads.com"
    login_cookie = "sessionid"

    level = "experimental"
    login_url = "https://www.threads.com/login"
    actions = (
        Action(
            "post",
            "Publish a text-only Threads post (logged-in threads.com session)",
            (Param("text", "post text, ≤500 chars"),),
            # "opencli" so the dispatcher serializes this with other browser traffic.
            (("opencli", "_post"),),
        ),
    )

    def _post(self, *, text: str, timeout: float) -> list:
        from ._threads_write import threads_post

        return threads_post(text=text, timeout=timeout)
