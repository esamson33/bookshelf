# Running Bookshelf in the background

The server is an ordinary foreground process — `python3 -m bookshelf` and it
serves http://127.0.0.1:8765 until you Ctrl-C it. To have it simply *be there*,
macOS runs it as a **LaunchAgent**: launchd starts it at login, restarts it if it
dies, and keeps its output in a log file.

## The interpreter matters

There are two `python3` on this machine:

| | |
|---|---|
| `/opt/local/bin/python3` | MacPorts, 3.14.7 — **has `openai`**, first on `PATH` |
| `/usr/bin/python3` | Apple's system Python — no `openai`, lookups would fail |

launchd does not read your shell profile, so it gets no `PATH` and cannot guess.
The plist names the MacPorts interpreter by absolute path. Same for the working
directory: `.bookshelf.db`, `.bookshelf.toml` and `data/` all default to paths
beside the app, so `WorkingDirectory` has to be the checkout.

## The agent

`~/Library/LaunchAgents/com.esamson.bookshelf.plist`:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>com.esamson.bookshelf</string>
  <key>ProgramArguments</key>
  <array>
    <string>/opt/local/bin/python3</string>
    <string>-m</string>
    <string>bookshelf</string>
  </array>
  <key>WorkingDirectory</key><string>/Users/esamson/Desktop/Bookshelf</string>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>/Users/esamson/Library/Logs/bookshelf.log</string>
  <key>StandardErrorPath</key><string>/Users/esamson/Library/Logs/bookshelf.err</string>
</dict>
</plist>
```

`RunAtLoad` starts it at login; `KeepAlive` brings it back if it exits. To serve
a different port or shelf, add the flags to `ProgramArguments` as separate
`<string>` entries — `--port`, `9000` — one per element, not one string.

Install and start it:

```
launchctl load -w ~/Library/LaunchAgents/com.esamson.bookshelf.plist
```

Check it took:

```
launchctl list | grep bookshelf          # PID, then 0 for the exit status
curl -sI http://127.0.0.1:8765 | head -1
```

## Day to day

```
launchctl kickstart -k gui/$(id -u)/com.esamson.bookshelf   # restart after a code change
launchctl unload -w ~/Library/LaunchAgents/...plist         # stop, and stop starting at login
launchctl load   -w ~/Library/LaunchAgents/...plist         # start again
tail -f ~/Library/Logs/bookshelf.err                        # where a traceback lands
```

`-w` writes the choice down, so an `unload -w` survives a reboot — that is what
you want for "off", and why turning it back on needs the matching `load -w`.
Editing the plist needs an `unload` / `load` pair; `kickstart -k` only restarts
the process under the config launchd already has.

## The API key

launchd hands the process a bare environment, so `OPENAI_API_KEY` from your
shell is *not* there. Lookups still work because the key is read from
`.bookshelf.toml` in the working directory. If the Manage view ever says no key
resolves, either fix that file or give launchd the variable directly:

```xml
  <key>EnvironmentVariables</key>
  <dict><key>OPENAI_API_KEY</key><string>sk-…</string></dict>
```

— which does put the key in a plain file in your home directory, so the TOML is
the better of the two.

## Without the agent

For a one-off run that ends when you say so:

```
python3 -m bookshelf > /tmp/bookshelf.log 2>&1 &   # dies with the shell
nohup python3 -m bookshelf > /tmp/bookshelf.log 2>&1 &   # survives it
pkill -f 'python3 -m bookshelf'                    # stop either
```

Do not run one of these *and* the agent — the second to start will fail to bind
port 8765, and with `KeepAlive` launchd will keep retrying it.
