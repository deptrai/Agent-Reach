#!/usr/bin/env python3
"""Back-compat shim — the capture pipeline moved to `capture_mutation.py`.

`--platform fb` there is byte-identical to what this script used to do, and
the helpers (`parse_body`, `sanitize`, `default_out_name`, `oc`, `oc_eval`)
stay importable from here because `capture_fb_comment.py`,
`capture_fb_post_edit.py` and `capture_fb_share.py` do
`from capture_fb_mutation import parse_body, sanitize`.

Usage unchanged:
    uv run python scripts/capture_fb_mutation.py \
        --profile acrtv927 --name UFILikeMutation --seconds 90
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from capture_mutation import (  # noqa: E402,F401
    _EPOCH_RE,
    _ID_KEYS,
    _UUID_RE,
    _ZERO_UUID,
    default_out_name,
    main,
    oc,
    oc_eval,
    parse_body,
    sanitize,
)

if __name__ == "__main__":
    raise SystemExit(main())
