#!/usr/bin/env -S uv run --quiet --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["websocket-client>=1.7"]
# ///
"""chrome-cdp — drive Google Chrome via Chrome DevTools Protocol.

Chrome is launched against a DEDICATED per-purpose user-data-dir under
~/.config/chrome-cdp/ — NOT your personal browser. Consequences:

  * Your real browser is NEVER touched. `launch`/`quit` only ever kill Chrome
    processes whose --user-data-dir is inside our base dir. That is the whole
    point: an automation browser that cannot take over your daily driver.
  * A dedicated profile starts logged-out + extension-free. Each profile is
    logged into once by hand; sessions then persist on disk in that profile's
    dir. Cookies cannot be copied in from your real Chrome (App-Bound
    Encryption), so the one-time manual login is unavoidable by design.
  * Anti-throttle flags are baked into every launch, so background/occluded
    tabs don't freeze async JS.

Subcommands:
  launch [--profile NAME]   Launch/attach Chrome on the NAME profile's dedicated
                            user-data-dir with CDP on the configured port.
                            Idempotent if already up on that profile; relaunches
                            (killing only OUR automation Chrome) if switching.
  status                    Print whether CDP is up, which profile, tab count.
  tabs                      List page tabs with index, title, url.
  navigate URL              Navigate active (or --tab N) tab to URL.
  screenshot PATH           PNG screenshot of active (or --tab N) tab to PATH.
  eval JS                   Run JS expression in active tab; print result.
  eval-on --url-contains S  URL-bound eval: pick the tab by URL substring.
  html                      Print outerHTML of active tab.
  newtab [URL]              Open a new tab (optionally to URL).
  closetab                  Close active (or --tab N) tab.
  quit                      Quit ONLY our automation Chrome (never your browser).

All commands except `launch`/`quit` require CDP to be up. Run `launch` first.
Stderr from spawned Chrome goes to ~/.cache/chrome-cdp.log.

macOS only. See README.md for the three OS-bound assumptions.
"""

import argparse
import base64
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request

from websocket import create_connection

# A DEDICATED port. Chrome's conventional debug port is 9222; using a different
# one by default means this coexists with anything else already debugging on
# 9222 instead of fighting it for the socket. Override with CHROME_CDP_PORT to
# run two independent automation browsers side by side.
PORT = int(os.environ.get("CHROME_CDP_PORT", "9223"))
CHROME_BIN = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
CDP_URL = f"http://localhost:{PORT}"
LOG_PATH = os.path.expanduser("~/.cache/chrome-cdp.log")

# Every automation profile lives under this base dir. Killing/quitting is scoped
# to "any Chrome whose --user-data-dir starts with BASE_DIR" — your personal
# Chrome uses the OS-default user-data-dir (~/Library/Application Support/
# Google/Chrome on macOS), which can never prefix-match this path. That is the
# safety invariant; if you change BASE_DIR, keep it somewhere the OS default
# cannot fall under.
BASE_DIR = os.path.expanduser("~/.config/chrome-cdp")
DEFAULT_PROFILE = "default"

# Optional friendly-name aliases → canonical profile dir, e.g.
# {"work": "PROFILE_A"} lets `--profile work` and `--profile PROFILE_A` resolve
# to the same directory. Matched case-insensitively. Empty by default.
PROFILE_ALIASES: dict[str, str] = {}

# Anti-throttle + clean-launch flags on EVERY launch. Without these, Chrome
# suspends timers in backgrounded/occluded tabs and async JS in a tab that
# isn't frontmost silently stalls — the failure looks like a hung script, not a
# throttled one, which is what makes it expensive to diagnose.
LAUNCH_FLAGS = [
    f"--remote-debugging-port={PORT}",
    # Required, or the CDP WebSocket handshake is rejected with a 403.
    "--remote-allow-origins=*",
    "--disable-backgrounding-occluded-windows",
    "--disable-background-timer-throttling",
    "--disable-renderer-backgrounding",
    "--disable-features=CalculateNativeWinOcclusion",
    "--no-first-run",
    "--no-default-browser-check",
]


# ---- CDP plumbing -------------------------------------------------------
def cdp_get(path: str):
    with urllib.request.urlopen(f"{CDP_URL}{path}", timeout=2) as r:
        return json.loads(r.read())


def is_up() -> bool:
    try:
        cdp_get("/json/version")
        return True
    except (urllib.error.URLError, ConnectionError, OSError, TimeoutError):
        return False


def list_pages() -> list[dict]:
    return [t for t in cdp_get("/json/list") if t.get("type") == "page"]


def pick_tab(idx: int | None) -> dict:
    pages = list_pages()
    if not pages:
        sys.exit("no page tabs found — open one first")
    if idx is None:
        return pages[0]
    if idx < 0 or idx >= len(pages):
        sys.exit(f"--tab {idx} out of range (0..{len(pages) - 1})")
    return pages[idx]


