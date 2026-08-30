# Build Prompt — a browser-control skill for Claude Code

Paste everything below the line into Claude Code. It builds the skill from
scratch, in your environment, with the reasoning intact.

**Why a build prompt when working code already ships in `skill/`?** Because the
code is the easy part. What is expensive to rediscover is *why each line is
there* — every constraint below was learned by something failing in a way that
looked like something else. Read the spec even if you just copy the files: the
"Failure if you skip it" notes are the actual product.

---

You are building a Claude Code skill called `chrome-cdp` that lets you drive a
real Google Chrome browser through the Chrome DevTools Protocol.

## What to produce

1. `~/.claude/skills/chrome-cdp/chrome_cdp.py` — a single-file CLI, executable,
   run through `uv` with PEP 723 inline dependencies. No virtualenv, no
   `pip install` step for the user.
2. `~/.claude/skills/chrome-cdp/SKILL.md` — YAML frontmatter with `name` and a
   `description` written in trigger-phrase form so the harness knows when to
   load it, then the command reference and the safety model.
3. A shell alias in the user's `~/.zshrc`:
   `alias chrome-cdp='~/.claude/skills/chrome-cdp/chrome_cdp.py'`

Target: macOS. State that assumption in the SKILL.md rather than pretending to
portability you have not tested.

## The core design decision

Do **not** attach to the browser the user already has open.

Chrome's remote debugging is a **launch-time flag** (`--remote-debugging-port`),
and two Chrome processes cannot share a single `--user-data-dir`. Together those
two facts mean attaching a debugger to the user's running browser requires
killing and relaunching it. That destroys their session and their tabs.

Instead: launch a **second, dedicated Chrome instance** against a user-data-dir
you own, under `~/.config/chrome-cdp/<PROFILE>/`. Support multiple named
profiles so different sets of logged-in accounts stay isolated from one another.

Accept the consequence honestly: a fresh profile has **no logins and no
extensions**, and you cannot copy cookies in from the user's real Chrome —
App-Bound Encryption prevents it. Each profile gets logged into once, by hand.
Sessions then persist on disk. Document this as the tradeoff it is; it is the
first thing a user will ask about.

## Non-negotiable requirements

Each of these has a failure mode that does not look like its cause. That is why
they are requirements and not suggestions.

**1. Kill scoping is the safety invariant.**
`launch` and `quit` must only ever kill Chrome processes whose
`--user-data-dir` is inside your base dir. Enumerate processes, parse the
`--user-data-dir` argument out of each command line, and prefix-match it
against the base dir. Nothing else is ever a kill target.
> *Failure if you skip it:* you kill the user's real browser and lose their
> work. Note the invariant only holds because `~/.config/chrome-cdp` can never
> prefix-match Chrome's OS-default profile dir (`~/Library/Application Support/
> Google/Chrome`). If you relocate the base dir, re-check that property.

**2. `--remote-allow-origins=*` on every launch.**
> *Failure if you skip it:* the CDP WebSocket handshake is rejected with a
> **403**. The HTTP endpoints (`/json/list`, `/json/version`) keep working
> fine, so the browser looks healthy and only the parts that matter break.

**3. Four anti-throttle flags on every launch:**
`--disable-backgrounding-occluded-windows`, `--disable-background-timer-throttling`,
`--disable-renderer-backgrounding`, `--disable-features=CalculateNativeWinOcclusion`.
> *Failure if you skip it:* Chrome suspends timers in tabs that aren't
> frontmost. Any `await`ed JS in a backgrounded tab stalls indefinitely. It
> presents as your script hanging, not as a throttled tab, and it only
> reproduces when the window happens to be behind another one — so it looks
> intermittent.

**4. A dedicated port, overridable by env var.**
Default to 9223 rather than the conventional 9222, and read an override from
`CHROME_CDP_PORT`.
> *Failure if you skip it:* you fight whatever else is already debugging on
> 9222. The override also lets two automation browsers run side by side.
> Derive the port-conflict error message from the port constant — never
> hardcode the number in the string, or the two drift and the message sends
> people to the wrong port.

**5. Refuse a port conflict; never resolve it by force.**
If the port is up but no process under your base dir owns it, exit with a clear
`port_conflict` error. Do not kill it.
> *Failure if you skip it:* requirement 1, defeated by the one code path that
> most needs it.

**6. `launch` is idempotent, and profile-switch relaunches only your own.**
Already up on the requested profile → no-op. Up on a *different* profile of
yours → kill only yours, relaunch. Verify the process is actually gone (poll
after SIGKILL) rather than assuming, and fail loudly if it survives.

**7. Provide a URL-bound eval that refuses ambiguity.**
Alongside `--tab N`, provide `eval-on --url-contains SUBSTRING`, which
re-resolves the tab by URL on every call and **exits with an error** when zero
or more than one tab matches.
> *Failure if you skip it:* tab indexes are not stable. A tab opened or closed
> between two commands shifts every index after it, so `--tab 2` silently acts
> on a different page than the one you inspected. Refusing to guess converts a
> silent wrong-target into a loud error. Prefer it for anything with side
> effects.

**8. Never silence Chrome's output.**
Send the spawned browser's stdout and stderr to a log file
(`~/.cache/chrome-cdp.log`), appended with a timestamp per launch. When CDP
fails to come up, that log is the only evidence of why.

**9. `eval` must use `returnByValue: true` and `awaitPromise: true`.**
> *Failure if you skip it:* you get back opaque remote object handles instead
> of values, and any `async` expression returns an unresolved Promise rather
> than its result.
>
> *Document the corollary, because it will bite your users:* `Runtime.evaluate`
> compiles the expression as a plain script, so **bare top-level `await` is a
> SyntaxError** — `awaitPromise` resolves a returned Promise, it does not enable
> top-level await. Async work must be wrapped: `(async () => { ... })()`. Put a
> working example in your SKILL.md, since the natural thing to write fails.

## Commands to implement

`launch [--profile NAME]`, `status`, `tabs`, `navigate URL [--tab N]`,
`screenshot PATH [--tab N]`, `eval JS [--tab N]`,
`eval-on JS --url-contains SUB`, `html [--tab N]`, `newtab [URL]`,
`closetab [--tab N]`, `quit`.

Every command except `launch` and `quit` should fail fast with an actionable
message when CDP isn't up.

## Protocol reference

- `GET http://localhost:<port>/json/version` — liveness check.
- `GET http://localhost:<port>/json/list` — tabs; filter to `type == "page"`,
  and use each entry's `webSocketDebuggerUrl`.
