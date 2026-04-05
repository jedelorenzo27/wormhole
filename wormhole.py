#!/usr/bin/env python3
"""
wormhole — Fix broken auto-play on Roku streaming apps.

Monitors your Roku's playback state via the local ECP API. When an
episode or movie ends, it presses Select to start the next one.
Works with any streaming app (Apple TV+, etc.) that lacks auto-play.

No blind timers — reacts to actual playback state changes.
No API keys, no cloud, no dependencies beyond Python 3.6+.

Modes:
  - GUI:    Double-click or run with no arguments
  - CLI:    Run with subcommands (scan, test, run)
  - Docker: Headless CLI mode

See README.md for setup and usage.
"""

import argparse
import urllib.request
import urllib.error
import socket
import time
import sys
import threading
import xml.etree.ElementTree as ET
from datetime import datetime

__version__ = "1.0.0"

# ── DEFAULT SETTINGS (override via CLI flags or GUI) ─────────────────
DEFAULT_POLL_INTERVAL = 5
DEFAULT_RETRY_INTERVAL = 30
DEFAULT_RETRY_DURATION = 300
DEFAULT_MAX_EPISODES = 0        # 0 = unlimited
# ─────────────────────────────────────────────────────────────────────


def log(msg):
    """Print a timestamped log message."""
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"[{ts}] {msg}")


# ── ROKU ECP COMMUNICATION ──────────────────────────────────────────

def discover_roku():
    """Find Roku devices on the local network using SSDP multicast."""
    msg = (
        'M-SEARCH * HTTP/1.1\r\n'
        'Host: 239.255.255.250:1900\r\n'
        'Man: "ssdp:discover"\r\n'
        'ST: roku:ecp\r\n'
        'MX: 3\r\n'
        '\r\n'
    )
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(5)
    sock.sendto(msg.encode(), ('239.255.255.250', 1900))

    devices = []
    try:
        while True:
            data, addr = sock.recvfrom(1024)
            response = data.decode()
            for line in response.split('\r\n'):
                if line.upper().startswith('LOCATION:'):
                    url = line.split(':', 1)[1].strip()
                    ip = url.replace('http://', '').replace('https://', '').split(':')[0]
                    if ip not in devices:
                        devices.append(ip)
    except socket.timeout:
        pass
    sock.close()
    return devices


def ecp_get(ip, path):
    """GET request to a Roku ECP endpoint. Returns XML string or None."""
    try:
        url = f"http://{ip}:8060/{path}"
        req = urllib.request.Request(url)
        response = urllib.request.urlopen(req, timeout=5)
        return response.read().decode()
    except Exception:
        return None


def send_keypress(ip, key):
    """Send a keypress to the Roku via ECP."""
    url = f"http://{ip}:8060/keypress/{key}"
    req = urllib.request.Request(url, method='POST', data=b'')
    try:
        urllib.request.urlopen(req, timeout=5)
        return True
    except urllib.error.URLError:
        return False


# ── ROKU STATE QUERIES ───────────────────────────────────────────────

def get_device_info(ip):
    """Get Roku device information as a dict."""
    body = ecp_get(ip, "query/device-info")
    if not body:
        return None

    info = {}
    fields = {
        'user-device-name': 'Device Name',
        'friendly-device-name': 'Friendly Name',
        'default-device-name': 'Default Name',
        'model-name': 'Model',
        'model-number': 'Model Number',
        'serial-number': 'Serial',
        'software-version': 'Software',
        'wifi-mac': 'MAC Address',
        'network-name': 'Network (SSID)',
        'network-type': 'Network Type',
        'power-mode': 'Power',
        'user-device-location': 'Location',
    }

    try:
        root = ET.fromstring(body)
        for tag, label in fields.items():
            el = root.find(tag)
            if el is not None and el.text and el.text.strip():
                info[label] = el.text.strip()
    except ET.ParseError:
        pass

    return info


def get_device_name(info):
    """Extract the best display name from device info."""
    if not info:
        return "Roku"
    return (info.get('Device Name')
            or info.get('Friendly Name')
            or info.get('Default Name')
            or 'Roku')


