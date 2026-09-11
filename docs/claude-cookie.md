# Setting the Claude cookie by hand

The Claude section reads your claude.ai usage using your **browser session
cookie**. Normally it finds that cookie on its own from a running Firefox- or
Chromium-family browser, so there is nothing to do — just stay logged in.

Use this guide only if your browser isn't picked up automatically (an
unsupported browser, a hardened profile, a container), and the panel keeps
showing a "log in to claude.ai" message even though you *are* logged in. You'll
hand the panel the cookie once, from a saved network capture (`.har` file).

> **Heads up:** a `.har` contains your full session cookie in plain text —
> anyone with the file can act as you on claude.ai. Keep it local and delete it
> as soon as you're done (the last step does this for you).

## 1. Capture a request to the usage endpoint

1. Open **claude.ai** in your browser and make sure you're logged in.
2. Open the developer tools (**F12**) and click the **Network** tab.
3. Tick **Preserve log**, then reload the page (**F5**).
4. In the Network filter box type `usage`. You want a request whose URL ends in
   `/usage` (`…/api/organizations/…/usage`). Reopen the Claude **Settings →
   Usage** page if you don't see one.

## 2. Save it as a .har

Right-click any request in the Network list → **Save all as HAR** (Firefox:
"Save All As HAR"; Chrome/Chromium: "Save all as HAR with content"). Save it
into your **Downloads** folder. The filename doesn't matter.

## 3. Import the cookie

Paste this into a terminal. It reads the newest `.har` in `~/Downloads`, pulls
the cookie out of the `/usage` request, writes it where the panel looks for it
(`~/.config/mint-hud/.claude_web_cookie`, mode 600), and then deletes the
`.har`:

```bash
python3 - <<'PY'
import json, os, glob
hars = sorted(glob.glob(os.path.expanduser('~/Downloads/*.har')), key=os.path.getmtime)
if not hars:
    raise SystemExit('no .har found in ~/Downloads')
path = hars[-1]
with open(path) as f:
    data = json.load(f)
entry = next(e for e in data['log']['entries']
             if e['request']['url'].rstrip('/').endswith('/usage'))
cookie = next(h['value'] for h in entry['request']['headers']
              if h['name'].lower() == 'cookie')
out = os.path.expanduser('~/.config/mint-hud/.claude_web_cookie')
os.makedirs(os.path.dirname(out), exist_ok=True)
with open(out, 'w') as f:
    f.write(cookie.strip() + '\n')
os.chmod(out, 0o600)
os.remove(path)                     # the .har held your cookie in plain text
print(f'saved cookie ({len(cookie)} chars) from {os.path.basename(path)}; .har deleted')
PY
```

The panel picks the cookie up within a few seconds — the Claude slot should
start showing your session and weekly usage.

## Notes

- The cookie is stored only on your machine, at
  `~/.config/mint-hud/.claude_web_cookie` (permissions `600`), and is never
  committed or sent anywhere except claude.ai's own usage endpoint.
- Sessions expire. If the slot goes back to a login message weeks later, just
  repeat these steps — or switch to a supported browser and stay logged in, and
  it refreshes itself.
