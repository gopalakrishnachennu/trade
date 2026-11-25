#!/usr/bin/env python3
"""Headless Chrome screenshot of the dashboard via CDP, with console errors.

Usage: screenshot_dashboard.py URL OUT.png [--wait 12] [--width 1600] [--resolve HOST=IP]
SSE keeps the page "loading" forever, so we drive Chrome over CDP, wait, then
capture the full page and print any console errors / exceptions.
"""
import argparse, asyncio, base64, json, os, shutil, subprocess, tempfile, time, urllib.request

import websockets


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("url"); ap.add_argument("out")
    ap.add_argument("--wait", type=float, default=12); ap.add_argument("--width", type=int, default=1600)
    ap.add_argument("--resolve", default=None, help="HOST=IP for --host-resolver-rules")
    ap.add_argument("--click", default=None, help="CSS selector to click before capture")
    ap.add_argument("--eval", default=None, help="JS expression to evaluate and print before capture")
    a = ap.parse_args()
    port = 9333
    prof = tempfile.mkdtemp(prefix="cdp-")
    args = [shutil.which("google-chrome") or "google-chrome", "--headless=new", "--no-sandbox", "--disable-gpu",
            "--hide-scrollbars", f"--remote-debugging-port={port}", f"--user-data-dir={prof}",
            f"--window-size={a.width},1200", "about:blank"]
    if a.resolve:
        host, ip = a.resolve.split("=")
        args.insert(1, f"--host-resolver-rules=MAP {host} {ip}")
    proc = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(50):
            try:
                tabs = json.load(urllib.request.urlopen(f"http://127.0.0.1:{port}/json"))
                ws_url = next(t["webSocketDebuggerUrl"] for t in tabs if t.get("type") == "page")
                break
            except Exception:
                time.sleep(0.2)
        async with websockets.connect(ws_url, max_size=200_000_000) as ws:
            n = 0
            logs = []

            async def call(method, **params):
                nonlocal n
                n += 1
                my = n
                await ws.send(json.dumps({"id": my, "method": method, "params": params}))
                while True:
                    msg = json.loads(await ws.recv())
                    if msg.get("id") == my:
                        return msg.get("result", {})
                    collect(msg)

            def collect(msg):
                m = msg.get("method")
                if m == "Runtime.exceptionThrown":
                    d = msg["params"]["exceptionDetails"]
                    logs.append(f"EXCEPTION: {d.get('text')} {(d.get('exception') or {}).get('description','')[:400]}")
                elif m == "Runtime.consoleAPICalled" and msg["params"]["type"] in ("error", "warning"):
                    logs.append(f"console.{msg['params']['type']}: " + " ".join(str(x.get('value', x.get('description'))) for x in msg["params"]["args"]))
                elif m == "Log.entryAdded" and msg["params"]["entry"]["level"] in ("error",):
                    logs.append(f"log: {msg['params']['entry']['text']} {msg['params']['entry'].get('url','')}")

            await call("Runtime.enable"); await call("Log.enable"); await call("Page.enable")
            await call("Page.navigate", url=a.url)
            end = time.time() + a.wait
            while time.time() < end:
                try:
                    msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=max(0.1, end - time.time())))
                    collect(msg)
                except asyncio.TimeoutError:
                    break
            if a.click:
                await call("Runtime.evaluate", expression=f"document.querySelector({json.dumps(a.click)}).click()")
                await asyncio.sleep(1.5)
            if a.eval:
                r = await call("Runtime.evaluate", expression=a.eval, returnByValue=True)
                print("eval:", (r.get("result") or {}).get("value"))
            m = await call("Page.getLayoutMetrics")
            h = int(m["cssContentSize"]["height"])
            await call("Emulation.setDeviceMetricsOverride", width=a.width, height=h, deviceScaleFactor=1, mobile=False)
            await asyncio.sleep(1.0)
            shot = await call("Page.captureScreenshot", format="png", captureBeyondViewport=True)
            os.makedirs(os.path.dirname(os.path.abspath(a.out)), exist_ok=True)
            open(a.out, "wb").write(base64.b64decode(shot["data"]))
            stats = await call("Runtime.evaluate", returnByValue=True, expression="""JSON.stringify({
              uplots: document.querySelectorAll('.uplot').length,
              kpis: document.querySelectorAll('.kpi').length,
              blotterRows: document.querySelectorAll('#blotter tbody tr').length,
              events: document.querySelectorAll('#events tbody tr').length,
              axisLabels: [...document.querySelectorAll('.u-label')].length,
              sys: (document.getElementById('sysBadge')||{}).textContent, risk: (document.getElementById('riskBadge')||{}).textContent})""")
            print(json.dumps({"out": a.out, "height": h, "stats": json.loads((stats.get("result") or {}).get("value") or "{}"), "errors": logs}, indent=1))
    finally:
        proc.terminate()
        shutil.rmtree(prof, ignore_errors=True)


asyncio.run(main())
