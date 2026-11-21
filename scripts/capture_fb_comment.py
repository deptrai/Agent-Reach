#!/usr/bin/env python3
"""Capture the real GraphQL mutations for comment create + delete in the
dedicated Agent Reach browser (CloakBrowser, CDP 127.0.0.1:9222).

Usage:
    uv run python scripts/capture_fb_comment.py create [text]
    uv run python scripts/capture_fb_comment.py delete <comment-permalink-or-id> [--out FILE]
    uv run python scripts/capture_fb_comment.py arm            # arm interceptor only
    uv run python scripts/capture_fb_comment.py dump           # dump window.__fbCap

`delete` drives the real UI: it finds the target comment's ⋯ menu on the
permalink page, clicks Delete, confirms the dialog if one appears, then dumps
and sanitizes the captured `useCometUFIDeleteCommentMutation` body. The DOM
walk is best-effort — Facebook's markup drifts, so if the probe reports
`menuCount: 0` or `deleteOption: false`, adjust the selectors in
`_DELETE_UI_JS` against a live page before trusting a fresh capture. The
sanitized skeleton is merged into `_fb_comment_vars.json` under "delete"
(or written standalone with --out).
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
VARS_PATH = Path(__file__).with_name("..") / "agent_reach" / "channels" / "_fb_comment_vars.json"

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


def type_comment(tid, text):
    """Focus the composer and type via real keyboard events."""
    focus_js = r"""
