# win-bluetooth-suspends-start-script

Diagnostic script for testing how Bluetooth headphones handle A2DP stream
transitions on Windows. It plays a short sound, stops, waits and plays again,
over and over, so that Windows sends **AVDTP START** and **AVDTP SUSPEND** to the
headphones on every cycle. It records every transition, detects when the
headphones drop the link, and can capture a Bluetooth HCI trace of the failure.

It was written to reproduce a link-loss bug in the **Bowers & Wilkins Px7 S2**
(see [Results with Px7 S2](#results-with-bowers--wilkins-px7-s2)), but it works
with any A2DP headphones.

## Requirements

- Windows 10 or 11
- Python 3.10 or newer from [python.org](https://www.python.org/downloads/windows/)
  (tick *Add python.exe to PATH* in the installer)
- The headphones paired with the PC and connected
- Optional, for HCI traces: an elevated (administrator) terminal, `wpr.exe`
  (built into Windows), `btetlparse.exe` from the Microsoft
  [Bluetooth Test Platform](https://learn.microsoft.com/en-us/windows-hardware/drivers/bluetooth/testing-btp-overview),
  and [Wireshark](https://www.wireshark.org/)

Install the Python packages (one line):

```
py -m pip install numpy sounddevice soundfile winrt-runtime winrt-Windows.Foundation winrt-Windows.Devices.Bluetooth winrt-Windows.Media winrt-Windows.Media.Core winrt-Windows.Media.Playback
```

Use `py -m pip`, not plain `pip`, so the packages go into the same Python that
`py` runs.

## Quick start

1. Find the name of the headphones' audio output:

   ```
   py a2dp_hunt_win.py --list-devices
   ```

   Look for a line like `Headphones (Px7 S2)`. Part of the name is enough for
   `--device`; the Hands-Free endpoint is skipped automatically.

2. Find the Bluetooth address of the headphones: Device Manager → Bluetooth →
   the headphones → Properties → Details → *Bluetooth device address*, or in
   PowerShell:

   ```
   Get-PnpDevice -Class Bluetooth | Where-Object FriendlyName -like '*Px7*' | Select-Object FriendlyName, InstanceId
   ```

   The address is the 12 hex digits after `DEV_`. Both `AA:BB:CC:DD:EE:FF` and
   `aabbccddeeff` are accepted.

3. Run:

   ```
   py a2dp_hunt_win.py --mac AA:BB:CC:DD:EE:FF --device Px7
   ```

   By default this runs 100 cycles of 3 s tone and 0.5 s pause and stops at the
   first link loss.

## Before every run

- Close everything that can play or hold audio: browsers, Teams, Discord,
  Spotify, games. Anything holding the audio output open keeps the stream
  alive, and Windows never sends SUSPEND.
- Turn off system sounds (Sound settings → More sound settings → Sounds →
  Sound Scheme: *No Sounds*).
- Wear the headphones or disable their wear sensor. Taking them off makes many
  headphones send AVRCP pause commands.
- Keep the laptop on power with sleep disabled for long runs.
- For `--mode player` the headphones must be the default playback device.
- Do not click inside the console window: in the classic console, selecting
  text pauses the program.

## While it runs

Each cycle prints one line:

```
[2026-09-28T23:46:35.665+02:00] #  10/50 gap 500 | on 141.7 ms | actual gap 500.6 ms | ok
```

Keys (the console window must be focused):

| Key | Action |
| --- | --- |
| `m` | Press the moment you hear the headphones' own disconnect sound. The time goes into the report. |
| `q` or Ctrl+C | Stop cleanly. The trace and the summary are still saved. |

**Windows notices a dead link only after the link supervision timeout, about
21 seconds later.** Until then it keeps "playing" into the dead link and the
script keeps cycling. When Windows reports the disconnect, the script
attributes the loss to the transition about 21 s earlier (`--detect-delay`),
marks the cycles that ran into the dead link as `after_loss` and leaves them
out of the statistics. Wait for the report; do not press `q` right after
hearing the disconnect sound.

## Modes

| Mode | What drives the transitions | AVRCP |
| --- | --- | --- |
| `wasapi` (default) | The script opens and closes a WASAPI stream with a quiet tone (or `--audio` file). Precise timing. | Script-controlled: the script owns a Windows media session and sets it to Playing / Paused around each START (`--avrcp-offset`). `--no-avrcp` turns it off. |
| `player` | Windows MediaPlayer plays your `--files` with play / pause, optionally switching track on every START. | Automatic, decided by Windows as with a real player. |

AVDTP is not reachable from user mode on Windows, so the script provokes
START and SUSPEND the way media players do: Windows sends START when audio
starts and SUSPEND once the buffered audio has drained after it stops. `--gap`
is therefore the time between stopping and starting audio; the real
SUSPEND → START gap on the air is shorter and must be read from the trace.

## Capturing an HCI trace

`BluetoothStack.wprp` in this repository is Microsoft's Bluetooth tracing
profile from [microsoft/busiotools](https://github.com/microsoft/busiotools).
Run from an elevated terminal and pass it with `--wpr`:

```
py a2dp_hunt_win.py --mac AA:BB:CC:DD:EE:FF --device Px7 --wpr "C:\path\BluetoothStack.wprp!BluetoothStack.Light"
```

The profile file defines only one in-memory profile, `BluetoothStack.Light`.
Without `.Light` WPR looks for a verbose in-memory profile that does not
exist and fails. To record to a file instead, use
`--wpr "...\BluetoothStack.wprp!BluetoothStack" --wpr-filemode` (larger
files). The script refuses to hunt without a trace when `--wpr` is given and
WPR fails to start.

The script saves `hit1.etl`, `hit2.etl`, … after every failure and
`final.etl` at the end of a run without failures. Convert them to pcapng with
`btetlparse` (the output file comes first, then the input):

```
cd "C:\BTP\v1.14.0\x64"
Get-ChildItem C:\path\a2dp_hunt_win -Recurse -Filter *.etl | ForEach-Object { .\btetlparse.exe -pcapng ([IO.Path]::ChangeExtension($_.FullName, ".pcapng")) $_.FullName }
```

### Reading the trace in Wireshark

Set View → Time Display Format → Time of Day, so times match the script's log.

A memory-mode trace usually starts after the headphones connected, so it does
not contain the L2CAP channel setup, and Wireshark shows AVDTP as plain L2CAP.
To decode it, find the AVDTP signalling channel: the host sends it 3-byte
packets (`btl2cap.length == 3 && hci_h4.direction == 0x00`) and the headphones
answer with 2-byte packets (`btl2cap.length == 2 && hci_h4.direction == 0x01`).
Add both CIDs in Analyze → Decode As… → *L2CAP CID* → *AVDTP*. For complete
decoding including AVRCP, start WPR yourself before the headphones connect,
run the script without `--wpr`, and stop WPR afterwards.

| Filter | Shows |
| --- | --- |
| `bthci_evt.code == 0x05` | Disconnection Complete (0x08 = connection timeout) |
| `bthci_acl && hci_h4.direction == 0x01` | everything received from the headphones; the last packet before the disconnect is where they died |
| `btavdtp.signal_id in {0x07, 0x09}` | AVDTP START and SUSPEND (after Decode As) |
| `bthci_evt.code == 0x13` | Number Of Completed Packets: the headphones' radio acknowledged the host's packets |
| `bthci_evt.code == 0x14` | Mode Change (sniff) |
| `bthci_evt.code == 0x11` | Flush Occurred |

## Output

Each run creates `a2dp_hunt_win\run_<date>_<time>\`:

| File | Contents |
| --- | --- |
| `summary.txt` | results per configuration, with failure rate and 95 % upper bound |
| `cycles.csv` | one row per cycle: `time_on` ≈ START, `time_off` ≈ SUSPEND follows |
| `hunt.log` | the console log |
| `meta.json` | Windows build, Bluetooth driver versions, Wi-Fi state, audio output |
| `hitN.etl`, `final.etl` | WPR traces (with `--wpr`) |

| `result` | Meaning | Counted |
| --- | --- | --- |
| `ok` | cycle completed | yes |
| `link_lost` | the loss is attributed to this cycle (estimate, verify in the trace) | yes |
| `after_loss` | ran into an already dead link before Windows noticed | no |
| `stopped` | stopped with `q` or Ctrl+C | no |
| `local_error` | audio problem on the PC, not the headphones; the run stops | yes |

## Options

| Option | Default | Meaning |
| --- | --- | --- |
| `--mac` | required | headphones' Bluetooth address |
| `--mode` | `wasapi` | `wasapi` or `player` |
| `--list-devices` | | list WASAPI outputs and exit |
| `--device` | | part of the output name (wasapi) |
| `--device-index` | | exact index from `--list-devices` |
| `--exclusive` | off | WASAPI exclusive mode |
| `--audio` | test tone | audio file for wasapi (WAV, FLAC, MP3, OGG) |
| `--files` | | files, folders or wildcards for player |
| `--switch-tracks` | off | player: next file on every START |
| `--volume` | 0.5 | 0..1 |
| `--gap` | 500 | ms from audio off to audio on |
| `--sweep-gap` | | comma-separated gaps, e.g. `300,500,1000` |
| `--preset` | | `A`: gaps 200–1200 ms; `B`: gaps 25–45 s (Windows puts the idle link into sniff before START) |
| `--play` | 3000 | ms of audio per cycle |
| `--avrcp-offset` | -2 | ms of AVRCP Playing relative to audio on; negative = before |
| `--sweep-avrcp` | | comma-separated offsets; negatives need `=`: `--sweep-avrcp=-200,-2,0,50` |
| `--no-avrcp` | | no media session, no AVRCP status changes (wasapi) |
| `--cycles-per-config` | 100 | cycles per configuration |
| `--order` | `interleave` | interleave configurations cycle by cycle, or `block` |
| `--max-hits` | 1 | failures to collect; more than 1 waits for the headphones to reconnect and continues |
| `--detect-delay` | 21.3 | s from the fatal transition to Windows reporting the disconnect |
| `--post-hit` | 5 | s of extra trace after the disconnect report |
| `--reconnect-wait` | 180 | s to wait for the headphones after a failure |
| `--wpr` | | WPR profile; enables trace capture (administrator) |
| `--wpr-filemode` | off | WPR file mode instead of memory |
| `--outdir` | `a2dp_hunt_win` | where run folders go |

## Examples

```
# the basic reproduction, 50 cycles, with an HCI trace
py a2dp_hunt_win.py --mac AA:BB:CC:DD:EE:FF --device Px7 --gap 500 --cycles-per-config 50 --wpr "C:\path\BluetoothStack.wprp!BluetoothStack.Light"

# the same without any AVRCP traffic
py a2dp_hunt_win.py --mac AA:BB:CC:DD:EE:FF --device Px7 --gap 500 --no-avrcp --cycles-per-config 50

# long gaps: the link is in sniff mode before every START
py a2dp_hunt_win.py --mac AA:BB:CC:DD:EE:FF --device Px7 --preset B

# collect 5 failures in one session
py a2dp_hunt_win.py --mac AA:BB:CC:DD:EE:FF --device Px7 --max-hits 5

# your own music with play / pause and a track change on every cycle
py a2dp_hunt_win.py --mac AA:BB:CC:DD:EE:FF --mode player --files "D:\Music\test" --switch-tracks
```

## Results with Bowers & Wilkins Px7 S2

Setup: Windows 11 (build 26200), Realtek RTL8852BE adapter.

- Every run failed within the first 10 cycles; runs started right after a
  reconnect failed at exactly the 10th SUSPEND.
- In all captured failures the last packet from the headphones is AVDTP
  SUSPEND_ACCEPT, sent normally. The next packet from the host is never
  acknowledged, not even at baseband level.
- The headphones play their own disconnect sound less than a second after
  SUSPEND_ACCEPT and send no disconnect to the host. Windows detects the loss
  only through link supervision timeout, 21.05–21.32 s later (HCI
  Disconnection Complete, reason 0x08).
- The host sequence and timing are the same in failing and successful cycles.
- It fails with AAC and with SBC, and without any AVRCP traffic. Sniff mode is
  not involved.
- On the same laptop under Linux (BlueZ + PipeWire) the failure did not occur.
  The main timing difference: Windows sends SUSPEND 15–50 ms after the last
  audio packet.