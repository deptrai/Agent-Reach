# -*- coding: utf-8 -*-
"""Facebook search and profiles through OpenCLI in the shared Agent Reach browser."""

from ._opencli_site import OpenCLISiteChannel
from .base import Action, Param


class FacebookChannel(OpenCLISiteChannel):
    name = "facebook"
    description = "Facebook 帖子、主页和群组"
    site = "facebook"
    domains = ("facebook.com", "fb.com", "fb.watch")
    usage = "opencli facebook search/profile/feed/groups -f yaml"
    login_hint = "facebook.com"
    login_cookie = "c_user"

    level = "experimental"
    login_url = "https://www.facebook.com/login"
    actions = (
        Action(
            "search",
            "Search Facebook",
            (
                Param("query", "what to search for"),
                Param("limit", "max results", required=False, type=int, default=5),
            ),
            (("opencli", "_opencli_search"),),
            live_test={"query": "openai", "limit": 2},
            public_web=("facebook.com/",),
        ),
        Action(
            "profile",
            "A user's or page's profile",
            (Param("username", "username or page name"),),
            (("opencli", "_opencli_profile"),),
            live_test={"username": "zuck"},
        ),
        Action(
            "post",
            "Post a text status update",
            (
                Param("text", "post content"),
                Param("audience", "only_me | friends | public", required=False, default="only_me"),
            ),
            # CDP inside the shared browser (backend label "opencli" so the
            # dispatcher serializes it with other browser traffic).
            (("opencli", "_post"),),
        ),
    )

    def _opencli_search(self, *, query: str, limit: int, timeout: float) -> list:
        return self._opencli(["search", query, "--limit", str(limit)], timeout)[:limit]

    def _opencli_profile(self, *, username: str, timeout: float) -> list:
        return self._opencli(["profile", username], timeout, empty_is_error=False)[:1]

    def _post(self, *, text: str, audience: str, timeout: float) -> list:
        if audience not in ("only_me", "friends", "public"):
            from agent_reach.runtime.result import BAD_INPUT, ReachError

            raise ReachError(BAD_INPUT, "audience must be one of: only_me, friends, public")
        from ._facebook_write import facebook_post

        return facebook_post(text=text, audience=audience, timeout=timeout)