(()=>{try{
const eds=[...document.querySelectorAll('[contenteditable="true"],[role="textbox"]')];
const ed=eds.find(e=>/comment|bình luận/i.test((e.getAttribute('aria-label')||'')+(e.getAttribute('placeholder')||'')+(e.id||'')))||eds[eds.length-1];
if(!ed)return{error:'no_editor'};
ed.focus();ed.click();
return{ok:true,tag:ed.tagName,id:ed.id||''};
}catch(e){return{error:String(e)}}})()
"""
    print("focus:", _eval(tid, focus_js, 10))
    time.sleep(0.5)
    # Type through Input.insertText so React sees real input events.
    cdp.call("Input.insertText", {"text": text}, target_id=tid)
    time.sleep(0.5)
    # Submit with Enter.
    for t in ("keyDown", "keyUp"):
        cdp.call(
            "Input.dispatchKeyEvent",
            {"type": t, "key": "Enter", "code": "Enter", "windowsVirtualKeyCode": 13},
            target_id=tid,
        )


def create_comment(tid, text):
    type_comment(tid, text)
    print("waiting for mutation capture...")
    for i in range(20):
        time.sleep(1)
        cap = dump(tid)
        names = [c.get("n") for c in cap]
        if any("Comment" in (n or "") for n in names):
            print("CAPTURED:", names)
            return cap
    print("no Comment mutation seen; names:", [c.get("n") for c in cap])
    return cap


_DELETE_UI_JS = r"""
(()=>{try{
const cid=__FB_CID;
const norm=s=>String(s||'').toLowerCase();
// 1) locate the comment container: an anchor carrying comment_id=<cid>, then
//    climb to a block that also holds a ⋯/more-options control.
const anchors=[...document.querySelectorAll('a[href*="comment_id="]')]
  .filter(a=>{const h=a.getAttribute('href')||'';
    return h.includes('comment_id='+cid)||h.endsWith('comment_id='+cid);});
let box=null;
for(const a of anchors){
  let n=a;
  for(let i=0;i<12&&n;i++){
    n=n.parentElement;
    if(!n)break;
    if(n.querySelector('[aria-label] button,[role="button"][aria-label]')){box=n;break;}
  }
  if(box)break;
}
const scope=box||document;
// 2) the ⋯ trigger: aria-label matching more/options (EN + VI), small button.
const trig=[...scope.querySelectorAll('[aria-label]')].find(el=>{
  const a=norm(el.getAttribute('aria-label'));
  const r=el.getBoundingClientRect();
  return /more|option|thêm|tùy|tuỳ/.test(a)&&r.width<120&&r.height<120;
});
if(!trig)return{error:'no_menu_trigger',anchors:anchors.length};
trig.scrollIntoView({block:'center'});
trig.click();
return{clicked:true,anchors:anchors.length};
}catch(e){return{error:String(e)}}})()
"""

_CONFIRM_JS = r"""
(async()=>{try{
const norm=s=>String(s||'').toLowerCase();
// Menu is open: click the Delete/Xoá item (exact label preferred).
const items=[...document.querySelectorAll('[role="menuitem"],[role="button"],li,div[tabindex]')];
const labels=items.map(el=>norm(el.innerText||el.getAttribute('aria-label')||''));
const pick=items.find(el=>/^delete( comment)?$/.test(norm(el.innerText||el.getAttribute('aria-label')||'')))
  ||items.find(el=>/^(xoá|xóa)( bình luận| comment)?$/.test(norm(el.innerText||el.getAttribute('aria-label')||'')))
  ||items.find(el=>/\b(delete|xoá|xóa)\b/.test(norm(el.innerText||'')));
if(!pick)return{error:'no_delete_option',labels:labels.slice(0,20)};
pick.click();
// FB may show a confirm dialog — accept the destructive confirm button.
for(let i=0;i<20;i++){
  await new Promise(r=>setTimeout(r,250));
  const btns=[...document.querySelectorAll('[role="button"],button')];
  const conf=btns.find(b=>/^(delete|xoá|xóa)$/.test(norm(b.innerText||b.getAttribute('aria-label')||'')));
  if(conf){conf.click();return{confirmed:true};}
}
return{clicked:true,confirmed:false};
}catch(e){return{error:String(e)}}})()
"""


def delete_comment(tid, cid):
    """Drive the UI delete for comment_id `cid`; returns probe dict."""
    js = _DELETE_UI_JS.replace("__FB_CID", json.dumps(cid))
    probe = _eval(tid, js, 15)
    print("menu probe:", probe)
    if not isinstance(probe, dict) or probe.get("error"):
        return probe
    time.sleep(1.0)  # let the menu render
    res = _eval(tid, _CONFIRM_JS, 20)
    print("confirm:", res)
    return res


def write_delete_vars(tid, out_path):
    """Find the captured delete mutation, sanitize, merge into vars file."""
    cap = dump(tid)
    hits = [c for c in cap if "DeleteComment" in (c.get("n") or "") and c.get("b")]
    if not hits:
        print("[!] no DeleteComment mutation captured; names:", [c.get("n") for c in cap])
        return False
    parsed = parse_body(hits[-1]["b"])
    skeleton = {
        "params": sanitize(parsed["params"]),
        "variables": sanitize(parsed["variables"]),
    }
    skeleton["doc_id"] = parsed["doc_id"]
    if out_path:
        Path(out_path).write_text(json.dumps(skeleton, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        print(f"    -> {out_path}")
        return True
    # Merge under "delete" in the shared vars file, keeping params/create.
    doc = {}
    if VARS_PATH.exists():
        try:
            doc = json.loads(VARS_PATH.read_text(encoding="utf-8"))
        except Exception:
            doc = {}
    doc.setdefault("params", skeleton["params"])
    doc["delete"] = {"variables": skeleton["variables"], "doc_id": skeleton["doc_id"]}
    VARS_PATH.write_text(json.dumps(doc, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"    -> {VARS_PATH} (merged under 'delete')")
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

    if mode == "create":
        text = sys.argv[2] if len(sys.argv) > 2 else "test comment from capture script"
        ensure_on_post(tid)
        arm(tid)
        create_comment(tid, text)
        print("waiting for mutation capture...")
        for i in range(20):
            time.sleep(1)
            cap = dump(tid)
            names = [c.get("n") for c in cap]
            if any("Comment" in (n or "") for n in names):
                print("CAPTURED:", names)
                break
        else:
            print("no Comment mutation seen; names:", names)
    elif mode == "delete":
        args = [a for a in sys.argv[2:] if not a.startswith("--")]
        out = None
        if "--out" in sys.argv:
            i = sys.argv.index("--out")
            out = sys.argv[i + 1] if i + 1 < len(sys.argv) else None
        if not args:
            print("usage: capture_fb_comment.py delete <comment-permalink-or-id> [--out FILE]")
            return
        url = args[0]
        ensure_on_post(tid, url if url.startswith("http") else POST_URL)
        arm(tid)
        cid = (
            urllib.parse.parse_qs(urllib.parse.urlparse(url).query).get("comment_id", [None])[0]
            if url.startswith("http")
            else url
        )
        print("comment_id:", cid)
        if not cid:
            print("[!] could not extract comment_id from target")
            return
        delete_comment(tid, cid)
        # give the mutation a moment to fire, then capture+sanitize
        for _ in range(10):
            time.sleep(1)
            cap = dump(tid)
            if any("DeleteComment" in (c.get("n") or "") for c in cap):
                break
        write_delete_vars(tid, out)
    else:
        print(__doc__)


if __name__ == "__main__":
    main()
