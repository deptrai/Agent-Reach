"""Unit tests for scripts/capture_mutation.py --platform x — X body/header
sanitize, op/queryId URL extraction, GET-vs-POST body parsing, out naming,
plus the capture_fb_mutation shim's re-export surface."""

import importlib.util
import json
import urllib.parse
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, _SCRIPTS / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


cap = _load("capture_mutation")
shim = _load("capture_fb_mutation")


def _x_rec(**over):
    rec = {
        "n": "CreateTweet",
        "u": "https://x.com/i/api/graphql/AbCdEf123/CreateTweet",
        "q": "AbCdEf123",
        "m": "POST",
        "h": {
            "authorization": "Bearer AAAAAREALTOKEN%3D",
            "x-csrf-token": "144f9e9brealcsrf",
            "x-client-transaction-id": "realtxid123",
            "x-twitter-auth-type": "OAuth2Session",
            "x-twitter-active-user": "yes",
            "x-twitter-client-language": "en",
            "content-type": "application/json",
            "cookie": "auth_token=REALSECRET; ct0=REALSECRET",
        },
        "b": json.dumps({
            "variables": {
                "tweet_text": "hello skeleton",
                "tweet_id": "1900123456789012345",
                "rest_id": "987654321",
                "in_reply_to_tweet_id": "1800111222333444555",
                "media": {"media_entities": [], "possibly_sensitive": False},
                "semantic_annotation_ids": [],
                "composer_session_id": "f6023f7d-aaaa-bbbb-cccc-1234567890ab",
                "auth_token": "REALSECRET",
            },
            "features": {"communities_web_enable_tweet_community_results_fetch": True},
            "queryId": "AbCdEf123",
        }),
    }
    rec.update(over)
    return rec


class TestXOpFromUrl:
    def test_post_path(self):
        assert cap.x_op_from_url("https://x.com/i/api/graphql/QID123/CreateTweet") == (
            "QID123", "CreateTweet"
        )

    def test_get_with_query(self):
        qid, op = cap.x_op_from_url(
            "https://x.com/i/api/graphql/KmJCeZ__ef/HomeTimeline?variables=%7B%7D&features=%7B%7D"
        )
        assert (qid, op) == ("KmJCeZ__ef", "HomeTimeline")

    def test_non_graphql(self):
        qid, op = cap.x_op_from_url("https://x.com/home")
        assert qid == ""


class TestSanitizeXHeaders:
    def test_secrets_become_placeholders(self):
        out = cap.sanitize_x_headers(_x_rec()["h"])
        assert out["authorization"] == "Bearer <bearer>"
        assert out["x-csrf-token"] == "<ct0>"
        assert out["x-client-transaction-id"] == "<fresh-txid>"

    def test_cookie_dropped(self):
        assert "cookie" not in cap.sanitize_x_headers(_x_rec()["h"])

    def test_static_headers_kept(self):
        out = cap.sanitize_x_headers(_x_rec()["h"])
        assert out["x-twitter-auth-type"] == "OAuth2Session"
        assert out["x-twitter-active-user"] == "yes"
        assert out["content-type"] == "application/json"

    def test_unknown_token_header_redacted(self):
        out = cap.sanitize_x_headers({"x-some-new-token": "secretvalue"})
        assert out["x-some-new-token"] == "<redacted>"

    def test_no_real_secret_survives(self):
        flat = json.dumps(cap.sanitize_x_headers(_x_rec()["h"]))
        assert "REALSECRET" not in flat
        assert "144f9e9brealcsrf" not in flat
        assert "realtxid123" not in flat


class TestParseXBody:
    def test_post_json(self):
        body = cap.parse_x_body(_x_rec())
        assert body["variables"]["tweet_text"] == "hello skeleton"
        assert body["queryId"] == "AbCdEf123"

    def test_get_query_params(self):
        qs = urllib.parse.urlencode({
            "variables": json.dumps({"count": 20, "rest_id": "7"}),
            "features": json.dumps({"flag": True}),
        })
        rec = _x_rec(
            n="HomeTimeline", q="KmJCeZ__ef", m="GET", b="",
            u=f"https://x.com/i/api/graphql/KmJCeZ__ef/HomeTimeline?{qs}",
        )
        body = cap.parse_x_body(rec)
        assert body["variables"]["count"] == 20
        assert body["features"]["flag"] is True
        assert body["queryId"] == "KmJCeZ__ef"


class TestSkeletonX:
    def test_shape(self):
        sk = cap._skeleton_x(_x_rec())
        assert sk["platform"] == "x"
        assert sk["operation"] == "CreateTweet"
        assert sk["method"] == "POST"
        assert sk["query_id"] == "AbCdEf123"
        assert sk["path_template"] == "/i/api/graphql/{query_id}/CreateTweet"
        assert set(sk) >= {"headers_template", "body_template"}

    def test_ids_scrubbed(self):
        sk = cap._skeleton_x(_x_rec())
        v = sk["body_template"]["variables"]
        assert v["tweet_id"] == "0"
        assert v["rest_id"] == "0"
        assert v["in_reply_to_tweet_id"] == "0"
        assert v["tweet_text"] == "hello skeleton"
        assert v["composer_session_id"] == cap._ZERO_UUID
        assert v["auth_token"] == "<redacted>"
        assert sk["body_template"]["features"]["communities_web_enable_tweet_community_results_fetch"] is True

    def test_no_real_values_on_disk(self):
        flat = json.dumps(cap._skeleton_x(_x_rec()))
        assert "REALSECRET" not in flat
        assert "144f9e9brealcsrf" not in flat
        assert "1900123456789012345" not in flat


class TestOutNameX:
    def test_snake_case(self):
        assert cap.default_out_name_x("CreateTweet") == (
            "agent_reach/channels/_x_create_tweet_vars.json"
        )

    def test_acronym(self):
        assert cap.default_out_name_x("UserTweets") == (
            "agent_reach/channels/_x_user_tweets_vars.json"
        )


class TestShimSurface:
    """capture_fb_* wrappers import parse_body/sanitize from the shim."""

    def test_reexports(self):
        for name in ("parse_body", "sanitize", "default_out_name", "oc", "oc_eval",
                     "_ZERO_UUID", "_UUID_RE", "_EPOCH_RE", "_ID_KEYS", "main"):
            assert hasattr(shim, name), name

    def test_sanitize_matches(self):
        # importlib loads each file as a separate module object — assert the
        # shim's re-exports behave identically rather than `is` identity.
        sample = {"a": {"actor_id": "9", "uuid": "f6023f7d-aaaa-bbbb-cccc-1234567890ab"}}
        assert shim.sanitize(sample) == cap.sanitize(sample)
        assert shim._ZERO_UUID == cap._ZERO_UUID

    def test_fb_defaults_unchanged(self):
        # FB call path (no id_keys) must not include X-only keys.
        out = cap.sanitize({"input": {"rest_id": "999", "actor_id": "123"}})
        assert out["input"]["rest_id"] == "999"  # not an FB id key
        assert out["input"]["actor_id"] == "0"
