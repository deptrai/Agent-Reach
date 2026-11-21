#!/usr/bin/env python3
"""Capture the real GraphQL mutations for post edit + delete in the dedicated
Agent Reach browser (CloakBrowser, CDP 127.0.0.1:9222).

Usage:
    uv run python scripts/capture_fb_post_edit.py edit [new-text]
    uv run python scripts/capture_fb_post_edit.py delete [--out FILE]
    uv run python scripts/capture_fb_post_edit.py arm            # arm interceptor only
    uv run python scripts/capture_fb_post_edit.py dump           # dump window.__fbCap

Both modes drive the real post UI on the test post permalink: the ⋯ (more
options) button on the main post opens a menu with "Edit post" / "Delete
post". The DOM walk is best-effort — Facebook's markup drifts, so if the probe
reports `menuCount: 0` or `itemFound: false`, adjust the selectors in
`_MENU_JS` / `_PICK_JS` against a live page before trusting a fresh capture.

Sanitized skeletons (params + variables + doc_id, actor/uid -> "0", uuids ->
zero-uuid, epochs -> 0) are merged into
`agent_reach/channels/_fb_post_vars.json` under "edit" / "delete" (or written
standalone with --out).
"""
import json
import sys
import time
import urllib.parse
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from agent_reach.runtime import cdp  # noqa: E402
from agent_reach.channels._facebook_write import _eval, _fb_target, _wait_ready  # noqa: E402
from capture_fb_mutation import parse_body, sanitize  # noqa: E402

POST_URL = "https://www.facebook.com/61554949615037/posts/122311668092164987"
VARS_PATH = Path(__file__).with_name("..") / "agent_reach" / "channels" / "_fb_post_vars.json"

# Transport keys safe to persist — everything else in params is a live
# session token (fb_dtsg/lsd/jazoest/av/__user/...) that the send JS
# overwrites at runtime anyway, so it must NEVER be written to disk.
_TRANSPORT_PARAMS = {
    "__aaid", "__ccg", "__crn", "__csr", "__dyn", "__hblp", "__hsdp",
    "__jssesw", "__req", "__s", "__sjsp", "dpr",
}

ARM_JS = r"""
(()=>{try{
if(window.__fbCap)return{already:true};
window.__fbCap=[];
const rec=(n,b)=>{try{window.__fbCap.push({n:String(n||""),b:String(b||"")})}catch(e){}};
const keep=(u)=>String(u||"").indexOf("/api/graphql")>=0;
const X=window.XMLHttpRequest;
const origOpen=X.prototype.open, origSend=X.prototype.send;
X.prototype.open=function(m,u,...rest){this.__fbU=u;this.__fbM=m;return origOpen.call(this,m,u,...rest)};
X.prototype.send=function(b){
try{
if(this.__fbM&&this.__fbM.toUpperCase()==="POST"&&keep(this.__fbU)){
let n="";try{const f=new URLSearchParams(String(b||""));n=f.get("fb_api_req_friendly_name")||"";}catch(e){}
rec(n,b);
}
}catch(e){}
return origSend.apply(this,arguments)};
const F=window.fetch;
window.fetch=function(u,o){
try{
const url=typeof u==="string"?u:(u&&u.url)||"";
if(keep(url)&&o&&String(o.method||"GET").toUpperCase()==="POST"){
let n="";try{const f=new URLSearchParams(String(o.body||""));n=f.get("fb_api_req_friendly_name")||"";}catch(e){}
rec(n,o.body);
}
}catch(e){}
return F.apply(this,arguments)};
return{armed:true};
}catch(e){return{error:String(e)}}})()
"""


def ensure_on_post(tid, url=POST_URL):
    """Navigate the tab to the post if it isn't already there."""
    try:
        href = _eval(tid, "location.href", 8) or ""
    except Exception:
        href = ""
    want = urllib.parse.urlparse(url).path.rstrip("/")
    have = urllib.parse.urlparse(href).path.rstrip("/")
    if have == want:
        return
    cdp.call("Page.navigate", {"url": url}, target_id=tid)
    for _ in range(30):
        time.sleep(1)
        try:
            href = _eval(tid, "location.href", 8) or ""
        except Exception:
            href = ""
        if urllib.parse.urlparse(href).path.rstrip("/") == want:
            _wait_ready(tid, 15)
            return
    raise RuntimeError("page did not reach " + url)


def arm(tid):
    print("arm:", _eval(tid, ARM_JS, 10))


def dump(tid):
    raw = _eval(tid, "JSON.stringify(window.__fbCap||[])", 10)
    try:
        return json.loads(raw) if isinstance(raw, str) else []
    except Exception:
        return []


# The ⋯ trigger on the MAIN post (not on feed items below it): scope to the
# first article/[role=article] on the permalink page, then pick the small
# aria-labelled more/options button.
_MENU_JS = r"""
(()=>{try{
const norm=s=>String(s||'').toLowerCase();
const scope=document.querySelector('[role="article"]')||document;
const trig=[...scope.querySelectorAll('[aria-label]')].find(el=>{
  const a=norm(el.getAttribute('aria-label'));
  const r=el.getBoundingClientRect();
  return /more|option|thêm|tùy|tuỳ/.test(a)&&r.width<140&&r.height<140;
});
if(!trig)return{error:'no_menu_trigger'};
trig.scrollIntoView({block:'center'});
trig.click();
return{clicked:true};
}catch(e){return{error:String(e)}}})()
"""

