"""Unit tests for scripts/capture_fb_mutation.py — sanitize + body parsing."""

import importlib.util
import json
import urllib.parse
from pathlib import Path

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "capture_fb_mutation",
    Path(__file__).resolve().parent.parent / "scripts" / "capture_fb_mutation.py",
)
cap = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(cap)


class TestSanitize:
    def test_actor_and_user_ids_zeroed(self):
        vars_ = {
            "input": {
                "actor_id": "61590064244856",
                "user_id": "123",
                "message": {"text": "hello"},
            }
        }
        out = cap.sanitize(vars_)
        assert out["input"]["actor_id"] == "0"
        assert out["input"]["user_id"] == "0"
        assert out["input"]["message"]["text"] == "hello"

    def test_uuids_zeroed_but_suffix_kept(self):
        vars_ = {
            "input": {
                "idempotence_token": "f6023f7d-aaaa-bbbb-cccc-1234567890ab_FEED",
                "logging": {"composer_session_id": "f6023f7d-aaaa-bbbb-cccc-1234567890ab"},
            }
        }
        out = cap.sanitize(vars_)
        assert out["input"]["idempotence_token"] == cap._ZERO_UUID + "_FEED"
        assert out["input"]["logging"]["composer_session_id"] == cap._ZERO_UUID

    def test_uuid_embedded_in_string_replaced(self):
        v = "id,123,f6023f7d-aaaa-bbbb-cccc-1234567890ab,x"
        assert cap.sanitize(v) == "id,123," + cap._ZERO_UUID + ",x"

    def test_epoch_strings_and_ints_zeroed(self):
        vars_ = {"t1": "1795202351115", "t2": 1795202351115, "t3": "42", "t4": 42}
        out = cap.sanitize(vars_)
        assert out == {"t1": "0", "t2": 0, "t3": "42", "t4": 42}

    def test_attribution_template(self):
        vars_ = {
            "navigation_data": {
                "attribution_id_v2": "CometHomeRoot.react,comet.home,via_cold_start,1795202351115,1,2,,"
            }
        }
        out = cap.sanitize(vars_)
        assert out["navigation_data"]["attribution_id_v2"] == (
            "CometHomeRoot.react,comet.home,via_cold_start,0,0,0,,"
        )

    def test_relay_flags_untouched(self):
        vars_ = {
            "__relay_internal__pv__FooBarrelayprovider": True,
            "input": {"audience": {"privacy": {"base_state": "FRIENDS"}}},
        }
        out = cap.sanitize(vars_)
        assert out["__relay_internal__pv__FooBarrelayprovider"] is True
        assert out["input"]["audience"]["privacy"]["base_state"] == "FRIENDS"

    def test_real_skeleton_roundtrip(self):
        skeleton_path = (
            Path(__file__).resolve().parent.parent
            / "agent_reach" / "channels" / "_fb_post_create_vars.json"
        )
        if not skeleton_path.exists():
            pytest.skip("no skeleton on disk")
        vars_ = json.loads(skeleton_path.read_text())
        out = cap.sanitize(vars_)
        flat = json.dumps(out)
        assert "61590064244856" not in flat  # actor_id gone
        assert cap._UUID_RE.search(flat) is None or "00000000" in flat


class TestParseBody:
    def _body(self, **over):
        params = {
            "av": "61590064244856",
            "__user": "61590064244856",
            "fb_dtsg": "NAfx...:15:1787946078",
            "fb_api_req_friendly_name": "ComposerStoryCreateMutation",
            "doc_id": "29221801510819514",
            "variables": json.dumps({"input": {"message": {"text": "hi"}}}),
        }
        params.update(over)
        return urllib.parse.urlencode(params)

    def test_extracts_fields(self):
        out = cap.parse_body(self._body())
        assert out["friendly_name"] == "ComposerStoryCreateMutation"
        assert out["doc_id"] == "29221801510819514"
        assert out["variables"]["input"]["message"]["text"] == "hi"
        assert "variables" not in out["params"]
        assert out["params"]["__user"] == "61590064244856"

    def test_missing_variables_empty(self):
        out = cap.parse_body(self._body(variables=""))
        assert out["variables"] == {}


class TestDefaultOutName:
    def test_mutation_suffix_stripped(self):
        assert cap.default_out_name("UFILikeMutation") == (
            "agent_reach/channels/_fb_ufi_like_vars.json"
        )

    def test_plain_name(self):
        assert cap.default_out_name("ComposerStoryCreateMutation") == (
            "agent_reach/channels/_fb_composer_story_create_vars.json"
        )
