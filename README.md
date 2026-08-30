# chrome-cdp — give your Claude agent a browser

A Claude Code skill that lets your agent drive a real Google Chrome — navigate,
read pages, click, run JavaScript, take screenshots — **without touching the
browser you use every day.**

## What's in the box

| Path | What it is |
|---|---|
| `BUILD_PROMPT.md` | Paste into Claude Code and it builds the skill from scratch, with the reasoning behind every constraint |
| `skill/chrome_cdp.py` | The working CLI, ready to use |
| `skill/SKILL.md` | The skill definition Claude Code loads |
| `skill/cdp_click.py` | Real-mouse-click helper for UI that ignores synthetic clicks |

Two ways to use this: **copy `skill/` and go**, or **run the build prompt** and
have your agent construct it while explaining itself. The build prompt is worth
reading either way — it documents the failure mode behind each design choice,
and those are the parts that are expensive to rediscover.

## Install (4 steps)

**1. Install `uv`** (if you don't have it). The script declares its own
dependencies inline, so there's no virtualenv to manage:
```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

**2. Drop the skill in place:**
```bash
mkdir -p ~/.claude/skills/chrome-cdp
cp skill/*.py skill/SKILL.md ~/.claude/skills/chrome-cdp/
chmod +x ~/.claude/skills/chrome-cdp/*.py
```

**3. Add the alias** to `~/.zshrc`, then open a new shell:
```bash
echo "alias chrome-cdp='~/.claude/skills/chrome-cdp/chrome_cdp.py'" >> ~/.zshrc
```

**4. Launch and confirm:**
```bash
chrome-cdp launch --profile PROFILE_A
chrome-cdp status          # → UP, profile PROFILE_A
chrome-cdp navigate https://example.com
chrome-cdp eval 'document.title'   # → Example Domain
```

## The one tradeoff you need to know up front

**The automation browser starts logged out, and you log it in by hand — once.**

Your existing Chrome cookies cannot be copied in. Chrome encrypts them with
App-Bound Encryption, which ties them to the profile that created them. There is
no import path, and anything claiming otherwise is either stale or a
credential-stealing technique.

So the first time you want your agent working inside some account, you launch
the profile, log in yourself in that window, and you're done — the session
persists on disk, and every later run is already authenticated.

Use separate profiles (`--profile`) for sets of accounts you want kept apart.
Switching profiles relaunches the automation browser; it never touches yours.

## Why it can't hijack your browser

`launch` and `quit` only kill Chrome processes whose `--user-data-dir` lives
inside `~/.config/chrome-cdp/`. Your personal Chrome uses the operating
system's default profile directory, which can never match that path — so it is
never a candidate for shutdown, by construction rather than by care.

It also runs on port **9223** instead of Chrome's conventional 9222, so it
coexists with anything else already debugging. Override with `CHROME_CDP_PORT`
to run two automation browsers side by side.

And if the port is held by a browser this tool did not launch, it **refuses to
proceed** rather than clearing the way by force.

## Safety, plainly

A logged-in automation profile can act on those accounts — post, send, delete —
with the same reach as you sitting at that browser. It is a real browser with
real sessions, not a sandbox. Scope what you log it into accordingly, and treat
screenshots as potentially containing personal data: pass them around by file
path, never by pasting image bytes into a shared log or transcript.

## Requirements and limits

- **macOS only.** Three things assume it, and each is a small fix rather than a
  rewrite if you port it:
  - the Chrome binary path (`/Applications/Google Chrome.app/...`)
  - process enumeration via `ps -Axo`
  - `os.kill(pid, 9)`
- Google Chrome installed.
- Python 3.11+ via `uv`.

## Troubleshooting

**`cdp_timeout: Chrome launched but CDP never came up`** — read
`~/.cache/chrome-cdp.log`. Chrome's own stderr is there and is never silenced.

**`port_conflict`** — something else holds the port. Either free it, or set
`CHROME_CDP_PORT` to an unused one. This tool will not kill a browser it didn't
launch.

**`SyntaxError: await is only valid in async functions`** — expected. Wrap the
expression: `eval '(async () => { ...; return x; })()'`. CDP compiles what you
pass as a plain script, so top-level `await` is not available.

**A click does nothing** — the target probably ignores synthetic events. Use
`cdp_click.py`, which dispatches real mouse input.

**Async JS hangs when the window is behind another** — an anti-throttle launch
flag is missing. All four are listed in `BUILD_PROMPT.md`.
