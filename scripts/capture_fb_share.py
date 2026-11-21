#!/usr/bin/env python3
"""Capture the real GraphQL share-to-timeline mutation in the dedicated
Agent Reach browser (CloakBrowser, CDP 127.0.0.1:9222).

Usage:
    uv run python scripts/capture_fb_share.py <post-url> [--text T]
        [--audience only_me|friends|public] [--keep] [--out FILE]
    uv run python scripts/capture_fb_share.py arm      # arm interceptor only
    uv run python scripts/capture_fb_share.py dump     # dump captured bodies

The script creates its OWN facebook.com tab (never reuses existing tabs —
other agent sessions may be driving them), arms a fetch/XHR interceptor that
persists captures to localStorage (a successful share navigates to the new
permalink and would destroy a window-only buffer), then drives the real
Share UI with CDP Input.dispatchMouseEvent:

    share button (aria "Chia sẻ bài viết"/"Share …post")
      → share dialog ("Bảng feed" / "Chia sẻ ngay" / "Share now")
        → [optional] audience picker ("Chỉnh sửa đối tượng" → row → "Xong"/Done)
        → [optional] caption via Input.insertText on the contenteditable
        → click "Chia sẻ ngay"/"Share now"

On success the captured `ComposerStoryCreateMutation` body is parsed +
sanitized (actor/uid → "0", uuids → zero-uuid, epochs → 0) and written to
agent_reach/channels/_fb_share_vars.json as {params, variables}. `doc_id` is
printed separately — paste it into _facebook_share._DOC_ID if it rotated.

Facebook's markup drifts: if the probe reports no share button / no dialog,
adjust _OPEN_SHARE_JS / _DIALOG_JS selectors against a live page first.
"""
import json
import sys
import time
import urllib.parse
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))

from agent_reach.runtime import cdp  # noqa: E402
from agent_reach.channels._facebook_write import _eval, _wait_ready  # noqa: E402
from capture_fb_mutation import parse_body, sanitize  # noqa: E402

VARS_PATH = REPO_ROOT / "agent_reach" / "channels" / "_fb_share_vars.json"

_AUDIENCE_LABELS = {
    "only_me": r"chỉ mình tôi|only me",
    "friends": r"bạn bè|friends",
    "public": r"công khai|public",
}

