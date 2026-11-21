# -*- coding: utf-8 -*-
"""Facebook search and profiles through OpenCLI in the shared Agent Reach browser."""

from __future__ import annotations

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
        Action(
            "react",
            "React to a post or comment",
            (
                Param("url", "facebook.com post or comment URL"),
                # Flag form (--type love) per the story's acceptance command;
                # required=False so argparse accepts --type, Python still
                # rejects a missing type with BAD_INPUT.
                Param("type", "like | love | haha | wow | sad | angry", required=False),
            ),
            (("opencli", "_react"),),
        ),
        Action(
            "unreact",
            "Remove your reaction from a post or comment",
            (Param("url", "facebook.com post or comment URL"),),
            (("opencli", "_unreact"),),
        ),
        Action(
            "comment",
            "Comment on a Facebook post",
            (
                Param("url", "facebook.com post URL"),
                Param("text", "comment content"),
            ),
            (("opencli", "_comment"),),
        ),
        Action(
            "uncomment",
            "Delete your own Facebook comment",
            (Param("url", "comment permalink (?comment_id=…) or raw comment id"),),
            (("opencli", "_uncomment"),),
        ),
        Action(
            "post-edit",
            "Edit your own Facebook post text (audience preserved)",
            (
                Param("url", "facebook.com post permalink or raw story id"),
                Param("text", "new post content"),
            ),
            (("opencli", "_post_edit"),),
        ),
        Action(
            "post-delete",
            "Delete your own Facebook post",
            (
                Param("url", "facebook.com post permalink or raw story id"),
                Param("confirm", "pass --confirm to delete permanently", required=False, type=bool, default=False),
            ),
            (("opencli", "_post_delete"),),
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

    def _react(self, *, url: str, type: str | None, timeout: float) -> list:
        from agent_reach.runtime.result import BAD_INPUT, ReachError

        if type not in ("like", "love", "haha", "wow", "sad", "angry"):
            raise ReachError(BAD_INPUT, "type must be one of: like, love, haha, wow, sad, angry")
        from ._facebook_react import facebook_react

        return facebook_react(url=url, reaction=type, timeout=timeout)

    def _unreact(self, *, url: str, timeout: float) -> list:
        from ._facebook_react import facebook_unreact

        return facebook_unreact(url=url, timeout=timeout)

    def _comment(self, *, url: str, text: str, timeout: float) -> list:
        from ._facebook_comment import facebook_comment

        return facebook_comment(url=url, text=text, timeout=timeout)

    def _uncomment(self, *, url: str, timeout: float) -> list:
        from ._facebook_comment import facebook_uncomment

        return facebook_uncomment(url=url, timeout=timeout)

    def _post_edit(self, *, url: str, text: str, timeout: float) -> list:
        from ._facebook_post_edit import facebook_post_edit

        return facebook_post_edit(url=url, text=text, timeout=timeout)

    def _post_delete(self, *, url: str, confirm: bool, timeout: float) -> list:
        from ._facebook_post_edit import facebook_post_delete

        return facebook_post_delete(url=url, confirm=confirm, timeout=timeout)
