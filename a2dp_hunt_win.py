r"""
a2dp_hunt_win.py v2.0 - Windows counterpart of a2dp_hunt.py (Linux v2.3)
=========================================================================

Runs many audio-off -> audio-on cycles against the Px7 S2 on Windows,
records everything and stops at the first link loss (or waits for the
headphones to reconnect and continues, see --max-hits). Command line,
cycles.csv and summary.txt follow the Linux script, so results from both
systems can be put side by side.

What Windows does not allow, and what replaces it
-------------------------------------------------
* AVDTP is not reachable from user mode. SUSPEND and START are provoked the
  way players provoke them: audio stops -> Windows sends SUSPEND once the
  buffered audio has drained; audio starts -> Windows sends START. So --gap
  is audio off -> audio on. The on-air SUSPEND -> START gap is shorter by the
  drain time (tens to a few hundred ms); read it from the trace.
* No START / SUSPEND acceptance, no l2ping, no RSSI. A dead link is noticed
  only when Windows reports the disconnect after link supervision timeout,
  ~21 s later. The script then attributes the hit retroactively to the
  transition ~21 s before the report (--detect-delay), marks the cycles that
  ran into the dead link as after_loss and leaves them out of the statistics.
  The attribution is an estimate: confirm it in the trace.
* No "stall" result: a link that is alive with AVDTP stuck is invisible
  without the trace.
* Sniff needs nothing: Windows puts an idle link into sniff by itself
  (10-17 s after SUSPEND in the captures). Gaps of 25 s and more reproduce
  the type B path (Exit_Sniff_Mode -> START).

Modes
-----
wasapi (default)  The script opens and closes a WASAPI stream to the
                  headphones: generated quiet tone (same as on Linux) or an
                  --audio file. AVRCP is script-controlled like the Linux
                  HuntPlayer: the script owns a Windows media session (SMTC)
                  and switches it to Playing / Paused at --avrcp-offset around
                  the START; Windows forwards that as AVRCP
                  PLAYBACK_STATUS_CHANGED.
player            Windows MediaPlayer plays your --files with play/pause
                  (--switch-tracks: next file on every START). Windows decides
                  the AVRCP timing itself, as with a real player.

Buttons and the wear sensor of the headphones send AVRCP commands; in both
modes they are logged and ignored.

Keys: m = you heard the disconnect tone (goes into the report),
      q or Ctrl+C = stop cleanly.

Outputs (one folder per run)
----------------------------
    cycles.csv    one row per cycle (time_on ~ START, time_off ~ SUSPEND)
    summary.txt   results per configuration
    hunt.log      console log
    meta.json     Windows build, Bluetooth driver versions, Wi-Fi, output
    hitN.etl      WPR trace saved after each hit (with --wpr)
    final.etl     WPR trace at the end (with --wpr; memory mode keeps the
                  last minutes - enough to check that SUSPEND/START happen)

Times are local wall-clock time with UTC offset, as in the Linux script and
in Wireshark's "Time of Day" column.

Requirements: Python 3.10+ from python.org, then (one line)
  pip install numpy sounddevice soundfile winrt-runtime winrt-Windows.Foundation winrt-Windows.Devices.Bluetooth winrt-Windows.Media winrt-Windows.Media.Core winrt-Windows.Media.Playback

Before starting: close everything that plays or holds audio (browser,
Teams, Discord, Spotify, games) and set system sounds to "No sounds".
Anything holding the endpoint open keeps the stream alive (no SUSPEND), and
any other media app competes for the media session that AVRCP reports.
For --mode player the headphones must be the default playback device.

Usage (one line each)
-----
  python a2dp_hunt_win.py --list-devices
  python a2dp_hunt_win.py --mac AA:BB:CC:DD:EE:FF --device Px7 --sweep-gap 300,500,1000
  python a2dp_hunt_win.py --mac AA:BB:CC:DD:EE:FF --device Px7 --preset B
  python a2dp_hunt_win.py --mac AA:BB:CC:DD:EE:FF --device Px7 --gap 500 --sweep-avrcp=-200,-2,0,50
  python a2dp_hunt_win.py --mac AA:BB:CC:DD:EE:FF --mode player --files "D:\Music\test" --switch-tracks
  # elevated prompt, with the Bluetooth trace, keep hunting after a hit:
  python a2dp_hunt_win.py --mac AA:BB:CC:DD:EE:FF --device Px7 --preset A --max-hits 3 --wpr "C:\bt\BluetoothStack.wprp!BluetoothStack"

Presets: A = gaps 200-1200 ms (link stays active), B = 25-45 s (link in sniff).
Negative numbers in lists need '=':  --sweep-avrcp=-200,-2,0,50
"""

import argparse
import asyncio
import csv
import ctypes
import glob
import json
import os
import platform
import signal
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

VERSION = "2.0-win"

# last packet from the headphones -> HCI Disconnection Complete was
# 20.90-21.39 s in the captures, plus the ConnectionStatus notification
DETECT_DELAY_S = 21.3

PRESETS = {
    "A": [200, 350, 500, 800, 1200],
    "B": [25000, 33000, 45000],
}

FIELDS = ["time_on", "time_off", "cycle", "gap_ms", "avrcp_offset_ms",
          "play_ms", "actual_gap_ms", "start_ms", "result", "phase", "note"]
NOT_COUNTED = ("after_loss", "stopped")
AUDIO_EXT = {".mp3", ".wav", ".flac", ".m4a", ".aac", ".wma", ".ogg", ".opus"}

