# wormhole

*A worm in Apple's apple.*

Fix broken auto-play on Roku streaming apps.

Many streaming apps (Apple TV+, etc.) don't auto-play the next episode on Roku and other third-party devices. You finish an episode, get dumped back to a menu, and have to manually click to start the next one. Wormhole fixes that.

## How It Works

Wormhole monitors your Roku's playback state over your local network using Roku's [External Control Protocol (ECP)]([https://developer.roku.com/docs/developer-program/dev-tools/external-control-api.md]). When it detects that playback has stopped (an episode or movie ended), it sends a `Select` keypress to start the next one — just like pressing OK on your remote.

## Requirements

- **Roku device** (any model — sticks, boxes, TVs)
- Device on the **same network** as your Roku

## Download

**Pre-built binaries (no install needed):** Download for your platform from the [Releases page](https://github.com/jedelorenzo27/wormhole/releases):

| Platform | File |
|----------|------|
| Windows | `wormhole-windows.exe` |
| Mac | `wormhole-macos` |
| Linux | `wormhole-linux` |

Double-click the file and the GUI opens. No Python or Docker needed.

**Or run from source:** Requires [Python 3.6+](https://www.python.org/downloads/) (no extra libraries needed) or [Docker](https://www.docker.com/get-started/).

## Setup (One-Time)

### 1. Enable External Control on Your Roku

On your Roku, go to:

```
Settings > System > Advanced system settings > Control by mobile apps
```

Set it to **Default** or **Permissive**.

### 2. Find Your Roku's IP Address

Wormhole can find it for you automatically (see Usage below), or you can find it manually on your Roku:

```
Settings > Network > About
```

## Usage

### GUI Mode (Easiest)

Double-click `wormhole-windows.exe` (or the Mac/Linux binary, or `wormhole.py` if running from source). A window opens where you can:

1. Click **Scan** to find Roku devices on your network
2. Select your Roku from the dropdown
3. Set an episode limit (optional, 0 = unlimited)
4. Click **Start**

That's it. Wormhole runs in the background and auto-plays the next episode whenever playback stops. Click **Stop** or close the window to quit.

### CLI Mode

For power users, Docker, or headless setups. Start playing a show on your Roku, then run:

```bash
python wormhole.py run --ip 192.168.1.42
```

Press `Ctrl+C` to stop.

If you only have one Roku on your network, you can skip the `--ip` flag and it will auto-discover:

```bash
python wormhole.py run
```

### Test Your Connection

```bash
python wormhole.py test --ip 192.168.1.42
```

Shows device info, active app, playback status, and sends a test keypress.

### Scan for Devices

```bash
python wormhole.py scan
```

### CLI Options

| Flag | Default | Description |
|------|---------|-------------|
| `--ip` | auto-discover | Roku IP address |
| `--episodes` | `0` (unlimited) | Stop after N episodes (0 = run forever) |
| `--poll` | `5` | Seconds between status checks |
| `--retry-interval` | `30` | Seconds between Select retries after content ends |
| `--retry-duration` | `300` | Max seconds to keep retrying before waiting for manual intervention |

**Examples:**

```bash
# Run until you Ctrl+C
python wormhole.py run --ip 192.168.1.42

# Stop after 5 episodes
python wormhole.py run --ip 192.168.1.42 --episodes 5

# Poll more frequently (every 2 seconds)
python wormhole.py run --ip 192.168.1.42 --poll 2
```

## How It Handles Edge Cases

| Scenario | What Happens |
|----------|--------------|
| **Episode ends normally** | Detects state change to `stop`, presses Select |
| **You fast-forward to the end** | Same — detects the stop immediately |
| **You pause** | Waits patiently, resumes monitoring on unpause |
| **You leave the app** | Pauses auto-play, waits for you to return |
| **You switch shows** | Doesn't care — auto-plays the next episode of whatever you're watching |
| **You watch a movie** | Works the same — presses Select when the movie ends |
| **You fall asleep** | Keeps going until episode limit or you stop it |
| **Your computer locks** | No effect — the script keeps running |
| **Select press does nothing** | Retries every 30s for up to 5 minutes |

## Running with Docker

For headless setups or if you don't want to install Python.

### Build

```bash
docker build -t wormhole .
```

### Run

```bash
docker run --rm --network host wormhole run --ip 192.168.1.42
```

`--network host` gives the container direct access to your local network so it can reach the Roku.

### Other commands

```bash
# Scan for devices
docker run --rm --network host wormhole scan

# Test connection
docker run --rm --network host wormhole test --ip 192.168.1.42

# Limit to 10 episodes
docker run --rm --network host wormhole run --ip 192.168.1.42 --episodes 10
```

### Run in the background

```bash
docker run -d --name wormhole --network host wormhole run --ip 192.168.1.42
```

Check logs:

```bash
docker logs -f wormhole
```

Stop it:

```bash
docker stop wormhole
```

### Docker on Mac/Windows

Docker Desktop on macOS and Windows doesn't support true `--network host` mode. The container can still reach your Roku, just skip `--network host` and make sure the Roku's IP is routable from your machine:

```bash
docker run --rm wormhole run --ip 192.168.1.42
```

Auto-discovery (`scan`) won't work without host networking since SSDP multicast doesn't bridge. Just use `--ip` directly.

## Building a Standalone Executable

You can package Wormhole into a single `.exe` (Windows) or binary (Mac/Linux) using PyInstaller. No Python installation needed for end users.

```bash
pip install pyinstaller
pyinstaller --onefile --windowed --icon wormhole.ico --add-data "wormhole.ico;." --name wormhole wormhole.py
```

The executable will be in `dist/wormhole.exe` (or `dist/wormhole` on Mac/Linux). Double-clicking it opens the GUI. Running it from a terminal with arguments uses CLI mode.

## Deployment Options

Wormhole runs anywhere Python or Docker does.

| Platform | Notes |
|----------|-------|
| **Your computer** | Simplest. Double-click for GUI or run from terminal. Stops if your machine sleeps. |
| **Raspberry Pi** | Ideal always-on setup. A $35 Pi Zero W on your network runs it 24/7. |
| **NAS (Synology, etc.)** | Most NAS devices support Docker. Run it as a container. |
| **Old laptop / mini PC** | Anything with Python and Wi-Fi works. |
| **Cloud VM** | Won't work — must be on the same local network as your Roku. |

## FAQ

**Does this work with apps other than Apple TV+?**
Yes. It works with any streaming app on Roku. It monitors playback state at the system level and presses Select when content ends. The app doesn't matter.

**Will it press buttons in the wrong app?**
No. Wormhole locks onto whichever app is active when you start it. If you switch to a different app, it pauses and waits for you to come back.

**Can it tell what show I'm watching?**
No. Roku's ECP doesn't expose content metadata (show title, episode number, etc.). Wormhole only sees: which app is active, whether something is playing/paused/stopped, and the current playback position and duration.

**Will pressing Select during playback cause problems?**
No. During active playback, the Select keypress is ignored by the Roku. It only has an effect when there's a UI element focused on screen (like the next episode tile).

**Does this use the internet?**
No. Everything is local network traffic between your device and the Roku over port 8060. No API keys, no cloud services, no data leaves your network.

**Will my computer going to sleep stop it?**
Yes. Sleep kills the network connection. On macOS, use `caffeinate python wormhole.py run` to prevent sleep. On Windows, adjust your power settings while Wormhole is running. Or run it on a Raspberry Pi / NAS / Docker container that stays on.

**Can I run this on a Raspberry Pi?**
Yes. Any device with Python 3.6+ or Docker on the same network will work. A Pi is a great always-on option. See [Deployment Options](#deployment-options).

## How It Works (Technical)

Wormhole uses three Roku ECP endpoints, all simple HTTP GET/POST requests on port 8060:

| Endpoint | Purpose |
|----------|---------|
| `GET /query/active-app` | Identify which app is in the foreground |
| `GET /query/media-player` | Get playback state (`play`/`pause`/`stop`), position, and duration |
| `POST /keypress/Select` | Simulate pressing the OK/Select button on the remote |

The main loop polls `query/media-player` every few seconds. When the state transitions from `play` to `stop`, it starts pressing `Select` every 30 seconds until playback resumes (indicating the next episode started).

## License

MIT

## Contributing

Issues and PRs welcome. This was built to solve a real problem — if you find edge cases or have ideas, open an issue.
