# Wayland / X server troubleshooting

If you're running Raspberry Pi OS Trixie (Debian 13) or newer, the desktop is **Wayland**, not X11. Playwright's bundled Chromium is built for X11 and fails to start with:

```
Looks like you launched a headed browser without having a XServer running.
ERROR:ui/ozone/platform/x11/ozone_platform_x11.cc:256] Missing X server or $DISPLAY
```

This only matters for the **interactive login** (`--login` mode). Headless daily runs don't need any display server.

## Diagnosis

In a terminal *inside the Pi desktop session* (via Pi Connect screen sharing, VNC, or a directly-attached monitor):

```bash
echo "DISPLAY=$DISPLAY"                    # may show :0 even on Wayland
echo "XDG_SESSION_TYPE=$XDG_SESSION_TYPE"  # "wayland" confirms diagnosis
echo "WAYLAND_DISPLAY=$WAYLAND_DISPLAY"    # "wayland-0" if Wayland is running
ps aux | grep -E "Xorg|Xwayland|labwc" | grep -v grep
```

If `XDG_SESSION_TYPE=wayland` and there's no `Xwayland` process, you need the X11 compatibility layer.

## Fix

```bash
sudo apt install -y xwayland
```

Test whether the X server is now reachable:

```bash
xset q >/dev/null 2>&1 && echo "X is reachable" || echo "X NOT reachable"
```

If "reachable" but Chromium still complains, the X auth cookie isn't being shared with the subprocess. Add yourself to the access list:

```bash
xhost +SI:localuser:$(whoami)
```

Then retry:

```bash
cd ~/amazon-returns
source venv/bin/activate
python scripts/amazon_returns_pi.py --login
```

## Notes

- `xhost` permission resets at reboot. You'll need to run it again before the next `--login` session, but the saved cookies persist, so re-logins are infrequent (once every 30+ days when Amazon expires the session).
- This is **only required for `--login`**. Headless daily runs work over plain SSH with no display server, no `xhost`, nothing.
- If you'd rather skip the GUI on the Pi entirely, do the login on a Mac or Linux desktop machine and copy `amazon_session/` to the Pi via `scp`. Sessions are usually portable.
