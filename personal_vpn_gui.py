"""Personal VPN - a clean dark Flet GUI that controls the official WireGuard tools.

Run:  pip install "flet>=1.0,<2"   then   python personal_vpn_gui.py
Needs: WireGuard installed (Windows: WireGuard for Windows, run as Administrator;
       Linux: wireguard-tools, pkexec/PolicyKit or run as root).
Use the client.conf made by setup_wg_server.sh (or any trusted provider config).
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

import flet as ft

def resource_path(name: str) -> str:
    """Find a bundled file, both when run from source and inside the .exe."""
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    return str(base / name)


# ---------- Backend (talks to the official WireGuard tools) ----------

IS_WIN = sys.platform.startswith("win")
APP_DIR = Path.home() / ".personal_vpn"
PROFILE_DIR = APP_DIR / "profiles"
WIN_WG = r"C:\Program Files\WireGuard\wireguard.exe"
NAME_RE = re.compile(r"^[A-Za-z0-9_=+.-]{1,15}$")
FORBIDDEN = ("preup", "postup", "predown", "postdown")


def validate_conf(text: str) -> str | None:
    """Return an error message, or None if the config looks safe to import."""
    low = text.lower()
    if "[interface]" not in low or "[peer]" not in low:
        return "This is not a WireGuard config (needs [Interface] and [Peer])."
    for line in text.splitlines():
        key = line.split("=", 1)[0].strip().lower()
        if key in FORBIDDEN:
            return f"Config contains '{key}', which runs commands as admin. Rejected."
    if "vpn.example.com" in low or "<redacted>" in low:
        return "Config still has a placeholder (Endpoint/PrivateKey). Fix it first."
    return None


def import_profile(src: str, name: str) -> str | None:
    name = name.strip()
    if not NAME_RE.match(name):
        return "Name: 1-15 characters, letters/numbers/_ = + . - only."
    path = Path(src.strip().strip('"'))
    if not path.is_file():
        return "File not found. Paste the full path to the .conf file."
    err = validate_conf(path.read_text(encoding="utf-8", errors="ignore"))
    if err:
        return err
    PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    dest = PROFILE_DIR / f"{name}.conf"
    shutil.copyfile(path, dest)
    if not IS_WIN:
        os.chmod(dest, 0o600)
    return None


def list_profiles() -> list[str]:
    if not PROFILE_DIR.is_dir():
        return []
    return sorted(p.stem for p in PROFILE_DIR.glob("*.conf"))


def endpoint_of(name: str) -> str:
    try:
        for line in (PROFILE_DIR / f"{name}.conf").read_text().splitlines():
            if line.strip().lower().startswith("endpoint"):
                return line.split("=", 1)[1].strip()
    except OSError:
        pass
    return "unknown"


def remove_profile(name: str) -> None:
    try:
        (PROFILE_DIR / f"{name}.conf").unlink()
    except OSError:
        pass


def is_up(name: str) -> bool:
    if IS_WIN:
        r = subprocess.run(
            ["netsh", "interface", "show", "interface", f"name={name}"],
            capture_output=True, text=True,
        )
        return r.returncode == 0
    return Path(f"/sys/class/net/{name}").exists()


def _run(cmd: list[str]) -> tuple[bool, str]:
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    except FileNotFoundError:
        return False, f"Tool not found: {cmd[0]}. Install the official WireGuard tools."
    except subprocess.TimeoutExpired:
        return False, "Timed out."
    if r.returncode != 0:
        return False, (r.stderr or r.stdout or "Unknown error").strip()[-300:]
    return True, ""


def connect(name: str) -> tuple[bool, str]:
    conf = str(PROFILE_DIR / f"{name}.conf")
    if IS_WIN:
        ok, msg = _run([WIN_WG, "/installtunnelservice", conf])
    else:
        prefix = ["pkexec"] if (shutil.which("pkexec") and os.geteuid() != 0) else []
        ok, msg = _run(prefix + ["wg-quick", "up", conf])
    if not ok:
        return False, msg
    return (True, "") if is_up(name) else (False, "WireGuard did not report an active interface.")


def disconnect(name: str) -> tuple[bool, str]:
    if IS_WIN:
        return _run([WIN_WG, "/uninstalltunnelservice", name])
    conf = str(PROFILE_DIR / f"{name}.conf")
    prefix = ["pkexec"] if (shutil.which("pkexec") and os.geteuid() != 0) else []
    return _run(prefix + ["wg-quick", "down", conf])


def traffic(name: str) -> tuple[int, int]:
    """(rx_bytes, tx_bytes). Linux only; returns zeros elsewhere."""
    try:
        base = Path(f"/sys/class/net/{name}/statistics")
        return int((base / "rx_bytes").read_text()), int((base / "tx_bytes").read_text())
    except (OSError, ValueError):
        return 0, 0


# ---------- UI ----------

BG = "#0B1020"
CARD = "#141B30"
CARD_SEL = "#1C2748"
ACCENT = "#4F8CFF"
OK = "#2ED47A"
WARN = "#FFB020"
ERR = "#FF5C6C"
MUTED = "#8A94B0"
TEXT = "#EAF0FF"


def fmt_time(sec: int) -> str:
    return f"{sec // 3600:02d}:{sec % 3600 // 60:02d}:{sec % 60:02d}"


def fmt_speed(bps: float) -> str:
    for unit in ("B/s", "KB/s", "MB/s"):
        if bps < 1024 or unit == "MB/s":
            return f"{bps:.0f} {unit}" if unit == "B/s" else f"{bps:.1f} {unit}"
        bps /= 1024
    return "0 B/s"


class App:
    def __init__(self, page: ft.Page) -> None:
        self.page = page
        self.selected: str | None = None
        self.active: str | None = None
        self.state = "idle"  # idle | connecting | connected
        self.started = 0.0

        page.title = "Personal VPN"
        page.bgcolor = BG
        page.padding = 24
        page.theme_mode = ft.ThemeMode.DARK
        try:
            page.window.width = 430
            page.window.height = 800
            page.window.icon = resource_path("logo.ico")
        except Exception:
            pass

        if Path(resource_path("logo.png")).is_file():
            logo = ft.Image(src=resource_path("logo.png"), width=44, height=44)
        else:
            logo = ft.Text("\U0001f6e1", size=32)
        self.header = ft.Row(
            [logo, ft.Text("Personal VPN", size=22, weight=ft.FontWeight.BOLD, color=TEXT)],
            spacing=12,
        )

        # Home widgets
        self.power_icon = ft.Text("\u23fb", size=64, color=TEXT)
        self.power = ft.Container(
            content=ft.Column(
                [self.power_icon], height=180, width=180,
                alignment=ft.MainAxisAlignment.CENTER,
                horizontal_alignment=ft.CrossAxisAlignment.CENTER,
            ),
            width=180, height=180, border_radius=90, bgcolor=ACCENT,
            shadow=ft.BoxShadow(blur_radius=50, spread_radius=4,
                                color=ft.Colors.with_opacity(0.45, ACCENT)),
            animate=400, ink=True, on_click=self.toggle,
        )
        self.status = ft.Text("Disconnected", size=26, weight=ft.FontWeight.BOLD, color=TEXT)
        self.sub = ft.Text("Select a server and tap the button", size=14, color=MUTED)
        self.error = ft.Text("", size=12, color=ERR, selectable=True)
        self.t_time = ft.Text("00:00:00", size=18, weight=ft.FontWeight.W_600, color=TEXT)
        self.t_down = ft.Text("0 B/s", size=18, weight=ft.FontWeight.W_600, color=TEXT)
        self.t_up = ft.Text("0 B/s", size=18, weight=ft.FontWeight.W_600, color=TEXT)
        self.current = ft.Text("No server selected", size=15, color=TEXT)

        self.home = ft.Column(
            [
                ft.Column([self.status, self.sub], spacing=4,
                          horizontal_alignment=ft.CrossAxisAlignment.CENTER),
                self.power,
                self.error,
                self.card(ft.Column([ft.Text("SERVER", size=11, color=MUTED), self.current], spacing=4)),
                ft.Row(
                    [self.stat("Time", self.t_time), self.stat("Download", self.t_down),
                     self.stat("Upload", self.t_up)],
                    spacing=10,
                ),
            ],
            spacing=26, horizontal_alignment=ft.CrossAxisAlignment.CENTER,
        )

        # Servers widgets
        self.list_col = ft.Column(spacing=10)
        self.f_path = ft.TextField(label="Path to .conf file", border_radius=12, bgcolor=CARD)
        self.f_name = ft.TextField(label="Name (max 15 chars)", border_radius=12, bgcolor=CARD)
        self.add_msg = ft.Text("", size=12, color=ERR)
        self.servers = ft.Column(
            [
                ft.Text("Servers", size=24, weight=ft.FontWeight.BOLD, color=TEXT),
                self.list_col,
                ft.Text("ADD SERVER", size=11, color=MUTED),
                self.f_path, self.f_name, self.add_msg,
                self.button("Import", self.add_server, ACCENT),
            ],
            spacing=12, visible=False,
        )

        # Tabs
        self.tab_home = self.tab("Home", lambda e: self.show("home"))
        self.tab_srv = self.tab("Servers", lambda e: self.show("servers"))

        page.add(
            self.header,
            ft.Container(height=6),
            ft.Row([self.tab_home, self.tab_srv], spacing=10),
            ft.Container(height=14),
            ft.Column([self.home, self.servers], scroll=ft.ScrollMode.AUTO, expand=True),
        )
        self.render_servers()
        self.show("home")

    # --- small widget helpers ---
    def card(self, content, bg=CARD):
        return ft.Container(content=content, bgcolor=bg, border_radius=16, padding=16,
                            width=380, animate=250)

    def stat(self, label, value):
        return ft.Container(
            content=ft.Column([ft.Text(label.upper(), size=10, color=MUTED), value], spacing=4),
            bgcolor=CARD, border_radius=16, padding=14, width=121,
        )

    def button(self, label, on_click, color):
        return ft.Container(
            content=ft.Text(label, weight=ft.FontWeight.W_600, color=TEXT),
            bgcolor=color, border_radius=12, padding=14, ink=True, on_click=on_click,
        )

    def tab(self, label, on_click):
        return ft.Container(
            content=ft.Text(label, weight=ft.FontWeight.W_600, color=TEXT),
            border_radius=20, padding=10, ink=True, on_click=on_click, animate=250,
        )

    def show(self, which: str) -> None:
        self.home.visible = which == "home"
        self.servers.visible = which == "servers"
        self.tab_home.bgcolor = ACCENT if which == "home" else CARD
        self.tab_srv.bgcolor = ACCENT if which == "servers" else CARD
        self.page.update()

    # --- servers ---
    def render_servers(self) -> None:
        names = list_profiles()
        if self.selected not in names:
            self.selected = names[0] if names else None
        self.list_col.controls.clear()
        if not names:
            self.list_col.controls.append(ft.Text("No servers yet. Import a .conf below.", color=MUTED))
        for n in names:
            row = ft.Row(
                [
                    ft.Column([ft.Text(n, weight=ft.FontWeight.W_600, color=TEXT),
                               ft.Text(endpoint_of(n), size=12, color=MUTED)], spacing=2, expand=True),
                    ft.Container(content=ft.Text("\u2715", color=ERR), padding=8, ink=True,
                                 on_click=lambda e, n=n: self.delete_server(n)),
                ]
            )
            self.list_col.controls.append(
                ft.Container(content=row, bgcolor=CARD_SEL if n == self.selected else CARD,
                             border_radius=14, padding=14, ink=True, animate=250,
                             on_click=lambda e, n=n: self.select(n))
            )
        self.current.value = self.selected or "No server selected"
        self.page.update()

    def select(self, name: str) -> None:
        if self.state != "idle":
            return
        self.selected = name
        self.render_servers()

    def delete_server(self, name: str) -> None:
        if self.state != "idle":
            self.add_msg.value = "Disconnect before removing a server."
            self.page.update()
            return
        remove_profile(name)
        self.render_servers()

    def add_server(self, e) -> None:
        err = import_profile(self.f_path.value or "", self.f_name.value or "")
        self.add_msg.value = err or ""
        if not err:
            self.f_path.value = ""
            self.f_name.value = ""
        self.render_servers()

    # --- connect / disconnect ---
    def set_ui(self, state: str, error: str = "") -> None:
        self.state = state
        self.error.value = error
        colors = {"idle": (ACCENT, "Disconnected", "Select a server and tap the button"),
                  "connecting": (WARN, "Connecting...", "Please wait"),
                  "connected": (OK, "Connected", "Your traffic goes through the tunnel")}
        color, title, sub = colors[state]
        self.power.bgcolor = color
        self.power.shadow = ft.BoxShadow(blur_radius=50, spread_radius=4,
                                         color=ft.Colors.with_opacity(0.45, color))
        self.status.value, self.sub.value = title, sub
        self.page.update()

    def toggle(self, e) -> None:
        if self.state == "connecting":
            return
        threading.Thread(target=self._work, daemon=True).start()

    def _work(self) -> None:
        if self.state == "connected":
            self.set_ui("connecting")
            ok, msg = disconnect(self.active or "")
            if ok or not is_up(self.active or ""):
                self.active = None
                self.set_ui("idle")
            else:
                self.set_ui("connected", msg)
            return
        if not self.selected:
            self.set_ui("idle", "Import and select a server first.")
            return
        self.set_ui("connecting")
        ok, msg = connect(self.selected)
        if not ok:
            self.set_ui("idle", msg)
            return
        self.active = self.selected
        self.started = time.time()
        self.set_ui("connected")
        self.monitor()

    def monitor(self) -> None:
        name = self.active
        last_rx, last_tx = traffic(name)
        while self.state == "connected" and self.active == name:
            time.sleep(1)
            rx, tx = traffic(name)
            self.t_time.value = fmt_time(int(time.time() - self.started))
            self.t_down.value = fmt_speed(rx - last_rx)
            self.t_up.value = fmt_speed(tx - last_tx)
            last_rx, last_tx = rx, tx
            if not is_up(name):
                self.active = None
                self.set_ui("idle", "The tunnel went down unexpectedly.")
                break
            self.page.update()
        self.t_time.value, self.t_down.value, self.t_up.value = "00:00:00", "0 B/s", "0 B/s"
        self.page.update()


def main(page: ft.Page) -> None:
    App(page)


if __name__ == "__main__":
    ft.run(main)