# After the menu is open, click the Edit post / Delete post item.
_PICK_JS = r"""
(async()=>{try{
const norm=s=>String(s||'').toLowerCase();
const want=__FB_WANT; // "edit" | "delete"
const re=want==="edit"
  ?/(edit post|sửa bài|chỉnh sửa bài|edit)/
  :/(delete post|xoá bài|xóa bài|delete)/;
const items=[...document.querySelectorAll('[role="menuitem"],[role="button"],li,div[tabindex]')];
const pick=items.find(el=>re.test(norm(el.innerText||el.getAttribute('aria-label')||'')));
if(!pick)return{error:'item_not_found',want:want};
pick.click();
return{picked:want};
}catch(e){return{error:String(e)}}})()
"""

_EDIT_SAVE_JS = r"""
(async()=>{try{
const norm=s=>String(s||'').toLowerCase();
// Replace the composer text: focus, select-all, insert new text.
const eds=[...document.querySelectorAll('[contenteditable="true"],[role="textbox"]')];
const ed=eds.find(e=>/post|bài|viết|composer/i.test((e.getAttribute('aria-label')||'')+(e.getAttribute('placeholder')||'')))||eds[eds.length-1];
if(!ed)return{error:'no_editor'};
ed.focus();
const sel=window.getSelection();sel.selectAllChildren(ed);
return{ok:true};
}catch(e){return{error:String(e)}}})()
"""

_SAVE_CLICK_JS = r"""
(()=>{try{
const norm=s=>String(s||'').toLowerCase();
const btns=[...document.querySelectorAll('[role="button"],button')];
const save=btns.find(b=>/^(save|lưu|update|cập nhật)$/.test(norm(b.innerText||b.getAttribute('aria-label')||'')));
if(!save)return{error:'no_save_button'};
save.click();
return{clicked:true};
}catch(e){return{error:String(e)}}})()
"""

_CONFIRM_DELETE_JS = r"""
(async()=>{try{
const norm=s=>String(s||'').toLowerCase();
for(let i=0;i<24;i++){
  await new Promise(r=>setTimeout(r,250));
  const btns=[...document.querySelectorAll('[role="button"],button')];
  const conf=btns.find(b=>/^(delete|xoá|xóa)$/.test(norm(b.innerText||b.getAttribute('aria-label')||'')));
  if(conf){conf.click();return{confirmed:true};}
}
return{confirmed:false};
}catch(e){return{error:String(e)}}})()
"""


def _pick(tid, want):
    print("menu:", _eval(tid, _MENU_JS, 15))
    time.sleep(1.0)  # let the menu render
    res = _eval(tid, _PICK_JS.replace("__FB_WANT", json.dumps(want)), 15)
    print("pick:", res)
    return res


def edit_post(tid, text):
    if not _pick(tid, "edit").get("picked"):
        return
    time.sleep(1.5)  # composer dialog opens
    print("select-all:", _eval(tid, _EDIT_SAVE_JS, 15))
    cdp.call("Input.insertText", {"text": text}, target_id=tid)
    time.sleep(0.5)
    print("save:", _eval(tid, _SAVE_CLICK_JS, 15))


def delete_post(tid):
    if not _pick(tid, "delete").get("picked"):
        return
    print("confirm:", _eval(tid, _CONFIRM_DELETE_JS, 20))


def wait_capture(tid, needle, tries=20):
    for _ in range(tries):
        time.sleep(1)
        cap = dump(tid)
        names = [c.get("n") for c in cap]
        if any(needle in (n or "") for n in names):
            print("CAPTURED:", names)
            return cap
    print("no mutation seen; names:", [c.get("n") for c in dump(tid)])
    return []


def write_vars(tid, kind, needle, out_path=None):
    """Find the captured mutation, sanitize, merge into the vars file."""
    cap = dump(tid)
    hits = [c for c in cap if needle in (c.get("n") or "") and c.get("b")]
    if not hits:
        print(f"[!] no {needle} mutation captured; names:", [c.get("n") for c in cap])
        return False
    parsed = parse_body(hits[-1]["b"])
    # Transport keys only — every other params key is a live session token
    # (fb_dtsg/lsd/jazoest/av/__user/...) that the send JS overwrites at
    # runtime anyway, so it must NEVER be persisted on re-capture.
    skeleton = {
        "friendly": parsed["friendly_name"],
        "doc_id": parsed["doc_id"],
        "params": {k: v for k, v in sanitize(parsed["params"]).items()
                   if k in _TRANSPORT_PARAMS},
        "variables": sanitize(parsed["variables"]),
    }
    if out_path:
        Path(out_path).write_text(json.dumps(skeleton, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        print(f"    -> {out_path}")
        return True
    doc = {}
    if VARS_PATH.exists():
        try:
            doc = json.loads(VARS_PATH.read_text(encoding="utf-8"))
        except Exception:
            doc = {}
    doc[kind] = skeleton
    VARS_PATH.write_text(json.dumps(doc, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"    -> {VARS_PATH} (merged under '{kind}')")
    return True


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return
    mode = sys.argv[1]
    tid = (_fb_target(create=True) or {}).get("id")
    if not tid:
        print("[!] no facebook.com tab in the dedicated browser")
        return
    _wait_ready(tid, 20)

    if mode == "edit":
        text = sys.argv[2] if len(sys.argv) > 2 else "edited by capture script"
        ensure_on_post(tid)
        arm(tid)
        edit_post(tid, text)
        wait_capture(tid, "StoryEdit")
        write_vars(tid, "edit", "StoryEdit")
    elif mode == "delete":
        out = None
        if "--out" in sys.argv:
            i = sys.argv.index("--out")
            out = sys.argv[i + 1] if i + 1 < len(sys.argv) else None
        ensure_on_post(tid)
        arm(tid)
        delete_post(tid)
        wait_capture(tid, "StoryDelete")
        write_vars(tid, "delete", "StoryDelete", out)
    else:
        print(__doc__)


if __name__ == "__main__":
    main()