def get_active_app(ip):
    """Get the currently active app name and ID."""
    body = ecp_get(ip, "query/active-app")
    if not body:
        return None, None
    try:
        root = ET.fromstring(body)
        app_el = root.find('app')
        if app_el is not None:
            name = app_el.text.strip() if app_el.text else "Unknown"
            return name, app_el.get('id', '?')
    except ET.ParseError:
        pass
    return None, None


def get_media_player_status(ip):
    """Get media player state, position, and duration."""
    body = ecp_get(ip, "query/media-player")
    if not body:
        return None
    try:
        root = ET.fromstring(body)
        status = {
            'state': root.get('state', 'unknown'),
            'error': root.get('error', 'false'),
        }

        pos_el = root.find('position')
        if pos_el is not None and pos_el.text:
            ms = int(pos_el.text.strip().replace(' ms', ''))
            mins, secs = divmod(ms // 1000, 60)
            status['position'] = f"{mins:02d}:{secs:02d}"
            status['position_ms'] = ms

        dur_el = root.find('duration')
        if dur_el is not None and dur_el.text:
            ms = int(dur_el.text.strip().replace(' ms', ''))
            mins, secs = divmod(ms // 1000, 60)
            status['duration'] = f"{mins:02d}:{secs:02d}"
            status['duration_ms'] = ms

        is_live_el = root.find('is_live')
        if is_live_el is not None:
            status['is_live'] = is_live_el.text

        return status
    except (ET.ParseError, ValueError):
        return None


def format_status(app_name, media):
    """One-line status string from app + media data."""
    parts = []
    if app_name:
        parts.append(f"App: {app_name}")

    if media:
        state = media.get('state', '?')
        pos = media.get('position', '??:??')
        dur = media.get('duration', '??:??')
        if state == 'play':
            parts.append(f"Playing {pos} / {dur}")
        elif state == 'pause':
            parts.append(f"Paused  {pos} / {dur}")
        elif state in ('stop', 'none', 'close'):
            parts.append("Stopped")
        else:
            parts.append(f"State: {state}")
    else:
        parts.append("No media info")

    return " | ".join(parts)


# ── CORE AUTO-PLAY ENGINE ────────────────────────────────────────────

class WormholeEngine:
    """
    Core auto-play engine. Used by both GUI and CLI.

    The engine runs in its own thread and communicates state via
    callbacks so the GUI can update without blocking.
    """

    def __init__(self, ip, max_episodes=0, poll_interval=5,
                 retry_interval=30, retry_duration=300,
                 on_status=None, on_log=None):
        self.ip = ip
        self.max_episodes = max_episodes
        self.poll_interval = poll_interval
        self.retry_interval = retry_interval
        self.retry_duration = retry_duration
        self.on_status = on_status or (lambda *a: None)
        self.on_log = on_log or log
        self._stop_event = threading.Event()
        self._thread = None
        self.target_app_id = None
        self.episode = 0

    def start(self):
        """Start the engine in a background thread."""
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self):
        """Signal the engine to stop."""
        self._stop_event.set()

    def is_running(self):
        """Check if the engine is currently running."""
        return self._thread is not None and self._thread.is_alive()

    def _log(self, msg):
        self.on_log(msg)

    def _update_status(self, state, app_name, media):
        self.on_status(state, app_name, media, self.episode)

    def _should_stop(self):
        return self._stop_event.is_set()

    def _run(self):
        """Main auto-play loop."""
        ip = self.ip
        self._log(f"Wormhole started — monitoring {ip}")

        # Detect target app
        app_name, app_id = get_active_app(ip)
        media = get_media_player_status(ip)

        if media and media.get('state') == 'play':
            self.target_app_id = app_id
            self._log(f"Locked onto: {app_name} (ID: {self.target_app_id})")
        else:
            self._log("Waiting for playback to start...")

        try:
            self.episode = 0
            while not self._should_stop():
                self.episode += 1

                if self.max_episodes > 0 and self.episode > self.max_episodes:
                    break

                # Wait for playback
                if not self._wait_for_playback():
                    return

                # Watch until content ends
                if not self._watch_until_stopped():
                    return

                # Select next
                next_ep = self.episode + 1
                if self.max_episodes > 0 and next_ep > self.max_episodes:
                    break

                if not self._select_next():
                    self._log("Could not start next — waiting...")
                    if not self._wait_for_playback():
                        return

            self._log("Wormhole stopped.")
            self._update_status("stopped", None, None)

        except Exception as e:
            self._log(f"Error: {e}")
            self._update_status("error", None, None)

    def _wait_for_playback(self):
        """Block until playback starts. Returns False if stopped."""
        while not self._should_stop():
            app_name, app_id = get_active_app(self.ip)
            media = get_media_player_status(self.ip)
            self._update_status("waiting", app_name, media)

            if media and media.get('state') == 'play':
                if self.target_app_id is None or app_id == self.target_app_id:
                    self.target_app_id = app_id
                    self._log(f"Playback detected | {format_status(app_name, media)}")
                    return True

            time.sleep(self.poll_interval)
        return False

    def _watch_until_stopped(self):
        """Monitor playback until content ends. Returns False if stopped."""
        ep_label = f"#{self.episode}"
        self._log(f"Watching {ep_label}")

        last_state = 'play'
        was_playing = True
        last_log_time = 0

        while not self._should_stop():
            now = time.time()
            app_name, app_id = get_active_app(self.ip)
            media = get_media_player_status(self.ip)
            state = media.get('state', 'unknown') if media else 'unknown'

            # User left target app
            if self.target_app_id and app_id != self.target_app_id:
                self._log(f"  App changed to {app_name} — pausing...")
                self._update_status("app_changed", app_name, media)
                while not self._should_stop():
                    time.sleep(self.poll_interval)
                    app_name, app_id = get_active_app(self.ip)
                    if app_id == self.target_app_id:
                        self._log("  Target app is back — resuming")
                        break
                if self._should_stop():
                    return False
                continue

            self._update_status("watching", app_name, media)

            if state == 'play':
                was_playing = True
                if now - last_log_time >= self.poll_interval * 2:
                    self._log(f"  {format_status(app_name, media)}")
                    last_log_time = now

            elif state == 'pause':
                if last_state != 'pause':
                    self._log(f"  {format_status(app_name, media)}")

            elif state in ('stop', 'none', 'close'):
                if was_playing:
                    self._log("  Playback stopped — content ended")
                    return True

            last_state = state
            time.sleep(self.poll_interval)

        return False

    def _select_next(self):
        """Press Select until next content starts."""
        max_retries = self.retry_duration // self.retry_interval
        self._log(f"Pressing SELECT for next episode...")

        for attempt in range(1, max_retries + 1):
            if self._should_stop():
                return False

            app_name, app_id = get_active_app(self.ip)
            if self.target_app_id and app_id != self.target_app_id:
                self._log("  Left target app — aborting")
                return False

            send_keypress(self.ip, "Select")
            media = get_media_player_status(self.ip)
            self._log(f"  SELECT #{attempt}/{max_retries} | {format_status(app_name, media)}")
            self._update_status("retrying", app_name, media)

            time.sleep(self.retry_interval)

            media = get_media_player_status(self.ip)
            if media and media.get('state') == 'play':
                self._log("  Playback started!")
                return True

        self._log(f"  Gave up after {max_retries} attempts")
        return False


# ── GUI ──────────────────────────────────────────────────────────────

def run_gui():
    """Launch the tkinter GUI."""
    import tkinter as tk
    from tkinter import ttk

    engine = None
    discovered_devices = {}  # display_name -> ip

    # ── Window ──
    root = tk.Tk()
    root.title(f"Wormhole v{__version__}")
    root.configure(bg="#1a1a2e")
    root.minsize(680, 420)

    # Remove default tkinter icon
    try:
        root.iconbitmap(default='')
    except Exception:
        pass

    window_width = 680
    window_height = 420
    screen_w = root.winfo_screenwidth()
    screen_h = root.winfo_screenheight()
    x = (screen_w - window_width) // 2
    y = (screen_h - window_height) // 2
    root.geometry(f"{window_width}x{window_height}+{x}+{y}")

    # ── Styles ──
    BG = "#1a1a2e"
    FG = "#e0e0e0"
    ACCENT = "#e94560"
    ACCENT_HOVER = "#ff6b81"
    CARD_BG = "#16213e"
    ENTRY_BG = "#0f3460"
    MUTED = "#8a8a9a"

    style = ttk.Style()
    style.theme_use('clam')
    style.configure("Card.TFrame", background=CARD_BG)
    style.configure("App.TLabel", background=BG, foreground=FG, font=("Segoe UI", 10))
    style.configure("Title.TLabel", background=BG, foreground=FG, font=("Segoe UI", 16, "bold"))
    style.configure("Sub.TLabel", background=BG, foreground=MUTED, font=("Segoe UI", 8))
    style.configure("CardLabel.TLabel", background=CARD_BG, foreground=FG, font=("Segoe UI", 10))
    style.configure("CardMuted.TLabel", background=CARD_BG, foreground=MUTED, font=("Segoe UI", 9))
    style.configure("Status.TLabel", background=CARD_BG, foreground=ACCENT, font=("Segoe UI", 11, "bold"))

    # ── Header ──
    header_frame = tk.Frame(root, bg=BG)
    header_frame.pack(fill="x", padx=20, pady=(18, 2))

    ttk.Label(header_frame, text="Wormhole", style="Title.TLabel").pack(anchor="w")
    ttk.Label(header_frame, text="A worm in Apple's apple.", style="Sub.TLabel").pack(anchor="w")

    # ── Device Selection Card ──
    device_frame = tk.Frame(root, bg=CARD_BG, highlightbackground="#2a2a4a",
                            highlightthickness=1, bd=0)
    device_frame.pack(fill="x", padx=20, pady=(14, 0))

    device_inner = tk.Frame(device_frame, bg=CARD_BG)
    device_inner.pack(fill="x", padx=14, pady=12)

    ttk.Label(device_inner, text="Roku Device", style="CardLabel.TLabel").pack(anchor="w")

    device_row = tk.Frame(device_inner, bg=CARD_BG)
    device_row.pack(fill="x", pady=(6, 0))

    device_var = tk.StringVar(value="-- Click Scan --")
    device_dropdown = ttk.Combobox(device_row, textvariable=device_var,
                                   state="readonly", width=30)
    device_dropdown.pack(side="left", fill="x", expand=True)

    def do_scan():
        device_dropdown['values'] = []
        device_var.set("Scanning...")
        root.update()
        discovered_devices.clear()
        devices = discover_roku()
        if not devices:
            device_var.set("No devices found")
            return
        for ip in devices:
            info = get_device_info(ip)
            name = get_device_name(info)
            label = f"{name}  ({ip})"
            discovered_devices[label] = ip
        device_dropdown['values'] = list(discovered_devices.keys())
        first = list(discovered_devices.keys())[0]
        device_var.set(first)
        update_device_info()

    scan_btn = tk.Button(device_row, text="Scan", bg=ENTRY_BG, fg=FG,
                         activebackground=ACCENT, activeforeground="white",
                         relief="flat", padx=10, pady=2, cursor="hand2",
                         command=do_scan)
    scan_btn.pack(side="left", padx=(8, 0))

    # ── Device Info ──
    info_label = ttk.Label(device_inner, text="", style="CardMuted.TLabel")
    info_label.pack(anchor="w", pady=(6, 0))

    def update_device_info(*args):
        selected = device_var.get()
        ip = discovered_devices.get(selected)
        if ip:
            info = get_device_info(ip)
            if info:
                model = info.get('Model', '?')
                network = info.get('Network (SSID)', '?')
                info_label.config(text=f"Model: {model}  |  Network: {network}")
            else:
                info_label.config(text="Could not fetch device info")

    device_var.trace_add("write", update_device_info)

    # ── Settings Card ──
    settings_frame = tk.Frame(root, bg=CARD_BG, highlightbackground="#2a2a4a",
                              highlightthickness=1, bd=0)
    settings_frame.pack(fill="x", padx=20, pady=(10, 0))

    settings_inner = tk.Frame(settings_frame, bg=CARD_BG)
    settings_inner.pack(fill="x", padx=14, pady=12)

    ttk.Label(settings_inner, text="Settings", style="CardLabel.TLabel").pack(anchor="w")

    settings_grid = tk.Frame(settings_inner, bg=CARD_BG)
    settings_grid.pack(fill="x", pady=(6, 0))

    ttk.Label(settings_grid, text="Episode limit (0 = unlimited):",
              style="CardMuted.TLabel").grid(row=0, column=0, sticky="w", pady=2)
    episodes_var = tk.StringVar(value="0")
    episodes_entry = tk.Entry(settings_grid, textvariable=episodes_var, width=8,
                              bg=ENTRY_BG, fg=FG, insertbackground=FG, relief="flat")
    episodes_entry.grid(row=0, column=1, sticky="e", padx=(10, 0), pady=2)

    settings_grid.columnconfigure(0, weight=1)

    # ── Status Card ──
    status_frame = tk.Frame(root, bg=CARD_BG, highlightbackground="#2a2a4a",
                            highlightthickness=1, bd=0)
    status_frame.pack(fill="x", padx=20, pady=(10, 0))

    status_inner = tk.Frame(status_frame, bg=CARD_BG)
    status_inner.pack(fill="x", padx=14, pady=12)

    ttk.Label(status_inner, text="Status", style="CardLabel.TLabel").pack(anchor="w")

    status_label = ttk.Label(status_inner, text="Idle", style="Status.TLabel")
    status_label.pack(anchor="w", pady=(4, 0))

    playback_label = ttk.Label(status_inner, text="", style="CardMuted.TLabel")
    playback_label.pack(anchor="w", pady=(2, 0))

    episode_label = ttk.Label(status_inner, text="", style="CardMuted.TLabel")
    episode_label.pack(anchor="w", pady=(2, 0))

    # ── Log area ──
    log_frame = tk.Frame(root, bg=CARD_BG, highlightbackground="#2a2a4a",
                         highlightthickness=1, bd=0)
    log_frame.pack(fill="both", expand=True, padx=20, pady=(10, 0))

    log_scroll = tk.Scrollbar(log_frame)
    log_scroll.pack(side="right", fill="y")

    log_text = tk.Text(log_frame, height=8, bg=CARD_BG, fg=MUTED,
                       font=("Consolas", 8), relief="flat", wrap="word",
                       state="disabled", borderwidth=0, padx=10, pady=8,
                       yscrollcommand=log_scroll.set)
    log_text.pack(fill="both", expand=True)
    log_scroll.config(command=log_text.yview)

    def gui_log(msg):
        ts = datetime.now().strftime("%H:%M:%S")
        full = f"[{ts}] {msg}\n"
        log_text.config(state="normal")
        log_text.insert("end", full)
        log_text.see("end")
        log_text.config(state="disabled")

    def gui_status(state, app_name, media, episode_num):
        status_map = {
            "waiting": "Waiting for playback...",
            "watching": "Watching",
            "retrying": "Selecting next...",
            "app_changed": "App changed — paused",
            "stopped": "Stopped",
            "error": "Error",
        }
        status_label.config(text=status_map.get(state, state))

        if media:
            playback_label.config(text=format_status(app_name, media))
        else:
            playback_label.config(text="")

        if episode_num > 0:
            episode_label.config(text=f"Episode #{episode_num}")
        else:
            episode_label.config(text="")

    # ── Start / Stop Button ──
    btn_frame = tk.Frame(root, bg=BG)
    btn_frame.pack(fill="x", padx=20, pady=(12, 18))

    def toggle():
        nonlocal engine
        if engine and engine.is_running():
            engine.stop()
            start_btn.config(text="Start", bg=ACCENT)
            status_label.config(text="Stopped")
            gui_log("Stopped by user.")
        else:
            selected = device_var.get()
            ip = discovered_devices.get(selected)
            if not ip:
                gui_log("No device selected. Click Scan first.")
                return
            try:
                max_ep = int(episodes_var.get())
            except ValueError:
                max_ep = 0

            engine = WormholeEngine(
                ip=ip,
                max_episodes=max_ep,
                poll_interval=DEFAULT_POLL_INTERVAL,
                retry_interval=DEFAULT_RETRY_INTERVAL,
                retry_duration=DEFAULT_RETRY_DURATION,
                on_status=lambda *a: root.after(0, gui_status, *a),
                on_log=lambda m: root.after(0, gui_log, m),
            )
            engine.start()
            start_btn.config(text="Stop", bg="#444")
            gui_log(f"Started — monitoring {ip}")

    start_btn = tk.Button(btn_frame, text="Start", bg=ACCENT, fg="white",
                          activebackground=ACCENT_HOVER, activeforeground="white",
                          relief="flat", font=("Segoe UI", 12, "bold"),
                          cursor="hand2", pady=6, command=toggle)
    start_btn.pack(fill="x")

    # ── Cleanup on close ──
    def on_close():
        if engine and engine.is_running():
            engine.stop()
        root.destroy()

    root.protocol("WM_DELETE_WINDOW", on_close)
    root.mainloop()


# ── CLI COMMANDS ─────────────────────────────────────────────────────

def cmd_scan():
    """Discover and display all Roku devices on the network."""
    log("Searching for Roku devices on your network...")
    devices = discover_roku()
    if not devices:
        log("No Roku devices found on your network.")
        return

    log(f"Found {len(devices)} Roku device(s):\n")
    for ip in devices:
        info = get_device_info(ip)
        name = get_device_name(info)
        model = info.get('Model', '?') if info else '?'
        network = info.get('Network (SSID)', '?') if info else '?'
        app_name, app_id = get_active_app(ip)
        print(f"  {ip}")
        print(f"    Name:    {name}")
        print(f"    Model:   {model}")
        print(f"    Network: {network}")
        print(f"    Active:  {app_name or 'N/A'} (ID: {app_id or 'N/A'})")
        print()


def cmd_test(ip):
    """Test connection to a specific Roku."""
    log(f"Testing connection to {ip}...")
    print()

    info = get_device_info(ip)
    if info:
        log("Device Info:")
        for label, value in info.items():
            print(f"    {label}: {value}")
    else:
        log("Could not get device info — is the IP correct?")
        return

    print()

    app_name, app_id = get_active_app(ip)
    if app_name:
        log(f"Active App: {app_name} (ID: {app_id})")
    else:
        log("Could not get active app")

    media = get_media_player_status(ip)
    if media:
        log(f"Media Player: {format_status(app_name, media)}")
    else:
        log("No media player info (nothing playing?)")

    print()
    log("Sending test SELECT keypress...")
    if send_keypress(ip, "Select"):
        log("Keypress sent! Check your TV screen.")
    else:
        log("Keypress failed. Check network settings.")


def cmd_run(ip, max_episodes, poll_interval, retry_interval, retry_duration):
    """Main auto-play loop (CLI mode)."""
    info = get_device_info(ip)
    name = get_device_name(info)
    if info:
        log(f"Connected to: {name} ({ip})")
        model = info.get('Model', '?')
        network = info.get('Network (SSID)', '?')
        log(f"  Model: {model} | Network: {network}")
    else:
        log(f"Roku at {ip} — could not fetch details, continuing anyway")

    print()
    ep_limit = "unlimited" if max_episodes == 0 else str(max_episodes)
    log(f"wormhole v{__version__}")
    log(f"  Poll interval:   every {poll_interval}s")
    log(f"  Retry strategy:  SELECT every {retry_interval}s for up to {retry_duration // 60}m")
    log(f"  Episode limit:   {ep_limit}")
    print()
    log("Press Ctrl+C to stop.\n")

    engine = WormholeEngine(
        ip=ip,
        max_episodes=max_episodes,
        poll_interval=poll_interval,
        retry_interval=retry_interval,
        retry_duration=retry_duration,
    )

    try:
        engine._run()  # Run in main thread for CLI
    except KeyboardInterrupt:
        print()
        log("Stopped. Enjoy your show!")


# ── CLI ENTRY POINT ──────────────────────────────────────────────────

def resolve_ip(args_ip):
    """Resolve the Roku IP from args or auto-discovery."""
    if args_ip:
        return args_ip

    log("Searching for Roku devices on your network...")
    devices = discover_roku()
    if not devices:
        log("No Roku devices found on your network.")
        log("Use --ip to specify your Roku's IP address.")
        log("Find it on your Roku: Settings > Network > About")
        sys.exit(1)

    if len(devices) == 1:
        log(f"Found Roku at {devices[0]}")
        return devices[0]

    log(f"Found {len(devices)} Roku devices:")
    for i, dev_ip in enumerate(devices, 1):
        info = get_device_info(dev_ip)
        name = get_device_name(info)
        print(f"  [{i}] {dev_ip}  --  {name}")

    print()
    log("Multiple Roku devices found. Use --ip to specify which one.")
    log("Run 'wormhole scan' to see details for all devices.")
    sys.exit(1)


def has_cli_args():
    """Check if the user passed any CLI arguments."""
    # If run with subcommands or flags, it's CLI mode
    cli_indicators = ['scan', 'test', 'run', '--ip', '--help', '-h',
                      '--version', '--episodes', '--poll', '--retry-interval',
                      '--retry-duration', '--test', '--headless']
    return any(arg in sys.argv[1:] for arg in cli_indicators)


def main():
    # If no CLI args, launch GUI
    if not has_cli_args():
        try:
            run_gui()
        except ImportError:
            # tkinter not available (headless / Docker)
            log("GUI not available (tkinter not found). Use CLI mode.")
            log("Run: python wormhole.py run --ip <ROKU_IP>")
            log("Run: python wormhole.py --help")
            sys.exit(1)
        return

    # CLI mode
    parser = argparse.ArgumentParser(
        prog='wormhole',
        description='Fix broken auto-play on Roku streaming apps.',
        epilog='https://github.com/jedelorenzo27/wormhole',
    )
    parser.add_argument('--version', action='version', version=f'%(prog)s {__version__}')

    subparsers = parser.add_subparsers(dest='command')

    subparsers.add_parser('scan', help='Discover all Roku devices on your network')

    test_parser = subparsers.add_parser('test', help='Test connection to your Roku')
    test_parser.add_argument('--ip', help='Roku IP address')

    run_parser = subparsers.add_parser('run', help='Start auto-play (default command)')
    run_parser.add_argument('--ip', help='Roku IP address')
    run_parser.add_argument('--episodes', type=int, default=DEFAULT_MAX_EPISODES,
                            help='Max episodes before stopping (0 = unlimited, default: 0)')
    run_parser.add_argument('--poll', type=int, default=DEFAULT_POLL_INTERVAL,
                            help=f'Seconds between status checks (default: {DEFAULT_POLL_INTERVAL})')
    run_parser.add_argument('--retry-interval', type=int, default=DEFAULT_RETRY_INTERVAL,
                            help=f'Seconds between Select retries (default: {DEFAULT_RETRY_INTERVAL})')
    run_parser.add_argument('--retry-duration', type=int, default=DEFAULT_RETRY_DURATION,
                            help=f'Max seconds to retry Select (default: {DEFAULT_RETRY_DURATION})')

    args = parser.parse_args()

    # Default to 'run' if no command given but --ip is passed
    if args.command is None:
        if '--test' in sys.argv:
            args.command = 'test'
            args.ip = None
            for i, a in enumerate(sys.argv):
                if a == '--ip' and i + 1 < len(sys.argv):
                    args.ip = sys.argv[i + 1]
        elif '--ip' in sys.argv:
            args.command = 'run'
            args.ip = None
            args.episodes = DEFAULT_MAX_EPISODES
            args.poll = DEFAULT_POLL_INTERVAL
            args.retry_interval = DEFAULT_RETRY_INTERVAL
            args.retry_duration = DEFAULT_RETRY_DURATION
            for i, a in enumerate(sys.argv):
                if a == '--ip' and i + 1 < len(sys.argv):
                    args.ip = sys.argv[i + 1]
        else:
            parser.print_help()
            sys.exit(0)

    if args.command == 'scan':
        cmd_scan()
    elif args.command == 'test':
        ip = resolve_ip(getattr(args, 'ip', None))
        cmd_test(ip)
    elif args.command == 'run':
        ip = resolve_ip(args.ip)
        cmd_run(ip, args.episodes, args.poll, args.retry_interval, args.retry_duration)


if __name__ == "__main__":
    main()