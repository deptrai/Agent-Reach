# ShardBrowser ↔ OpenCLI extension spike — verified 2026-11-21, macOS arm64.
# Run: pip install shardx websockets
# Proves: extension loads headless (MV3 service_worker in /json), connects to
# OpenCLI daemon :19825, writes opencli_context_id_v1 to chrome.storage.local
# (what agent_reach cdp.extension_value reads), fixed CDP port via last-wins flag.
import json, time, urllib.request, subprocess
import websockets, asyncio
from shardx import ShardX

EXT = "/Users/luisphan/.agent-reach/browser/extension"  # OpenCLI unpacked ext
PORT = "9223"

sdk = ShardX()                      # first call downloads engine + 220 fp lib
prof = sdk.create_profile("mac-m4-pro-mbp14")
sess = sdk.launch(prof, cdp=True, headless=True, extra_args=[
    f"--disable-extensions-except={EXT}",
    f"--load-extension={EXT}",
    f"--remote-debugging-port={PORT}",   # overrides SDK's port=0 (last wins)
])
# sess.cdp_url is None when port overridden — use http://127.0.0.1:PORT/json
time.sleep(3)

def targets():
    return json.loads(urllib.request.urlopen(f"http://127.0.0.1:{PORT}/json", timeout=2).read())

async def eval_on(ws_url, expr):
    async with websockets.connect(ws_url, max_size=None) as ws:
        await ws.send(json.dumps({"id": 1, "method": "Runtime.evaluate",
                                  "params": {"expression": expr, "awaitPromise": True, "returnByValue": True}}))
        while True:
            m = json.loads(await ws.recv())
            if m.get("id") == 1:
                return (m.get("result", {}).get("result") or {}).get("value")

ctx = None
for _ in range(30):
    for t in targets():
        if t.get("type") == "service_worker" and "chrome-extension://" in t.get("url", ""):
            ctx = asyncio.run(eval_on(t["webSocketDebuggerUrl"],
                "chrome.storage.local.get('opencli_context_id_v1').then(r=>r['opencli_context_id_v1']||null)"))
    if ctx:
        break
    time.sleep(1)
print("opencli_context_id_v1 =", ctx)
print(subprocess.run(["opencli", "profile", "list"], capture_output=True, text=True).stdout[-800:])
sess.stop()