PIP_WINRT = ("pip install winrt-runtime winrt-Windows.Foundation "
             "winrt-Windows.Devices.Bluetooth winrt-Windows.Media "
             "winrt-Windows.Media.Core winrt-Windows.Media.Playback")

STOP = threading.Event()
_log_lock = threading.Lock()
_log_file = None


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

def stamp(t=None):
    """Local wall-clock time with UTC offset, millisecond precision."""
    return (datetime.fromtimestamp(time.time() if t is None else t)
            .astimezone().isoformat(timespec="milliseconds"))


def log(msg=""):
    line = f"[{stamp()}] {msg}"
    with _log_lock:
        print(line, flush=True)
        if _log_file:
            _log_file.write(line + "\n")
            _log_file.flush()


def sleep_until(deadline, *events):
    """Sleep until a monotonic deadline. False if interrupted (stop or event)."""
    while True:
        if STOP.is_set() or any(e.is_set() for e in events):
            return False
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return True
        time.sleep(min(remaining, 0.01))


def sleep_ms(ms, *events):
    return sleep_until(time.monotonic() + ms / 1000.0, *events)


def mono_to_wall(t_mono):
    return time.time() - (time.monotonic() - t_mono)


def parse_list(text):
    return [int(x) for x in text.split(",") if x.strip()]


def fmt_off(v):
    return "-" if v in ("", None) else f"{int(v):+d}"


def is_admin():
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def run_text(cmd, timeout):
    return subprocess.run(cmd, capture_output=True, text=True,
                          errors="replace", timeout=timeout)


# --------------------------------------------------------------------------
# link state, keyboard, WPR
# --------------------------------------------------------------------------

class Link(threading.Thread):
    """Polls BluetoothDevice.ConnectionStatus. Windows itself notices a dead
    link only when link supervision timeout expires (~21 s)."""

    def __init__(self, mac):
        super().__init__(daemon=True)
        try:
            from winrt.windows.devices.bluetooth import (
                BluetoothConnectionStatus, BluetoothDevice)
        except ImportError:
            sys.exit(f"missing WinRT packages:\n  {PIP_WINRT}")
        self.CONNECTED = BluetoothConnectionStatus.CONNECTED
        try:
            addr = int(mac.replace(":", "").replace("-", ""), 16)
        except ValueError:
            sys.exit(f"bad --mac: {mac}")

        async def _get():
            return await BluetoothDevice.from_bluetooth_address_async(addr)

        self.dev = asyncio.run(_get())
        if self.dev is None:
            sys.exit(f"{mac} is unknown to Windows - pair the headphones first")
        self.name = str(self.dev.name)
        self.up = threading.Event()
        self.down = threading.Event()
        self.down_at = None                    # monotonic
        self.down_wall = None
        if not self.connected():
            sys.exit(f"{self.name} is not connected - connect the headphones first")
        self.up.set()

    def connected(self):
        try:
            return self.dev.connection_status == self.CONNECTED
        except Exception:
            return False

    def run(self):
        last = True
        while not STOP.is_set():
            c = self.connected()
            if c != last:
                last = c
                if c:
                    self.down.clear()
                    self.up.set()
                    log("link      connected")
                else:
                    self.down_at, self.down_wall = time.monotonic(), time.time()
                    self.up.clear()
                    self.down.set()
                    log("link      DISCONNECTED (reported by Windows now)")
            time.sleep(0.1)


class Keys(threading.Thread):
    def __init__(self, link=None, detect_delay=DETECT_DELAY_S):
        super().__init__(daemon=True)
        self.marks = []                        # (monotonic, wall)
        self.link = link
        self.detect_delay = detect_delay
        self.q_armed = False

    def run(self):
        import msvcrt
        while not STOP.is_set():
            if msvcrt.kbhit():
                ch = msvcrt.getwch().lower()
                if ch in ("m", "\u044c"):      # m, same key on Ukrainian layout
                    self.marks.append((time.monotonic(), time.time()))
                    log(">>> mark: disconnect tone heard")
                elif ch in ("q", "\u0439"):    # q, same key on Ukrainian layout
                    now = time.monotonic()
                    guard = self.detect_delay + 5
                    recent = [t for t, _ in self.marks if now - t < guard]
                    if (recent and self.link is not None and self.link.up.is_set()
                            and not self.q_armed):
                        ago = now - recent[-1]
                        log(f"you marked a disconnect {ago:.0f} s ago; Windows reports it "
                            f"only ~{self.detect_delay:.0f} s after the fact. Wait "
                            f"~{guard - ago:.0f} s and the script stops by itself, "
                            f"or press q again to stop anyway")
                        self.q_armed = True
                    else:
                        log("stop requested")
                        STOP.set()
            time.sleep(0.03)


