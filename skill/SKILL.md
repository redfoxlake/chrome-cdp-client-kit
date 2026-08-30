---
name: chrome-cdp
description: "Drive Google Chrome via Chrome DevTools Protocol, on a DEDICATED automation profile that NEVER touches your personal browser. Use when the user says 'use the browser', 'open in Chrome', 'screenshot the page', 'check what's on the page', 'click on', 'navigate to', or wants the agent to act in a browser with logged-in sessions. The shell alias is `chrome-cdp`."
version: 1.0.0
---

# /chrome-cdp — Drive Chrome via CDP, without hijacking the user's browser

The agent controls **Google Chrome** through Chrome DevTools Protocol on
`localhost:9223`. Chrome is launched against a **dedicated automation profile**
under `~/.config/chrome-cdp/<PROFILE>/` — a browser the agent owns, separate
from the user's daily driver.

## Why it works this way

Remote debugging is a **launch-time flag**, and two Chrome processes cannot
share one profile directory. So the naive approach — attach a debugger to the
browser the user already has open — requires killing and relaunching their real
browser. That takes over their entire session, and it is the thing this design
exists to avoid.

Driving a **separate, dedicated Chrome instance** removes the conflict entirely:

- **The user's own browser is NEVER touched.** `launch`/`quit` only ever kill
  Chrome processes whose `--user-data-dir` is inside `~/.config/chrome-cdp/`.
  Their personal Chrome uses the OS-default dir (`~/Library/Application Support/
  Google/Chrome`), which can never prefix-match that path, so it is never a
  kill target.
- **Own port (9223), not 9222.** Coexists with anything already debugging on
  the conventional port. Override with `CHROME_CDP_PORT`.
- **Anti-throttle flags baked into every launch** — background/occluded tabs
  don't freeze async JS. Tabs report `visibilityState: "visible"` even when
  Chrome isn't frontmost.

## The tradeoff: dedicated profiles start logged-out

A fresh automation profile has **no logins and no extensions**. Each profile is
logged into **once, by hand** — cookies can't be copied across profiles because
of App-Bound Encryption. Sessions then persist on disk. Separate profiles keep
different accounts cleanly isolated from each other.

## Safety

- A logged-in automation profile can act on those accounts. Same blast radius as
  the user typing JS into their own DevTools console. Don't navigate or click
  private surfaces unless the task requires it.
- Screenshots may capture personal data — store by path, never dump image bytes
  into shared logs or transcripts.

## Commands

```bash
chrome-cdp launch [--profile PROFILE_A]  # launch/attach Chrome on that profile's dir, CDP on :9223
chrome-cdp status                        # UP/DOWN, which profile, tab count
chrome-cdp tabs                          # list page tabs as [idx] title — url
chrome-cdp navigate <url> [--tab N]      # navigate tab to url
chrome-cdp screenshot <path> [--tab N]   # PNG screenshot of tab
chrome-cdp eval '<js>' [--tab N]         # run JS, print result (returnByValue + awaitPromise)
chrome-cdp eval-on '<js>' --url-contains <sub>   # URL-bound eval; refuses 0/ambiguous matches
chrome-cdp html [--tab N]                # outerHTML of tab
chrome-cdp newtab [url]                  # open new tab
chrome-cdp closetab [--tab N]            # close tab
chrome-cdp quit                          # quit ONLY the automation Chrome
```

`--profile` defaults to `default`. Name profiles for whatever you want isolated —
`PROFILE_A`, `PROFILE_B`, `work`, `clientA` (create + log in on first use).
`--tab N` defaults to 0.

## Launch semantics

- `launch --profile PROFILE_A` when already up on it → `already_correct` (no-op).
- `launch --profile PROFILE_B` while `PROFILE_A` is up → kills **only the
  PROFILE_A automation Chrome**, relaunches on PROFILE_B. Switching profiles
  relaunches the agent's own Chrome, never the user's.
- If `:9223` is held by a browser we did not launch → `port_conflict`, and it
  refuses to kill it.

## Prefer `eval-on` over `--tab N` for anything that matters

Tab **indexes are not stable**. A tab opened, closed, or reordered between two
commands shifts every index after it, so `--tab 2` can quietly act on a
different page than the one you inspected. `eval-on --url-contains` re-resolves
the target by URL on every call and **refuses** when zero or more than one tab
matches, turning a silent wrong-target into a loud error.

## Common patterns

**Read what's on a page:**
```bash
chrome-cdp eval 'document.body.innerText' | head -200
```

**Wait for an element, then screenshot.** Note the `(async () => { ... })()`
wrapper — it is required, not stylistic. `Runtime.evaluate` compiles the
expression as a plain script, so a bare top-level `await` is a **SyntaxError**.
Wrapping it returns a Promise instead, which `awaitPromise` then resolves:
```bash
chrome-cdp eval '(async () => { await new Promise(r => { const w = setInterval(() => { if (document.querySelector("#done")) { clearInterval(w); r(); } }, 200); }); return "ready"; })()'
chrome-cdp screenshot /tmp/done.png
```

**Click something that ignores synthetic events** (hover-rendered controls, drag
handles, canvas widgets) — `element.click()` will not work; use a real mouse event:
```bash
./cdp_click.py '#disclosure-triangle'
./cdp_click.py 'h2[data-id="section"]' --sweep -4,-8,-12,-16
```

## Implementation notes

- Script: `chrome_cdp.py` in this dir, run via uv (PEP 723 inline dep
  `websocket-client`). No separate install step.
- Chrome binary: `/Applications/Google Chrome.app/Contents/MacOS/Google Chrome`.
- Base dir for all automation profiles: `~/.config/chrome-cdp/`.
- Chrome stderr/stdout from `launch` → `~/.cache/chrome-cdp.log` (never silenced).
- `--remote-allow-origins=*` is required or the WebSocket handshake 403s.
- Kill scoping (the safety invariant) is a `--user-data-dir` prefix match
  against the base dir — see `_our_chrome_procs()`.
- **macOS only.** See `README.md` for the three OS-bound assumptions.