def cdp_send(ws_url: str, method: str, params: dict | None = None) -> dict:
    ws = create_connection(ws_url, timeout=20)
    try:
        ws.send(json.dumps({"id": 1, "method": method, "params": params or {}}))
        while True:
            msg = json.loads(ws.recv())
            if msg.get("id") == 1:
                if "error" in msg:
                    sys.exit(f"CDP error on {method}: {msg['error']}")
                return msg.get("result", {})
    finally:
        ws.close()


def require_up():
    if not is_up():
        sys.exit("CDP endpoint not reachable — run `chrome-cdp launch` first")


# ---- profile-aware, isolation-safe launch -------------------------------
def _profile_dir(name: str) -> str:
    """Resolve a friendly profile name to its dedicated user-data-dir.
    Sanitized so a stray name can't escape BASE_DIR."""
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", name).strip("_") or DEFAULT_PROFILE
    safe = PROFILE_ALIASES.get(safe.lower(), safe)  # resolve alias (case-insensitive)
    return os.path.join(BASE_DIR, safe)


def _our_chrome_procs() -> list[tuple[int, str]]:
    """(pid, user-data-dir) for every running Chrome under BASE_DIR.
    This is the ONLY set of processes launch/quit will ever kill."""
    out = subprocess.run(
        ["ps", "-Axo", "pid=,command="], capture_output=True, text=True
    ).stdout
    procs = []
    for line in out.splitlines():
        line = line.strip()
        if not line:
            continue
        pid_str, _, cmd = line.partition(" ")
        m = re.search(r"--user-data-dir=(\S+)", cmd)
        if m and m.group(1).startswith(BASE_DIR) and "Google Chrome" in cmd:
            try:
                procs.append((int(pid_str), m.group(1)))
            except ValueError:
                pass
    return procs


def _running_profile_dir() -> str | None:
    """Which of OUR automation profiles currently owns a live process (if any)."""
    procs = _our_chrome_procs()
    return procs[0][1] if procs else None


def _kill_our_chrome() -> bool:
    """Kill ONLY our automation Chrome (scoped to BASE_DIR). Never your browser."""
    procs = _our_chrome_procs()
    if not procs:
        return True
    for pid, _ in procs:
        try:
            os.kill(pid, 9)
        except (ProcessLookupError, PermissionError):
            pass
    for _ in range(40):
        if not _our_chrome_procs():
            return True
        time.sleep(0.25)
    return False


def _spawn_chrome(pdir: str):
    os.makedirs(pdir, exist_ok=True)
    os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
    log = open(LOG_PATH, "ab")
    log.write(
        f"\n=== {time.strftime('%Y-%m-%d %H:%M:%S')} launching Chrome user-data-dir={pdir} ===\n".encode()
    )
    log.flush()
    cmd = [CHROME_BIN, f"--user-data-dir={pdir}", *LAUNCH_FLAGS, "about:blank"]
    subprocess.Popen(cmd, stdout=log, stderr=log, start_new_session=True)


def cmd_launch(args):
    profile = getattr(args, "profile", None) or DEFAULT_PROFILE
    target = _profile_dir(profile)

    # Already up on the requested profile → no-op.
    if is_up() and _running_profile_dir() == target:
        print(f"already_correct: CDP up on profile {profile!r} ({target})")
        return

    # The port is held, but NOT by one of our automation profiles → it's your
    # own browser or an unrelated debug session. Refuse rather than kill it.
    if is_up() and _running_profile_dir() is None:
        sys.exit(
            f"port_conflict: :{PORT} is held by a browser we did not launch. "
            "chrome-cdp will NOT kill it. Free the port, or set CHROME_CDP_PORT "
            "to an unused one, and retry."
        )

    # Switching from a different automation profile → kill only ours, relaunch.
    if _our_chrome_procs():
        if not _kill_our_chrome():
            sys.exit("chrome_would_not_die: our automation Chrome survived SIGKILL — kill it manually and retry")

    _spawn_chrome(target)
    for _ in range(60):
        if is_up():
            break
        time.sleep(0.5)
    else:
        sys.exit(f"cdp_timeout: Chrome launched but CDP never came up — check {LOG_PATH}")
    print(f"relaunched: CDP up @ {CDP_URL} on profile {profile!r} ({len(list_pages())} page tabs)")


def cmd_status(args):
    if not is_up():
        print("DOWN")
        sys.exit(1)
    v = cdp_get("/json/version")
    pages = list_pages()
    rpd = _running_profile_dir()
    prof = os.path.basename(rpd) if rpd else "(not an automation profile — your own browser?)"
    print(f"UP — {v.get('Browser')} @ {CDP_URL}")
    print(f"profile: {prof}")
    print(f"tabs: {len(pages)}")


def cmd_tabs(args):
    require_up()
    for i, t in enumerate(list_pages()):
        title = (t.get("title") or "").replace("\n", " ")[:60]
        url = (t.get("url") or "")[:80]
        print(f"[{i}] {title} — {url}")


def cmd_navigate(args):
    require_up()
    tab = pick_tab(args.tab)
    cdp_send(tab["webSocketDebuggerUrl"], "Page.navigate", {"url": args.url})
    print(f"navigated tab [{args.tab if args.tab is not None else 0}] → {args.url}")