class Wpr:
    """Bluetooth ETW trace. Saved after every hit, then restarted."""

    def __init__(self, profile, filemode, run_dir):
        self.profile, self.filemode, self.dir = profile, filemode, run_dir
        self.running = False
        if not profile:
            return
        if not is_admin():
            sys.exit("--wpr needs an elevated prompt (Run as administrator)")
        if not self.start():
            wprp = profile.split("!")[0]
            sys.exit("WPR did not start - a hit without a trace is useless, so stopping.\n"
                     "List the profiles in the file:\n"
                     f"  Select-String -Path \"{wprp}\" -Pattern '<Profile '\n"
                     "Id = Name.DetailLevel.LoggingMode. Pass Name after '!', and add\n"
                     "--wpr-filemode if the file only has ...File profiles.\n"
                     "If a trace is already running: wpr -cancel")

    def start(self):
        cmd = ["wpr", "-start", self.profile] + (["-filemode"] if self.filemode else [])
        r = run_text(cmd, 120)
        if r.returncode == 0:
            self.running = True
            log(f"capture   WPR ({'file' if self.filemode else 'memory'} mode)")
        else:
            msg = " ".join((r.stderr or r.stdout).split())
            log(f"WARNING: wpr -start failed: {msg[:300]}")
        return self.running

    def save(self, name, restart):
        if not self.running:
            return None
        path = os.path.join(self.dir, f"{name}.etl")
        log(f"WPR       saving {name}.etl (can take a minute) ...")
        r = run_text(["wpr", "-stop", path], 900)
        self.running = False
        if r.returncode != 0:
            log(f"WARNING: wpr -stop failed: {(r.stderr or r.stdout).strip()[:300]}")
            path = None
        if restart and not STOP.is_set():
            self.start()
        return path


# --------------------------------------------------------------------------
# AVRCP: script-owned media session
# --------------------------------------------------------------------------

class Avrcp:
    """Windows counterpart of the Linux HuntPlayer: a media session (SMTC)
    whose Playing / Paused status the script sets itself. Windows reports the
    status to the headphones as AVRCP PLAYBACK_STATUS_CHANGED. The session
    belongs to a MediaPlayer that never plays anything."""

    def __init__(self):
        from winrt.windows.media import (MediaPlaybackStatus,
                                         MediaPlaybackType)
        from winrt.windows.media.playback import MediaPlayer
        self.PLAYING = MediaPlaybackStatus.PLAYING
        self.PAUSED = MediaPlaybackStatus.PAUSED
        self.player = MediaPlayer()
        self.player.command_manager.is_enabled = False     # manual SMTC
        self.smtc = self.player.system_media_transport_controls
        self.smtc.is_enabled = True
        self.smtc.is_play_enabled = True
        self.smtc.is_pause_enabled = True
        try:
            du = self.smtc.display_updater
            du.type = MediaPlaybackType.MUSIC
            du.music_properties.title = "a2dp-hunt"
            du.update()
        except Exception:
            pass
        self.smtc.playback_status = self.PAUSED
        self.token = self.smtc.add_button_pressed(self._on_button)

    def _on_button(self, sender, e):
        try:
            name = getattr(e.button, "name", str(e.button))
        except Exception:
            name = "?"
        log(f"AVRCP button from headphones: {name} (ignored)")

    def set(self, playing):
        self.smtc.playback_status = self.PLAYING if playing else self.PAUSED

    def close(self):
        try:
            self.smtc.playback_status = self.PAUSED
            self.smtc.remove_button_pressed(self.token)
            self.player.close()
        except Exception:
            pass


# --------------------------------------------------------------------------
# audio
# --------------------------------------------------------------------------

def wasapi_outputs(sd):
    apis = [i for i, a in enumerate(sd.query_hostapis()) if "WASAPI" in a["name"]]
    if not apis:
        sys.exit("PortAudio offers no WASAPI host API")
    return [(i, d["name"]) for i, d in enumerate(sd.query_devices())
            if d["hostapi"] == apis[0] and d["max_output_channels"] > 0]


def find_output(sd, needle, index):
    outs = wasapi_outputs(sd)
    if index is not None:
        return (index if index in [i for i, _ in outs] else None), outs
    if not needle:
        return None, outs
    exact = [i for i, n in outs if n == needle]
    if exact:
        return exact[0], outs
    hits = [(i, n) for i, n in outs
            if needle.lower() in n.lower() and "hands-free" not in n.lower()]
    if len(hits) > 1:
        log("WARNING: several outputs match, using the first: "
            + ", ".join(f"[{i}] {n}" for i, n in hits))
    return (hits[0][0] if hits else None), outs