- `PUT http://localhost:<port>/json/new?<url>` — open a tab.
- `GET http://localhost:<port>/json/close/<id>` — close a tab.
- Over the WebSocket, send `{"id": N, "method": ..., "params": {...}}` and read
  until a message with the matching `id` comes back. Useful methods:
  `Page.navigate`, `Page.captureScreenshot` (returns base64),
  `Runtime.evaluate`, `Input.dispatchMouseEvent`.

## Optional: a real-mouse-click helper

`element.click()` in JS dispatches a *synthetic* event. Controls that only
render on hover, drag handles, canvas-drawn widgets, and anything checking
`event.isTrusted` will ignore it. For those, dispatch real input:

1. Scroll the element into view, **then** read `getBoundingClientRect()` — in
   that order, or you get coordinates for where it used to be.
2. Send `Input.dispatchMouseEvent` `mouseMoved` to the target point and **wait**
   (~350ms) for hover-only controls to paint.
3. Then `mousePressed` and `mouseReleased` at the same point.

Skipping the move, or not waiting after it, is the reason a click "does
nothing." When the target renders *outside* its own element (a disclosure
triangle to the left of a heading), sweep a few negative x-offsets and detect
which one changed the page.

## Acceptance checks

Run these; all must pass before you call it done.

1. `chrome-cdp launch --profile PROFILE_A` → a Chrome window opens.
   `chrome-cdp status` prints `UP` and names the profile.
2. Run `launch --profile PROFILE_A` again → reports already-correct, and **no
   second window appears**.
3. **The safety check.** With your own everyday Chrome running, confirm the
   process list your kill logic produces contains only the PID under
   `~/.config/chrome-cdp/` — and that your personal Chrome's PID is absent.
   Then `chrome-cdp quit` and confirm your own browser is still open. Do not
   skip this one; it is the requirement everything else rests on.
4. `navigate https://example.com` → `eval 'document.title'` returns
   `Example Domain` → `screenshot /tmp/t.png` writes a non-zero-byte PNG.
5. Put the automation window fully behind another window, then run
   `eval 'await new Promise(r => setTimeout(r, 3000))'`. It must return in ~3
   seconds. If it hangs, an anti-throttle flag is missing.
6. `eval-on 'document.title' --url-contains example.com` succeeds; the same
   command with a substring matching zero tabs, and one matching two tabs, both
   **exit non-zero** rather than picking one.
7. `launch --profile PROFILE_B` while PROFILE_A is up → PROFILE_A's window
   closes, a fresh logged-out one opens, and your personal browser is untouched.