def cmd_screenshot(args):
    require_up()
    tab = pick_tab(args.tab)
    res = cdp_send(tab["webSocketDebuggerUrl"], "Page.captureScreenshot", {"format": "png"})
    data = base64.b64decode(res["data"])
    path = os.path.expanduser(args.path)
    os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
    with open(path, "wb") as f:
        f.write(data)
    print(f"saved {len(data)} bytes → {path}")


def _eval_tab(tab, js):
    res = cdp_send(
        tab["webSocketDebuggerUrl"],
        "Runtime.evaluate",
        {"expression": js, "returnByValue": True, "awaitPromise": True},
    )
    r = res.get("result", {})
    if r.get("type") == "object" and "value" in r:
        print(json.dumps(r["value"], indent=2, default=str))
    elif "value" in r:
        print(r["value"])
    else:
        print(json.dumps(r, indent=2, default=str))


def cmd_eval(args):
    require_up()
    _eval_tab(pick_tab(args.tab), args.js)


def cmd_eval_on(args):
    """URL-bound eval: resolve the tab by URL substring EVERY call, refuse if
    0 or >1 match.

    Tab INDEXES are not stable — a tab opened, closed or reordered between two
    commands silently shifts every index after it, so `--tab 2` can act on a
    different page than the one you looked at. Binding by URL and refusing an
    ambiguous match turns that silent wrong-target into a loud error."""
    require_up()
    sub = args.url_contains
    matches = [t for t in list_pages() if sub in (t.get("url") or "")]
    if not matches:
        sys.exit(f"ownership: no tab URL contains {sub!r} — refusing to guess a tab")
    if len(matches) > 1:
        urls = "; ".join((t.get("url") or "")[:70] for t in matches)
        sys.exit(f"ownership: {len(matches)} tabs match {sub!r} (ambiguous) — {urls}")
    _eval_tab(matches[0], args.js)


def cmd_html(args):
    require_up()
    tab = pick_tab(args.tab)
    res = cdp_send(
        tab["webSocketDebuggerUrl"],
        "Runtime.evaluate",
        {"expression": "document.documentElement.outerHTML", "returnByValue": True},
    )
    print(res["result"]["value"])


def cmd_newtab(args):
    require_up()
    url = args.url or "about:blank"
    with urllib.request.urlopen(
        urllib.request.Request(f"{CDP_URL}/json/new?{url}", method="PUT"), timeout=5
    ) as r:
        tab = json.loads(r.read())
    print(f"opened tab → {tab.get('url')} (id {tab.get('id')})")


def cmd_closetab(args):
    require_up()
    tab = pick_tab(args.tab)
    with urllib.request.urlopen(f"{CDP_URL}/json/close/{tab['id']}", timeout=5) as r:
        body = r.read().decode().strip()
    print(f"closed tab [{args.tab if args.tab is not None else 0}] — {body}")


def cmd_quit(args):
    # Scoped kill — only our automation Chrome, never your personal browser.
    if _kill_our_chrome():
        print("quit automation Chrome (your browser untouched)")
    else:
        print("WARN: some automation Chrome procs survived — check manually", file=sys.stderr)


def main():
    p = argparse.ArgumentParser(
        prog="chrome-cdp", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    lp = sub.add_parser("launch", help="Launch/attach Chrome with CDP")
    lp.add_argument("--profile", help="Dedicated automation profile name (e.g. PROFILE_A). Default 'default'.")
    sub.add_parser("status", help="Check CDP status + which profile")
    sub.add_parser("tabs", help="List page tabs")
    sub.add_parser("quit", help="Quit ONLY our automation Chrome")

    nav = sub.add_parser("navigate", help="Navigate a tab to URL")
    nav.add_argument("url")
    nav.add_argument("--tab", type=int, help="tab index (default 0)")

    sc = sub.add_parser("screenshot", help="PNG screenshot of a tab")
    sc.add_argument("path")
    sc.add_argument("--tab", type=int)

    ev = sub.add_parser("eval", help="Run JS in a tab and print result")
    ev.add_argument("js")
    ev.add_argument("--tab", type=int)

    eo = sub.add_parser("eval-on", help="URL-bound eval: pick the tab by URL substring (refuses ambiguous)")
    eo.add_argument("js")
    eo.add_argument("--url-contains", required=True, dest="url_contains", help="substring the target tab's URL must contain")

    h = sub.add_parser("html", help="Print outerHTML of a tab")
    h.add_argument("--tab", type=int)

    nt = sub.add_parser("newtab", help="Open a new tab")
    nt.add_argument("url", nargs="?")

    ct = sub.add_parser("closetab", help="Close a tab")
    ct.add_argument("--tab", type=int)

    args = p.parse_args()
    handlers = {
        "launch": cmd_launch,
        "status": cmd_status,
        "tabs": cmd_tabs,
        "quit": cmd_quit,
        "navigate": cmd_navigate,
        "screenshot": cmd_screenshot,
        "eval": cmd_eval,
        "eval-on": cmd_eval_on,
        "html": cmd_html,
        "newtab": cmd_newtab,
        "closetab": cmd_closetab,
    }
    handlers[args.cmd](args)


if __name__ == "__main__":
    main()
