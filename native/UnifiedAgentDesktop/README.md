# Unified Agent desktop (slice S0)

A native window over the Mac hub's `/ui`, built with Tauri 2. This is slice
**S0, "Live with it"**, from the desktop plan in `anonb3ll/unified_brain`
(`projects/unified-agent-desktop/REVIEW-AND-OPTIONS.md` §5). the operator's
decisions 3, 4 and 6 (thread `2026-10-01-desktop-app-decisions`) shape it:
Tauri, a thin shell with no native access for the remote page, Linux desktop first.

| | |
|---|---|
| **What it does** | Opens `<hub>/ui` in a window. Single instance. Window size and position persist. `--toggle` shows or hides it (for a Hyprland bind). Tray with Show, Hide, Quit; closing the window hides it to the tray. `unified-agent://VIEW?k=v` opens `/ui?view=VIEW&k=v`. A bundled "hub unreachable" page retries every 5 s and moves on once the hub answers. Off-hub links open in the system browser; `file:`, `javascript:` and other schemes are refused. |
| **What it does not do yet** | No IPC for any page, no keychain token, no notifications, no inbox count (S1). No object paths such as `unified-agent://task/1556` (S2 router); those are refused, not guessed. If the hub drops *after* the page loads, the page's own error handling applies; the offline page is shown only at launch and on deep links. |

## No native access for the remote page

Decision 4 reverses DESIGN §21.1 for v1: the remote `/ui` gets **no IPC**.
Tauri only allows a command when a capability grants it, so:

- `tauri.conf.json` sets `app.security.capabilities` to `[]` and there is
  **no `capabilities/` directory** (an empty list loads every file in it).
- `app.withGlobalTauri` is `false`.
- The window, tray, toggling and deep links are all driven from Rust.

`core/src/tests.rs` (`tauri_config_grants_no_capabilities`) fails if either
condition changes. `scripts/smoke.sh` checks the behavior: a hostile `/ui`
tries five plugin and core commands and every one is denied.

S1 adds a *bundled* settings window. Whatever it is granted must be scoped to
that window's label and to bundled origins only, never with `remote`.

## Layout

| Path | What |
|---|---|
| `core/` | `ua-desktop-core`: engine-independent logic (hub URL, deep-link mapping, navigation policy, argv). No Tauri dependency, so it tests anywhere and survives a Chromium swap. |
| `src-tauri/` | The Tauri binary `unified-agent-desktop`. |
| `ui/` | Bundled pages. Only `offline.html` in S0, with its CSP in a meta tag and no inline script. |
| `scripts/smoke.sh` | End-to-end smoke test against a fake hub. |
| `packaging/linux/` | Desktop entry registering the `unified-agent://` handler. |

## Build and run (Linux desktop / Arch)

```bash
sudo pacman -S --needed webkit2gtk-4.1 libayatana-appindicator librsvg xdotool base-devel
cd native/UnifiedAgentDesktop
cargo build --release
./target/release/unified-agent-desktop --hub http://<mac-hub-host>:8765
```

Debian or Ubuntu: `libwebkit2gtk-4.1-dev libayatana-appindicator3-dev
librsvg2-dev libxdo-dev libssl-dev build-essential pkg-config`.

The hub comes from, in order: `--hub URL`, `$UA_DESKTOP_HUB_URL`, `hub_url` in
`~/.config/unified-agent-desktop/config.json`, then `http://127.0.0.1:8765`.
An invalid value is an error. The app does not fall back to the next source.
The URL must be a bare origin (`/ui` is tolerated); credentials, queries and
other paths are refused.

```json
{ "hub_url": "http://<mac-hub-host>:8765" }
```

Sign in through the page as in a browser. The session cookie lives in the
webview's own profile.

Linux desktop notes (verified on the desktop, 2026-10-01):

- **NVIDIA + Wayland:** WebKitGTK's DMA-BUF renderer exits at startup with
  `Error 71 (Protocol error) dispatching to Wayland display` (GTX 1070,
  Hyprland). The app sets `WEBKIT_DISABLE_DMABUF_RENDERER=1` on Linux unless
  you set it yourself.
- **Tray host:** current Linux desktop runs its own panel (quickshell,
  `linux-desktop-launch-shell`), not Waybar. That panel owns
  `org.kde.StatusNotifierWatcher`, so the tray icon appears there.

### Hyprland bind and deep links

Current Linux desktop configures Hyprland in Lua. Stock Linux desktop already uses
SUPER+A with other modifiers (SHIFT, CTRL, CTRL+SHIFT, SHIFT+ALT), so the
desktop install uses SUPER+ALT+A. Check `hyprctl binds -j` before picking a key.

```lua
-- ~/.config/hypr/bindings.lua
-- >>> unified-agent-desktop >>>
o.bind("SUPER + ALT + A", "Unified Agent", "unified-agent-desktop --toggle")
-- <<< unified-agent-desktop <<<

-- ~/.config/hypr/autostart.lua (runs at Hyprland start; a cold --toggle shows the window)
-- >>> unified-agent-desktop >>>
o.launch_on_start("unified-agent-desktop --toggle")
-- <<< unified-agent-desktop <<<
```

Run `hyprctl reload` and check `hyprctl configerrors` after editing. On older
Hyprland configs using `bindings.conf`, the equivalent is
`bindd = SUPER ALT, A, Unified Agent, exec, unified-agent-desktop --toggle`.

Install the binary on `PATH`, then either copy
`packaging/linux/unified-agent-desktop.desktop` to
`~/.local/share/applications/` and run
`xdg-mime default unified-agent-desktop.desktop x-scheme-handler/unified-agent`,
or run `unified-agent-desktop --register-scheme` once.

```bash
xdg-open 'unified-agent://approvals'   # opens /ui?view=approvals in the running window
```

## Checks

```bash
cargo test -p ua-desktop-core
cargo clippy --all-targets -- -D warnings
cargo fmt --all -- --check
xvfb-run -a dbus-run-session -- scripts/smoke.sh   # after cargo build
```

## Provisional choices

- Name `Unified Agent`, identifier `dev.unifiedagent.desktop`, scheme
  `unified-agent`: DESIGN D11's recommendation. Hub decision task #1617 is
  still open; decision 7 removed it as a hard prerequisite, so S0 uses the
  recommendation and changes only if #1617 says otherwise.
- `icons/icon.png` is a generated placeholder.
- The bundle targets are `deb` and `appimage`. Linux desktop runs the release
  binary directly. macOS (`app`, `dmg`) arrives with the week 7 Mac pass.