# Interceptor: persist every /api/graphql POST to localStorage so the
# capture survives the post-share navigation. Guarded by window.__fbArmed so
# re-arming on a new document is idempotent.
ARM_JS = r"""
(()=>{try{
if(window.__fbArmed)return{already:true};
window.__fbArmed=true;
const load=()=>{try{return JSON.parse(localStorage.getItem("__fbCap")||"[]")}catch(e){return[]}};
const save=(a)=>{try{localStorage.setItem("__fbCap",JSON.stringify(a.slice(-60)))}catch(e){}};
const rec=(n,b)=>{try{const a=load();a.push({n:String(n||""),b:String(b||"").slice(0,200000)});save(a)}catch(e){}};
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

_DUMP_JS = r"""
(()=>{try{return localStorage.getItem("__fbCap")||"[]"}catch(e){return"[]"}})()
"""

# Find the share button that is actually clickable (elementFromPoint must
# hit it — lookalike buttons hide behind dialog overlays).
_OPEN_SHARE_JS = r"""
(()=>{try{
const out={cands:[],pick:null};
const norm=s=>String(s||'');
for(const el of document.querySelectorAll('[aria-label]')){
  const a=norm(el.getAttribute('aria-label'));
  if(!/chia sẻ bài viết|share (this )?post|share$/i.test(a))continue;
  const r=el.getBoundingClientRect();
  if(r.width<20||r.width>200||r.height<15||r.height>80)continue;
  const cx=r.left+r.width/2, cy=r.top+r.height/2;
  const hit=document.elementFromPoint(cx,cy);
  const ok=!!(hit&&(hit===el||el.contains(hit)||hit.contains(el)));
  out.cands.push({al:a.slice(0,60),x:Math.round(cx),y:Math.round(cy),hit:ok});
}
const pick=out.cands.find(c=>c.hit)||out.cands[0];
if(pick)out.pick=pick;
return JSON.stringify(out);
}catch(e){return JSON.stringify({error:String(e)})}})()
"""

# Snapshot of open dialogs so we can verify the share dialog opened (it
# carries the "Bảng feed" destination + "Chia sẻ ngay"/"Share now" submit).
_DIALOG_JS = r"""
(()=>{try{
const ds=[...document.querySelectorAll('[role="dialog"]')].map(d=>{
  const t=(d.innerText||'').slice(0,400);
  const r=d.getBoundingClientRect();
  return {w:Math.round(r.width),share:/Bảng feed|Chia sẻ ngay|Share now|Say something|Hãy nói gì/.test(t),text:t.slice(0,120)};
});
return JSON.stringify(ds);
}catch(e){return JSON.stringify({error:String(e)})}})()
"""

# Inside the share dialog: click the audience edit button, or click the
# "Chia sẻ ngay"/"Share now" submit. Real mouse events land via
# Input.dispatchMouseEvent on the returned coordinates — elementFromPoint
# is verified again here so the caller never clicks an occluded element.
_FIND_IN_DIALOG_JS = r"""
(()=>{try{
const want=__FB_WANT;
const dlg=[...document.querySelectorAll('[role="dialog"]')].find(d=>
  /Bảng feed|Chia sẻ ngay|Share now|Say something|Hãy nói gì/.test(d.innerText||''));
if(!dlg)return JSON.stringify({error:'no_share_dialog'});
const cands=[];
if(want==='audience'){
  for(const el of dlg.querySelectorAll('[aria-label]')){
    const a=el.getAttribute('aria-label')||'';
    if(/Chỉnh sửa quyền riêng tư|Chỉnh sửa đối tượng|Edit privacy|Edit audience/i.test(a))cands.push(el);
  }
}else if(want==='submit'){
  // The submit is a plain div (no role/aria) whose innerText is exactly the
  // label — scan every element and keep exact-text matches only.
  for(const el of dlg.querySelectorAll('div,span,button,[role="button"],[aria-label]')){
    const t=(el.innerText||'').trim();
    const a=(el.getAttribute('aria-label')||'').trim();
    if(/^(Chia sẻ ngay|Share now|Đăng ngay|Post)$/i.test(t)||/^(Chia sẻ ngay|Share now|Đăng ngay|Post)$/i.test(a))cands.push(el);
  }
}
// Prefer the innermost candidate (last in document order) — outer wrappers
// carry the same label text.
cands.reverse();
for(const el of cands){
  const r=el.getBoundingClientRect();
  if(r.width<10||r.height<10)continue;
  const cx=r.left+r.width/2, cy=r.top+r.height/2;
  const hit=document.elementFromPoint(cx,cy);
  if(hit&&(hit===el||el.contains(hit)||hit.contains(el)))
    return JSON.stringify({x:cx,y:cy,label:(el.getAttribute('aria-label')||el.innerText||'').slice(0,60)});
}
return JSON.stringify({error:'no_'+want+'_button',tried:cands.length});
}catch(e){return JSON.stringify({error:String(e)})}})()
"""

# Audience picker dialog (separate ~548w dialog): click the wanted row, then
# "Xong"/"Done". Rows are label divs — real mouse events required (.click()
# on the row did NOT register in live runs).
_AUDIENCE_JS = r"""
(()=>{try{
const wantRe=new RegExp(__FB_LABEL,'i');
const dlg=[...document.querySelectorAll('[role="dialog"]')].find(d=>
  /Đối tượng của bài viết|Ai có thể xem|Who can see|Select audience/i.test(d.innerText||''));
if(!dlg)return JSON.stringify({error:'no_audience_dialog'});
const rows=[...dlg.querySelectorAll('[role="radio"],[aria-checked],li,div[tabindex]')];
for(const el of rows){
  const t=(el.innerText||'')+' '+(el.getAttribute('aria-label')||'');
  if(!wantRe.test(t))continue;
  const r=el.getBoundingClientRect();
  const cx=r.left+r.width/2, cy=r.top+r.height/2;
  const hit=document.elementFromPoint(cx,cy);
  if(hit&&(hit===el||el.contains(hit)||hit.contains(el)))
    return JSON.stringify({row:{x:cx,y:cy,t:t.slice(0,50)}});
}
return JSON.stringify({error:'no_audience_row',rows:rows.length});
}catch(e){return JSON.stringify({error:String(e)})}})()
"""

_AUDIENCE_DONE_JS = r"""
(()=>{try{
const dlg=[...document.querySelectorAll('[role="dialog"]')].find(d=>
  /Đối tượng của bài viết|Ai có thể xem|Who can see|Select audience/i.test(d.innerText||''));
if(!dlg)return JSON.stringify({error:'no_audience_dialog'});
for(const el of dlg.querySelectorAll('[role="button"],button,[aria-label]')){
  const t=((el.innerText||'')+' '+(el.getAttribute('aria-label')||'')).trim();
  if(!/^(Xong|Done|Save|Lưu)$/i.test(t))continue;
  const r=el.getBoundingClientRect();
  const cx=r.left+r.width/2, cy=r.top+r.height/2;
  const hit=document.elementFromPoint(cx,cy);
  if(hit&&(hit===el||el.contains(hit)||hit.contains(el)))
    return JSON.stringify({x:cx,y:cy});
}
return JSON.stringify({error:'no_done_button'});
}catch(e){return JSON.stringify({error:String(e)})}})()
"""

_FOCUS_CAPTION_JS = r"""
(()=>{try{
const dlg=[...document.querySelectorAll('[role="dialog"]')].find(d=>
  /Bảng feed|Chia sẻ ngay|Share now|Say something|Hãy nói gì/.test(d.innerText||''));
if(!dlg)return JSON.stringify({error:'no_share_dialog'});
const ed=dlg.querySelector('[contenteditable="true"],[role="textbox"]');
if(!ed)return JSON.stringify({error:'no_editor'});
ed.focus();
const r=ed.getBoundingClientRect();
return JSON.stringify({x:r.left+r.width/2,y:r.top+8});
}catch(e){return JSON.stringify({error:String(e)})}})()
"""


def own_tab():
    """Create a dedicated facebook.com target; returns its target id."""
    res = cdp.call("Target.createTarget", {"url": "https://www.facebook.com/"})
    tid = res.get("targetId") or res.get("id")
    if not tid:
        raise RuntimeError(f"Target.createTarget failed: {res!r}")
    return tid


def close_tab(tid):
    try:
        cdp.call("Target.closeTarget", {"targetId": tid})
    except Exception:
        pass


def click_xy(tid, x, y):
    for t in ("mousePressed", "mouseReleased"):
        cdp.call(
            "Input.dispatchMouseEvent",
            {"type": t, "x": x, "y": y, "button": "left", "clickCount": 1},
            target_id=tid,
        )
        time.sleep(0.05)


def ensure_on_post(tid, url):
    try:
        href = _eval(tid, "location.href", 8) or ""
    except Exception:
        href = ""
    if urllib.parse.urlparse(href).path.rstrip("/") == urllib.parse.urlparse(url).path.rstrip("/"):
        return
    cdp.call("Page.navigate", {"url": url}, target_id=tid)
    for _ in range(30):
        time.sleep(1)
        try:
            href = _eval(tid, "location.href", 8) or ""
        except Exception:
            href = ""
        if urllib.parse.urlparse(href).path.rstrip("/") == urllib.parse.urlparse(url).path.rstrip("/"):
            _wait_ready(tid, 15)
            return
    raise RuntimeError("page did not reach " + url)


def arm(tid):
    print("arm:", _eval(tid, ARM_JS, 10))


def dump(tid):
    raw = _eval(tid, _DUMP_JS, 10)
    try:
        return json.loads(raw) if isinstance(raw, str) else []
    except Exception:
        return []


def drive_share(tid, *, text="", audience=None):
    """Drive the real share dialog; returns True once submit is clicked."""
    probe = _eval(tid, _OPEN_SHARE_JS, 15)
    print("share buttons:", probe)
    try:
        pick = (json.loads(probe) if isinstance(probe, str) else probe or {}).get("pick")
    except Exception:
        pick = None
    if not pick:
        print("[!] no share button found — UI changed?")
        return False
    click_xy(tid, pick["x"], pick["y"])
    time.sleep(1.5)
    dialogs = _eval(tid, _DIALOG_JS, 10)
    print("dialogs:", dialogs)
    try:
        ds = json.loads(dialogs) if isinstance(dialogs, str) else dialogs or []
    except Exception:
        ds = []
    if not any(d.get("share") for d in ds):
        print("[!] share dialog did not open")
        return False

    if audience:
        want = _FIND_IN_DIALOG_JS.replace("__FB_WANT", json.dumps("audience"))
        btn = _eval(tid, want, 10)
        print("audience btn:", btn)
        try:
            b = json.loads(btn) if isinstance(btn, str) else btn
        except Exception:
            b = None
        if b and b.get("x"):
            click_xy(tid, b["x"], b["y"])
            time.sleep(1.2)
            row_js = _AUDIENCE_JS.replace("__FB_LABEL", json.dumps(_AUDIENCE_LABELS[audience]))
            row = _eval(tid, row_js, 10)
            print("audience row:", row)
            try:
                r = json.loads(row) if isinstance(row, str) else row
            except Exception:
                r = None
            if r and r.get("row"):
                click_xy(tid, r["row"]["x"], r["row"]["y"])
                time.sleep(0.8)
                done = _eval(tid, _AUDIENCE_DONE_JS, 10)
                print("done btn:", done)
                try:
                    d = json.loads(done) if isinstance(done, str) else done
                except Exception:
                    d = None
                if d and d.get("x"):
                    click_xy(tid, d["x"], d["y"])
                    time.sleep(1.0)
        else:
            print("[!] audience button missing — continuing with default audience")

    if text:
        foc = _eval(tid, _FOCUS_CAPTION_JS, 10)
        print("caption focus:", foc)
        try:
            f = json.loads(foc) if isinstance(foc, str) else foc
        except Exception:
            f = None
        if f and f.get("x"):
            click_xy(tid, f["x"], f["y"])
            cdp.call("Input.insertText", {"text": text}, target_id=tid)
            time.sleep(0.6)

    want = _FIND_IN_DIALOG_JS.replace("__FB_WANT", json.dumps("submit"))
    btn = _eval(tid, want, 10)
    print("submit btn:", btn)
    try:
        b = json.loads(btn) if isinstance(btn, str) else btn
    except Exception:
        b = None
    if not (b and b.get("x")):
        print("[!] submit button missing")
        return False
    click_xy(tid, b["x"], b["y"])
    return True


def find_share_mutation(cap):
    """Pick the ComposerStoryCreateMutation body whose composer_type is share."""
    for c in reversed(cap):
        if "ComposerStoryCreateMutation" not in (c.get("n") or ""):
            continue
        try:
            body = c.get("b") or ""
            vars_json = urllib.parse.parse_qs(body).get("variables", [""])[0]
            if '"share"' in vars_json and "composer_type" in vars_json:
                return body
        except Exception:
            continue
    return None


def write_vars(body, out_path):
    parsed = parse_body(body)
    skeleton = {
        "params": sanitize(parsed["params"]),
        "variables": sanitize(parsed["variables"]),
    }
    # Manual fix-ups the sanitizer cannot know: the session-bound encrypted
    # tracking blob is dropped (post skeleton precedent), message reset.
    inp = (skeleton.get("variables") or {}).get("input") or {}
    if "tracking" in inp:
        inp["tracking"] = [None]
    if "message" in inp:
        inp["message"] = {"ranges": [], "text": ""}
    path = Path(out_path) if out_path else VARS_PATH
    path.write_text(json.dumps(skeleton, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"    -> {path}")
    print("    doc_id:", parsed.get("doc_id"), "(update _facebook_share._DOC_ID if changed)")
    return True


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return
    arg0 = sys.argv[1]
    if arg0 in ("arm", "dump"):
        # attach to the caller's own new tab as well, for consistency
        tid = own_tab()
        try:
            _wait_ready(tid, 20)
            if arg0 == "arm":
                arm(tid)
            else:
                print(json.dumps(dump(tid), indent=1, ensure_ascii=False)[:4000])
        finally:
            close_tab(tid)
        return

    url = arg0
    text = ""
    audience = None
    keep = "--keep" in sys.argv
    out = None
    if "--text" in sys.argv:
        i = sys.argv.index("--text")
        text = sys.argv[i + 1] if i + 1 < len(sys.argv) else ""
    if "--audience" in sys.argv:
        i = sys.argv.index("--audience")
        audience = sys.argv[i + 1] if i + 1 < len(sys.argv) else None
        if audience not in _AUDIENCE_LABELS:
            print("[!] --audience must be one of: only_me, friends, public")
            return
    if "--out" in sys.argv:
        i = sys.argv.index("--out")
        out = sys.argv[i + 1] if i + 1 < len(sys.argv) else None
    if not url.startswith("http"):
        print("[!] first arg must be a public facebook post URL")
        return

    tid = own_tab()
    try:
        _wait_ready(tid, 20)
        ensure_on_post(tid, url)
        arm(tid)
        if drive_share(tid, text=text, audience=audience):
            print("waiting for mutation capture...")
            for _ in range(25):
                time.sleep(1)
                try:
                    cap = dump(tid)
                except Exception:
                    cap = []
                names = [c.get("n") for c in cap]
                body = find_share_mutation(cap)
                if body:
                    print("CAPTURED:", names)
                    write_vars(body, out)
                    return
            print("[!] no share ComposerStoryCreateMutation seen; names:", names)
        else:
            print("[!] share UI drive failed")
    finally:
        if not keep:
            close_tab(tid)


if __name__ == "__main__":
    main()
