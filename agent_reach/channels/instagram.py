# -*- coding: utf-8 -*-
"""Instagram users, profiles and posts through OpenCLI in the shared Agent Reach browser."""

from agent_reach.runtime.result import BAD_INPUT, ReachError

from ._opencli_site import OpenCLISiteChannel
from .base import Action, Param


class InstagramChannel(OpenCLISiteChannel):
    name = "instagram"
    description = "Instagram 用户、主页和指定用户帖子"
    site = "instagram"
    domains = ("instagram.com", "instagr.am")
    usage = "opencli instagram search/profile/user/explore -f yaml"
    login_hint = "instagram.com"
    login_cookie = "sessionid"

    level = "experimental"
    login_url = "https://www.instagram.com/accounts/login/"
    actions = (
        Action(
            "search",
            "Search users (not posts)",
            (
                Param("query", "what to search for"),
                Param("limit", "max results", required=False, type=int, default=5),
            ),
            (("opencli", "_opencli_search"),),
            live_test={"query": "nasa", "limit": 2},
        ),
        Action(
            "profile",
            "A user's profile",
            (Param("username", "Instagram username"),),
            (("opencli", "_opencli_profile"),),
            live_test={"username": "nasa"},
        ),
        Action(
            "posts",
            "A user's recent posts",
            (
                Param("username", "Instagram username"),
                Param("limit", "max posts", required=False, type=int, default=6),
            ),
            (("opencli", "_opencli_posts"),),
            live_test={"username": "nasa", "limit": 2},
        ),
        Action(
            "comment",
            "Comment on a post/reel (logged-in instagram.com session)",
            (
                Param("target", "post/reel URL, shortcode, or numeric media id"),
                Param("text", "comment body"),
            ),
            # "opencli" so the dispatcher serializes this with other browser traffic.
            (("opencli", "_comment"),),
        ),
        Action(
            "like",
            "Like a post/reel (logged-in instagram.com session)",
            (Param("target", "post/reel URL, shortcode, or numeric media id"),),
            (("opencli", "_like"),),
        ),
        Action(
            "unlike",
            "Remove your like from a post/reel",
            (Param("target", "post/reel URL, shortcode, or numeric media id"),),
            (("opencli", "_unlike"),),
        ),
        Action(
            "post",
            "Publish a post — DESCOPED: media pipeline (rupload_igphoto + media/configure) not implemented; instagram.com has no text-only post",
            (),
            (("opencli", "_post"),),
        ),
    )

    def _opencli_search(self, *, query: str, limit: int, timeout: float) -> list:
        return self._opencli(["search", query, "--limit", str(limit)], timeout)[:limit]

    def _opencli_profile(self, *, username: str, timeout: float) -> list:
        return self._opencli(["profile", username], timeout, empty_is_error=False)[:1]

    def _opencli_posts(self, *, username: str, limit: int, timeout: float) -> list:
        return self._opencli(["user", username, "--limit", str(limit)], timeout)[:limit]

    def _comment(self, *, target: str, text: str, timeout: float) -> list:
        from ._instagram_write import ig_comment

        return ig_comment(target=target, text=text, timeout=timeout)

    def _like(self, *, target: str, timeout: float) -> list:
        from ._instagram_write import ig_like

        return ig_like(target=target, unlike=False, timeout=timeout)

    def _unlike(self, *, target: str, timeout: float) -> list:
        from ._instagram_write import ig_like

        return ig_like(target=target, unlike=True, timeout=timeout)

    def _post(self, *, timeout: float) -> list:
        raise ReachError(
            BAD_INPUT,
            "instagram post is descoped: the media pipeline (PUT /rupload_igphoto/ + POST /api/v1/media/configure/) "
            "is not implemented and instagram.com has no text-only post — use `instagram comment` / `instagram like`",
        )
