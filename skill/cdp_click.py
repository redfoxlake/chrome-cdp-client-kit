#!/usr/bin/env -S uv run --quiet --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["websocket-client>=1.7"]
# ///
"""cdp_click — dispatch a REAL mouse click at an element, via CDP.

Why this exists as a separate tool: `element.click()` in JS is a synthetic
event. A large class of UI simply does not respond to it — controls that only
render on hover, drag handles, canvas-drawn widgets, and anything whose
framework listens for `mousedown`/`mouseup` or checks `event.isTrusted`. For
those you need `Input.dispatchMouseEvent`, which enters Chrome at the browser
level and is indistinguishable from a human click.

The sequence that makes hover-rendered controls work is the whole trick:
scroll the element into view, read its box, dispatch `mouseMoved` FIRST and
wait for the hover-only control to paint, and only then press and release.
Skipping the move (or not waiting after it) is why "the click did nothing."

Usage:
  cdp_click.py '<css-selector>'                 # click the element's center
  cdp_click.py '<css-selector>' --dx -12        # click 12px LEFT of its left edge
  cdp_click.py '<css-selector>' --dx -12 --dy 8 # offset from the top-left corner
  cdp_click.py '<css-selector>' --tab 2         # target a specific tab
  cdp_click.py '<css-selector>' --sweep -4,-8,-12,-16,-20

`--dx`/`--dy` are measured from the element's TOP-LEFT corner; omit both to hit
the center. Negative dx is the common case for a control that renders just
outside its own element (a disclosure triangle to the left of a heading).

`--sweep` tries each dx in turn and stops at the first one that changes the
page, which is how you find the right offset when the target is invisible
until hovered. It reports which offset worked so you can hardcode it after.

Exits non-zero if the selector matches nothing.
"""

import argparse
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import chrome_cdp  # noqa: E402
from websocket import create_connection  # noqa: E402


def main():
    ap = argparse.ArgumentParser(prog="cdp_click", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("selector", help="CSS selector for the target element")
    ap.add_argument("--tab", type=int, default=0, help="tab index (default 0)")
    ap.add_argument("--dx", type=float, help="x offset from the element's left edge")
    ap.add_argument("--dy", type=float, help="y offset from the element's top edge")
    ap.add_argument("--sweep", help="comma-separated dx values to try in order, "
                                    "stopping at the first that changes the page")
    ap.add_argument("--hover-ms", type=int, default=350,
                    help="wait after mouseMoved for hover-only controls to render (default 350)")
    ap.add_argument("--settle-ms", type=int, default=600,
                    help="wait after the click before checking for a change (default 600)")
    args = ap.parse_args()

    chrome_cdp.require_up()
    tab = chrome_cdp.pick_tab(args.tab)
    ws = create_connection(tab["webSocketDebuggerUrl"], timeout=20)
    state = {"id": 0}

    def send(method, params=None):
        state["id"] += 1
        mid = state["id"]
        ws.send(json.dumps({"id": mid, "method": method, "params": params or {}}))
        while True:
            msg = json.loads(ws.recv())
            if msg.get("id") == mid:
                if "error" in msg:
                    sys.exit(f"CDP error on {method}: {msg['error']}")
                return msg.get("result", {})

    def ev(expr):
        r = send("Runtime.evaluate",
                 {"expression": expr, "returnByValue": True, "awaitPromise": True})
        return r.get("result", {}).get("value")

    sel = json.dumps(args.selector)  # safe JS string literal

    # Scroll into view and read the box. Reading the rect BEFORE scrolling would
    # give coordinates for where the element used to be.
    rect_json = ev(
        f"(()=>{{const e=document.querySelector({sel});if(!e)return null;"
        f"e.scrollIntoView({{block:'center'}});const r=e.getBoundingClientRect();"
        f"return JSON.stringify({{left:r.left,top:r.top,width:r.width,height:r.height}});}})()"
    )
    if not rect_json:
        ws.close()
        sys.exit(f"no element matches {args.selector!r}")
    rect = json.loads(rect_json)

    def fingerprint():
        """Cheap signal for 'did the page change'. Covers both an element that
        expands in place and one that reveals siblings after it."""
        return ev(
            f"(()=>{{const e=document.querySelector({sel});if(!e)return '';"
            f"let s=e.innerText||'';let n=e.nextElementSibling,c=0;"
            f"while(n&&c<8){{s+=' '+(n.innerText||'');n=n.nextElementSibling;c++;}}"
            f"return s.slice(0,2000);}})()"
        ) or ""

    def click_at(x, y):
        send("Input.dispatchMouseEvent", {"type": "mouseMoved", "x": x, "y": y})
        time.sleep(args.hover_ms / 1000)
        send("Input.dispatchMouseEvent",
             {"type": "mousePressed", "x": x, "y": y,
              "button": "left", "clickCount": 1, "buttons": 1})
        send("Input.dispatchMouseEvent",
             {"type": "mouseReleased", "x": x, "y": y,
              "button": "left", "clickCount": 1, "buttons": 0})
        time.sleep(args.settle_ms / 1000)

    before = fingerprint()

    if args.sweep:
        offsets = [float(x) for x in args.sweep.split(",")]
        y = rect["top"] + min(14, rect["height"] / 2)
        for dx in offsets:
            click_at(rect["left"] + dx, y)
            if fingerprint() != before:
                print(json.dumps({"selector": args.selector, "rect": rect,
                                  "result": "changed", "dx": dx}))
                ws.close()
                return
        print(json.dumps({"selector": args.selector, "rect": rect,
                          "result": "no_change", "tried": offsets}))
        ws.close()
        return

    if args.dx is None and args.dy is None:
        x = rect["left"] + rect["width"] / 2
        y = rect["top"] + rect["height"] / 2
    else:
        x = rect["left"] + (args.dx or 0)
        y = rect["top"] + (args.dy or 0)

    click_at(x, y)
    print(json.dumps({"selector": args.selector, "rect": rect, "at": {"x": x, "y": y},
                      "result": "changed" if fingerprint() != before else "no_change"}))
    ws.close()


if __name__ == "__main__":
    main()