class WasapiAudio:
    """Stream open -> AVDTP START, stream closed -> AVDTP SUSPEND after drain."""

    def __init__(self, args):
        try:
            import numpy as np
            import sounddevice as sd
        except ImportError:
            sys.exit("pip install numpy sounddevice")
        self.np, self.sd, self.args = np, sd, args
        self.stream = None
        self.dev_name = None
        dev, outs = find_output(sd, args.device, args.device_index)
        if dev is None:
            listing = "\n".join(f"  [{i}] {n}" for i, n in outs)
            sys.exit(f"no WASAPI output for --device/--device-index; outputs:\n{listing}")
        self._use(dev)
        self._build()
        log(f"output    [{self.dev}] {self.dev_name}, {self.sr} Hz, "
            f"{'exclusive' if args.exclusive else 'shared'} mode")
        log(f"audio     {os.path.basename(args.audio) if args.audio else 'generated tone'}, "
            f"volume {args.volume}")

    def _use(self, dev):
        info = self.sd.query_devices(dev)
        self.dev, self.dev_name = dev, info["name"]
        self.sr = int(info["default_samplerate"])
        self.ch = min(2, int(info["max_output_channels"]))

    def _build(self):
        np = self.np
        if self.args.audio:
            buf = self._load(self.args.audio)
        else:
            # same signal as the Linux script: 440 + 660 Hz, about -24 dBFS
            t = np.arange(self.sr) / self.sr
            v = 0.06 * (np.sin(2 * np.pi * 440 * t)
                        + 0.5 * np.sin(2 * np.pi * 660 * t)) / 1.5
            buf = np.repeat(v[:, None], 2, axis=1)
        self.buf = (buf * self.args.volume).astype(np.float32)
        self.buf_sr = self.sr
        self.pos = 0

    def _load(self, path):
        np = self.np
        try:
            import soundfile as sf
        except ImportError:
            sys.exit("--audio needs: pip install soundfile")
        rate = sf.info(path).samplerate
        data, rate = sf.read(path, dtype="float32", always_2d=True,
                             frames=600 * rate)
        if data.shape[1] == 1:
            data = np.repeat(data, 2, axis=1)
        data = data[:, :2]
        if rate != self.sr:
            n = len(data)
            x = np.linspace(0, n - 1, int(round(n * self.sr / rate)))
            data = np.stack([np.interp(x, np.arange(n), data[:, c])
                             for c in range(2)], axis=1)
        return data

    def rescan(self):
        """After a reconnect the endpoint comes back; PortAudio must re-enumerate."""
        deadline = time.monotonic() + 20
        while not STOP.is_set():
            try:
                self.sd._terminate()
                self.sd._initialize()
            except Exception as e:
                log(f"WARNING: PortAudio re-init: {e!r}")
            dev, _ = find_output(self.sd, self.dev_name, None)
            if dev is not None:
                self._use(dev)
                if self.sr != self.buf_sr:
                    self._build()
                log(f"output    [{self.dev}] {self.dev_name} is back")
                return True
            if time.monotonic() > deadline:
                log(f"output '{self.dev_name}' did not come back")
                return False
            time.sleep(2)
        return False

    def start(self):
        self.stream = self.sd.OutputStream(
            device=self.dev, samplerate=self.sr, channels=self.ch,
            dtype="float32",
            extra_settings=self.sd.WasapiSettings(exclusive=self.args.exclusive))
        self.stream.start()
        return ""

    def play(self, ms, *events):
        np = self.np
        n = max(1, int(self.sr * ms / 1000))
        idx = (self.pos + np.arange(n)) % len(self.buf)
        seg = self.buf[idx, :self.ch]
        self.pos = (self.pos + n) % len(self.buf)
        f = min(int(0.02 * self.sr), n // 2)   # click-free start and end
        if f > 0:
            ramp = np.linspace(0, 1, f, dtype=np.float32)[:, None]
            seg[:f] *= ramp
            seg[-f:] *= ramp[::-1]
        chunk = max(1, self.sr // 50)          # 20 ms
        for i in range(0, n, chunk):
            if STOP.is_set() or any(e.is_set() for e in events):
                return False
            self.stream.write(np.ascontiguousarray(seg[i:i + chunk]))
        return True

    def stop(self):
        s, self.stream = self.stream, None
        if s is not None:
            try:
                s.stop()                       # plays out what is queued
            finally:
                s.close()

    def abort(self):
        s, self.stream = self.stream, None
        if s is not None:
            try:
                s.abort()
                s.close()
            except Exception:
                pass

    def close(self):
        self.abort()


def collect_files(items):
    files = []
    for it in items or []:
        p = Path(it).expanduser()
        if p.is_dir():
            files += sorted(f for f in p.iterdir() if f.suffix.lower() in AUDIO_EXT)
        elif p.is_file():
            files.append(p)
        else:
            files += sorted(Path(g) for g in glob.glob(it)
                            if Path(g).suffix.lower() in AUDIO_EXT)
    files = [f.resolve() for f in files]
    if not files:
        sys.exit("no audio files found (--files takes files, folders or wildcards)")
    return files


class PlayerAudio:
    """Windows MediaPlayer; its own media session sends AVRCP automatically."""

    def __init__(self, args):
        try:
            from winrt.windows.foundation import Uri
            from winrt.windows.media.core import MediaSource
            from winrt.windows.media.playback import (MediaPlaybackState,
                                                      MediaPlayer)
        except ImportError:
            sys.exit(f"missing WinRT packages:\n  {PIP_WINRT}")
        self.Uri, self.MediaSource = Uri, MediaSource
        self.PLAYING = MediaPlaybackState.PLAYING
        self.files = collect_files(args.files)
        self.switch = args.switch_tracks
        self.dev_name = "Windows default playback device"
        self.player = MediaPlayer()
        self.player.is_looping_enabled = True
        self.player.volume = args.volume
        self.error = None
        self.player.add_media_failed(self._on_failed)
        cm = self.player.command_manager
        cm.add_play_received(lambda s, e: self._button(e, "Play"))
        cm.add_pause_received(lambda s, e: self._button(e, "Pause"))
        self.idx = -1
        log(f"player    {len(self.files)} file(s), volume {args.volume}, "
            f"{'next file on every START' if self.switch else 'same file'}")
        log("output    Windows default playback device (must be the headphones)")

    def _button(self, e, name):
        try:
            e.handled = True
        except Exception:
            pass
        log(f"AVRCP button from headphones: {name} (ignored)")

    def _on_failed(self, sender, e):
        try:
            self.error = f"{e.error}: {e.error_message}"
        except Exception:
            self.error = "MediaFailed"

    def start(self):
        if self.idx < 0 or self.switch:
            self.idx = (self.idx + 1) % len(self.files)
            uri = self.Uri(self.files[self.idx].as_uri())
            self.player.source = self.MediaSource.create_from_uri(uri)
        self.error = None
        self.player.play()
        deadline = time.monotonic() + 5.0
        while self.player.playback_session.playback_state != self.PLAYING:
            if self.error:
                raise RuntimeError(f"media failed: {self.error}")
            if time.monotonic() > deadline:
                raise TimeoutError("player did not reach Playing within 5 s")
            if STOP.is_set():
                break
            time.sleep(0.005)
        return self.files[self.idx].name

    def play(self, ms, *events):
        return sleep_ms(ms, *events)

    def stop(self):
        self.player.pause()

    def abort(self):
        try:
            self.player.pause()
        except Exception:
            pass

    def rescan(self):
        return True

    def close(self):
        try:
            self.player.pause()
            self.player.close()
        except Exception:
            pass


# --------------------------------------------------------------------------
# the cycle driver
# --------------------------------------------------------------------------

class Hunter:
    def __init__(self, args, link, audio, avrcp, keys, wpr, run_dir):
        self.a, self.link, self.audio, self.avrcp = args, link, audio, avrcp
        self.keys, self.wpr, self.dir = keys, wpr, run_dir
        self.cycle = 0
        self.total = 0
        self.rows = []
        self.hits = []
        self.t_idle = None
        self.since = 0.0                       # monotonic time of the last (re)connect
        self._handled = None
        self.csv_path = os.path.join(run_dir, "cycles.csv")
        self.csv_f = open(self.csv_path, "w", newline="", encoding="utf-8")
        self.writer = csv.DictWriter(self.csv_f, fieldnames=FIELDS, extrasaction="ignore")
        self.writer.writeheader()

    # ---- small actions -----------------------------------------------------

    def _avrcp(self, playing):
        if self.avrcp:
            try:
                self.avrcp.set(playing)
            except Exception as e:
                log(f"WARNING: media session: {e!r}")

    def _end(self, row, phase):
        """Cycle cut short: the link is already down, or the user stopped."""
        self.audio.abort()
        self._avrcp(False)
        row.update(result="after_loss" if self.link.down.is_set() else "stopped",
                   phase=phase)
        self.t_idle = time.monotonic()
        return row

    def _audio_error(self, row, phase, e):
        self.audio.abort()
        self._avrcp(False)
        # Windows removes the endpoint when it drops the link, so an audio
        # error at that moment is a consequence of the loss, not a cause
        sleep_until(time.monotonic() + 1.5, self.link.down)
        if self.link.down.is_set() or STOP.is_set():
            return self._end(row, phase)
        row.update(result="local_error", phase=phase, note=f"{type(e).__name__}: {e}")
        return row

    # ---- one cycle ---------------------------------------------------------

    def one_cycle(self, gap, off):
        """gap -> [AVRCP PLAYING] -> audio on (START) -> play ->
        AVRCP PAUSED -> audio off (SUSPEND after drain)"""
        a, down = self.a, self.link.down
        self.cycle += 1
        row = dict.fromkeys(FIELDS, "")
        row.update(cycle=self.cycle, gap_ms=gap, play_ms=a.play, result="ok",
                   avrcp_offset_ms="" if off is None else off, _on=None, _off=None)

        pre = max(0, -off) if (self.avrcp and off is not None) else 0
        start_at = self.t_idle + gap / 1000.0
        if not sleep_until(max(self.t_idle, start_at - pre / 1000.0), down):
            return self._end(row, "gap")
        if pre:
            self._avrcp(True)
            if not sleep_until(start_at, down):
                return self._end(row, "gap")
        row["actual_gap_ms"] = round((time.monotonic() - self.t_idle) * 1000, 1)

        # ---- audio on -> AVDTP START
        t0 = time.monotonic()
        try:
            detail = self.audio.start()
        except Exception as e:
            return self._audio_error(row, "START", e)
        row["_on"] = time.monotonic()
        row["time_on"] = stamp()
        row["start_ms"] = round((row["_on"] - t0) * 1000, 1)
        if detail:
            row["note"] = detail
        if self.avrcp and off is not None and off >= 0:
            if not sleep_ms(off, down):
                return self._end(row, "START")
            self._avrcp(True)

        # ---- play
        try:
            if not self.audio.play(a.play, down):
                return self._end(row, "PLAY")
        except Exception as e:
            return self._audio_error(row, "PLAY", e)

        # ---- AVRCP PAUSED, audio off -> AVDTP SUSPEND once drained
        self._avrcp(False)
        try:
            self.audio.stop()
        except Exception as e:
            return self._audio_error(row, "SUSPEND", e)
        row["_off"] = time.monotonic()
        row["time_off"] = stamp()
        self.t_idle = row["_off"]
        return row

    # ---- bookkeeping -------------------------------------------------------

    def _record(self, row):
        self.rows.append(row)
        self.writer.writerow(row)
        self.csv_f.flush()
        av = f" avrcp {fmt_off(row['avrcp_offset_ms'])}" if row["avrcp_offset_ms"] != "" else ""
        note = f"  ({row['note']})" if row["result"] != "ok" and row["note"] else ""
        log(f"#{row['cycle']:>4}/{self.total} gap {row['gap_ms']}{av} | "
            f"on {row['start_ms'] or '-'} ms | actual gap {row['actual_gap_ms'] or '-'} ms"
            f" | {row['result']}{note}")

    def rewrite_csv(self):
        """Hits are attributed retroactively, so the file is rewritten."""
        try:
            self.csv_f.close()
        except Exception:
            pass
        self.csv_f = open(self.csv_path, "w", newline="", encoding="utf-8")
        self.writer = csv.DictWriter(self.csv_f, fieldnames=FIELDS, extrasaction="ignore")
        self.writer.writeheader()
        self.writer.writerows(self.rows)
        self.csv_f.flush()

    # ---- after a hit -------------------------------------------------------

    def after_hit(self, last=False):
        """Attribute the loss to the transition ~detect_delay before Windows
        reported it. True = reconnected, keep hunting."""
        a = self.a
        self._handled = self.link.down_at
        self.audio.abort()
        self._avrcp(False)
        t_d = self.link.down_at
        target = t_d - a.detect_delay

        log("")
        log("*" * 64)
        log(f"*** LINK LOST - Windows reported the disconnect at {stamp(self.link.down_wall)}")
        # only completed cycles since the last (re)connect can hold the fatal one
        cands = [(t, kind, r) for r in self.rows if r["result"] == "ok"
                 for t, kind in ((r["_on"], "START"), (r["_off"], "SUSPEND"))
                 if t is not None and max(target - 8, self.since) <= t <= target + 5]
        fatal = None
        if cands:
            t_f, kind, fatal = min(cands, key=lambda c: abs(c[0] - target))
            log(f"    transitions ~{a.detect_delay:.1f} s before the report "
                f"(the fatal one should be among them):")
            for t, k, r in cands:
                tag = "   <-- closest" if t == t_f else ""
                log(f"      {stamp(mono_to_wall(t))}  cycle {r['cycle']:>4}  "
                    f"gap {r['gap_ms']:>6}  avrcp {fmt_off(r['avrcp_offset_ms']):>5}  "
                    f"{k:<7} {t - t_d:+6.1f} s{tag}")
        else:
            t_f, kind = target, "steady"
            before = [r for r in self.rows if r["result"] == "ok" and r["_on"] is not None
                      and self.since <= r["_on"] <= target]
            fatal = before[-1] if before else None
            log("    no audio transition ~21 s before the report: the link died while "
                "the stream state was NOT changing (new information)")

        if fatal is not None:
            fatal.update(result="link_lost", phase=kind,
                         note=f"estimated from the disconnect report at "
                              f"{stamp(self.link.down_wall)} - verify in the trace")
            for r in self.rows:
                if r["cycle"] > fatal["cycle"] and r["result"] == "ok":
                    r["result"] = "after_loss"
            hit = fatal
        else:
            hit = {"cycle": "?", "time_on": stamp(mono_to_wall(target)), "phase": "unknown",
                   "gap_ms": "?", "avrcp_offset_ms": "", "note": "no cycle before the loss"}
        self.hits.append(hit)
        log(f"    attributed to cycle {hit['cycle']}, {hit['phase']}, gap {hit['gap_ms']} ms, "
            f"avrcp {fmt_off(hit['avrcp_offset_ms'])}")
        marks = [m for m in self.keys.marks if t_f - 5 <= m[0] <= t_d + 2]
        for m_mono, m_wall in marks:
            log(f"    your mark at {stamp(m_wall)}: {m_mono - t_f:+.1f} s relative to that transition")
        if marks:
            log("    a tone within a few seconds means the headphones dropped the link "
                "themselves (supervision timeout takes ~20 s)")
        log("*" * 64)

        STOP.wait(a.post_hit)
        more = not last and len(self.hits) < a.max_hits and not STOP.is_set()
        etl = self.wpr.save(f"hit{len(self.hits)}", restart=more)
        if etl:
            hit["note"] = f"{hit['note']}; trace {os.path.basename(etl)}"
        self.rewrite_csv()
        return more and self.wait_reconnect()

    def wait_reconnect(self):
        a = self.a
        log(f"waiting up to {a.reconnect_wait} s for the headphones to come back;")
        log("if they do not reconnect by themselves, connect them in Quick Settings -")
        log("the hunt continues automatically")
        deadline = time.monotonic() + a.reconnect_wait
        while not self.link.up.is_set():
            if STOP.is_set() or time.monotonic() > deadline:
                log("no reconnect - stopping")
                return False
            self.link.up.wait(0.2)
        STOP.wait(5)                           # let the audio endpoint settle
        if STOP.is_set() or not self.audio.rescan():
            return False
        STOP.wait(2)
        self.t_idle = self.since = time.monotonic()
        log("reconnected, hunting again")
        return True

    # ---- run ---------------------------------------------------------------

    def _loop(self, plan):
        for gap, off in plan:
            if STOP.is_set():
                return
            if self.link.down.is_set() and not self.after_hit():
                return
            row = self.one_cycle(gap, off)
            self._record(row)
            if row["result"] == "stopped":
                return
            if row["result"] == "local_error":
                log(f"local audio problem, not the headphones: {row['note']} - stopping")
                return
        if not self.link.down.is_set():
            # a loss in the last cycles is reported only ~21 s later
            log(f"plan finished - watching the link {self.a.detect_delay + 3:.0f} s more")
            sleep_until(time.monotonic() + self.a.detect_delay + 3, self.link.down)

    def run(self, plan):
        self.total = len(plan)
        self.t_idle = self.since = time.monotonic()
        try:
            self._loop(plan)
        finally:
            if self.link.down.is_set() and self._handled != self.link.down_at:
                self.after_hit(last=True)

    def cleanup(self):
        self.audio.close()
        if self.avrcp:
            self.avrcp.close()
        self.rewrite_csv()
        self.csv_f.close()

    def summary(self):
        a = self.a
        if self.avrcp:
            av = "script-controlled"
        elif a.mode == "player":
            av = "automatic (player)"
        else:
            av = "off"
        lines = [f"device {self.link.name}  output {self.audio.dev_name}  mode {a.mode}  "
                 f"avrcp {av}  play {a.play} ms", "",
                 f"{'gap_ms':>8} {'avrcp':>6} {'cycles':>7} {'ok':>6} {'lost':>5} "
                 f"{'other':>6}   link_lost rate"]
        stats = {}
        skipped = {}
        for r in self.rows:
            if r["result"] in NOT_COUNTED:
                skipped[r["result"]] = skipped.get(r["result"], 0) + 1
                continue
            c = stats.setdefault((r["gap_ms"], r["avrcp_offset_ms"]), {})
            c[r["result"]] = c.get(r["result"], 0) + 1

        def key(kv):
            gap, off = kv[0]
            return (gap, off if isinstance(off, int) else -10 ** 9)

        for (gap, off), c in sorted(stats.items(), key=key):
            n = sum(c.values())
            lost = c.get("link_lost", 0)
            ok = c.get("ok", 0)
            other = n - ok - lost
            if lost:
                rate = f"{lost}/{n} = {100.0 * lost / n:.1f} %"
            else:
                rate = f"0/{n}  (95 % upper bound {min(100.0, 300.0 / n):.1f} %)"
            lines.append(f"{gap:>8} {fmt_off(off):>6} {n:>7} {ok:>6} {lost:>5} {other:>6}   {rate}")
        if skipped:
            why = (" (after_loss = ran into an already dead link)"
                   if "after_loss" in skipped else "")
            lines += ["", "not counted: " + ", ".join(f"{v} {k}" for k, v in skipped.items())
                      + why]
        if self.keys.marks:
            lines += ["", "your 'disconnect tone' marks:"]
            for m_mono, m_wall in self.keys.marks:
                prev = [(t, k, r) for r in self.rows
                        for t, k in ((r["_on"], "START"), (r["_off"], "SUSPEND"))
                        if t is not None and t <= m_mono]
                if prev:
                    t, k, r = max(prev, key=lambda x: x[0])
                    lines.append(f"  {stamp(m_wall)}: {m_mono - t:.1f} s after {k} of cycle "
                                 f"{r['cycle']} (gap {r['gap_ms']})")
                else:
                    lines.append(f"  {stamp(m_wall)}: before the first cycle")
            if not self.hits:
                lines.append("  Windows reported no disconnect: the run stopped before the ~21 s "
                             "supervision timeout, so the cycles after the mark still count as ok")
        if self.hits:
            lines += ["", "hits (attribution estimated, verify in the trace):"]
            for h in self.hits:
                t = h.get("time_off") if h.get("phase") == "SUSPEND" else h.get("time_on")
                lines.append(f"  cycle {h['cycle']} at {t or '?'}: "
                             f"{h['phase']} (gap {h['gap_ms']}, avrcp "
                             f"{fmt_off(h['avrcp_offset_ms'])}) - {h['note']}")
        return "\n".join(lines)


def collect_meta(args, link, audio, avrcp):
    ps = ("Get-PnpDevice -Class Bluetooth -PresentOnly | ForEach-Object { "
          "$v = (Get-PnpDeviceProperty -InstanceId $_.InstanceId "
          "-KeyName 'DEVPKEY_Device_DriverVersion' -ErrorAction SilentlyContinue).Data; "
          "\"$($_.FriendlyName)`t$v`t$($_.InstanceId)\" }")

    def out(cmd):
        try:
            r = run_text(cmd, 30)
            return [x.strip() for x in (r.stdout or r.stderr).splitlines() if x.strip()]
        except Exception as e:
            return [f"n/a ({type(e).__name__})"]

    return {
        "started": stamp(), "script_version": VERSION, "args": vars(args),
        "device": link.name, "mac": args.mac,
        "output": audio.dev_name, "sample_rate": getattr(audio, "sr", None),
        "avrcp": ("script-controlled SMTC" if avrcp else
                  "automatic (MediaPlayer)" if args.mode == "player" else "off"),
        "windows": platform.platform(), "python": sys.version.split()[0],
        "bluetooth_drivers": out(["powershell", "-NoProfile", "-Command", ps]),
        "wifi": out(["netsh", "wlan", "show", "interfaces"])[:25],
        "codec": "not visible from user mode - AVDTP SET_CONFIGURATION in the trace",
    }


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------

def main():
    global _log_file
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--mac", help="headphone address, same as on Linux (required)")
    p.add_argument("--mode", choices=("wasapi", "player"), default="wasapi",
                   help="wasapi = script-controlled stream + AVRCP (default), "
                        "player = Windows MediaPlayer with your files")
    p.add_argument("--list-devices", action="store_true", help="list WASAPI outputs and exit")
    p.add_argument("--device", help="wasapi: part of the output name, e.g. Px7 "
                                    "(the Hands-Free endpoint is skipped)")
    p.add_argument("--device-index", type=int, help="wasapi: exact index from --list-devices")
    p.add_argument("--exclusive", action="store_true", help="wasapi: exclusive mode")
    p.add_argument("--audio", help="wasapi: audio file (default: generated quiet test tone)")
    p.add_argument("--files", nargs="+", help="player: files, folders or wildcards")
    p.add_argument("--switch-tracks", action="store_true",
                   help="player: next file on every START")
    p.add_argument("--volume", type=float, default=0.5, help="0..1 (default 0.5)")
    p.add_argument("--gap", type=int, default=500,
                   help="audio off -> audio on in ms (default 500)")
    p.add_argument("--sweep-gap", type=parse_list, help="comma-separated gaps, e.g. 300,500,1000")
    p.add_argument("--preset", choices=("A", "B"),
                   help="A: gaps 200-1200 ms, B: 25-45 s (Windows puts the link into sniff)")
    p.add_argument("--play", type=int, default=3000, help="audio time per cycle in ms (default 3000)")
    p.add_argument("--avrcp-offset", type=int, default=-2,
                   help="wasapi: AVRCP PLAYING relative to audio on, ms; negative = before "
                        "(Windows players: about -2)")
    p.add_argument("--sweep-avrcp", type=parse_list,
                   help="comma-separated offsets; negatives need '=': --sweep-avrcp=-200,-2,0,50")
    p.add_argument("--no-avrcp", dest="avrcp", action="store_false",
                   help="wasapi: no media session, no AVRCP status changes")
    p.add_argument("--cycles-per-config", type=int, default=100)
    p.add_argument("--order", choices=("interleave", "block"), default="interleave",
                   help="interleave configs cycle by cycle (default) or run them in blocks")
    p.add_argument("--max-hits", type=int, default=1,
                   help="stop after this many hits; >1 waits for the reconnect and continues")
    p.add_argument("--detect-delay", type=float, default=DETECT_DELAY_S,
                   help="s from the fatal transition to Windows reporting the disconnect "
                        f"(default {DETECT_DELAY_S}; calibrate against the trace)")
    p.add_argument("--post-hit", type=float, default=5,
                   help="s of extra trace after the disconnect report (default 5)")
    p.add_argument("--reconnect-wait", type=int, default=180,
                   help="s to wait for the headphones to come back after a hit")
    p.add_argument("--wpr", help="start WPR with this profile, e.g. "
                                 "C:\\bt\\BluetoothStack.wprp!BluetoothStack (admin)")
    p.add_argument("--wpr-filemode", action="store_true", help="WPR file mode instead of memory")
    p.add_argument("--outdir", default="a2dp_hunt_win")
    args = p.parse_args()

    if sys.platform != "win32":
        sys.exit("this script is for Windows; on Linux use a2dp_hunt.py")
    if args.list_devices:
        import sounddevice as sd
        for i, n in wasapi_outputs(sd):
            print(f"[{i:3}] {n}")
        return
    if not args.mac:
        sys.exit("--mac is required")
    args.mac = args.mac.upper()
    if args.mode == "player" and not args.files:
        sys.exit("--mode player needs --files")
    if args.mode == "wasapi" and args.files:
        sys.exit("--files is for --mode player; with wasapi use --audio")

    run_dir = os.path.abspath(os.path.join(
        args.outdir, "run_" + datetime.now().strftime("%Y%m%d_%H%M%S")))
    os.makedirs(run_dir, exist_ok=True)
    _log_file = open(os.path.join(run_dir, "hunt.log"), "w", encoding="utf-8")
    signal.signal(signal.SIGINT, lambda *_: STOP.set())
    try:
        ctypes.windll.winmm.timeBeginPeriod(1)     # 1 ms timer resolution
    except Exception:
        pass

    log(f"a2dp_hunt_win {VERSION}, mode {args.mode}")
    link = Link(args.mac)
    log(f"device    {link.name} ({args.mac}), connected")
    audio = WasapiAudio(args) if args.mode == "wasapi" else PlayerAudio(args)
    avrcp = None
    if args.mode == "wasapi" and args.avrcp:
        try:
            avrcp = Avrcp()
            log("avrcp     own media session (Playing / Paused set by the script)")
        except Exception as e:
            log(f"WARNING: media session failed ({e!r}), continuing without AVRCP")
    elif args.mode == "player":
        log("avrcp     automatic, from the player's media session")
    else:
        log("avrcp     disabled")

    gaps = PRESETS[args.preset] if args.preset else (args.sweep_gap or [args.gap])
    offsets = (args.sweep_avrcp or [args.avrcp_offset]) if avrcp else [None]
    configs = [(g, o) for g in gaps for o in offsets]
    n = args.cycles_per_config
    if args.order == "interleave":
        plan = [c for _ in range(n) for c in configs]
    else:
        plan = [c for c in configs for _ in range(n)]
    est_s = sum(g + args.play + 300 for g, _ in plan) / 1000.0
    log(f"plan      {len(configs)} config(s) x {n} cycles = {len(plan)} cycles, "
        f"about {est_s / 60:.0f} min")
    if min(gaps) < 300:
        log("NOTE: Windows sends SUSPEND only after buffered audio drains; with gaps "
            "below ~300 ms check in the trace that a SUSPEND happens at all")
    if max(gaps) >= 5000 and min(gaps) < 20000:
        log("NOTE: Windows enters sniff 10-17 s after SUSPEND; gaps between 5 and 20 s "
            "may or may not be in sniff at START")

    with open(os.path.join(run_dir, "meta.json"), "w", encoding="utf-8") as f:
        json.dump(collect_meta(args, link, audio, avrcp), f, indent=2, ensure_ascii=False)

    keys = Keys(link, args.detect_delay)
    wpr = Wpr(args.wpr, args.wpr_filemode, run_dir)
    hunter = Hunter(args, link, audio, avrcp, keys, wpr, run_dir)
    link.start()
    keys.start()
    log("running - m = heard the disconnect tone, q or Ctrl+C = stop")
    STOP.wait(1.0)
    try:
        hunter.run(plan)
    finally:
        hunter.cleanup()
        wpr.save("final", restart=False)
        summary = hunter.summary()
        with open(os.path.join(run_dir, "summary.txt"), "w", encoding="utf-8") as f:
            f.write(summary + "\n")
        log("")
        for line in summary.splitlines():
            log(line)
        log("")
        log(f"results: {run_dir}")
        try:
            ctypes.windll.winmm.timeEndPeriod(1)
        except Exception:
            pass
        _log_file.close()
        _log_file = None


if __name__ == "__main__":
    main()
