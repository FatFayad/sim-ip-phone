# sim-ip-phone

I had an old Huawei MediaPad T3 7 (BG2-U01, Android 7.0, unrooted) sitting on my desk with a SIM card in it, already plugged into my Debian home server over USB. I wanted two things: get incoming calls and SMS forwarded to Discord, and dial or answer calls from my PC browser over LAN or Tailscale.

## What actually works right now

`sim_notifier.py` watches the tablet over ADB (`dumpsys telephony.registry`, `dumpsys notification --noredact`, and `adb logcat -b events` to wake up fast) and sends incoming calls, missed calls, and SMS text to a Discord webhook. No Android app needed on the tablet.

`ip_caller.py` serves a local HTTPS web dialer (`static/index.html`) on port 5443 with PIN + HMAC cookie auth. From the browser you can dial, answer, hang up, send DTMF, and see live call state over WebSockets. It also runs a 16 kHz WebSocket to ALSA PCM bridge (`arecord` and `aplay`) during active calls.

Both run under `systemd --user` on Debian.

## What is still blocked (two-way call audio)

I originally wired the tablet's 3.5mm jack into the server's motherboard Realtek ALC897 Rear Blue (Line In) and Rear Green (Line Out) through a TRRS splitter. Calling and signaling work, and the PC side of the audio bridge is healthy, but two-way voice over the 3.5mm cable failed reliably because of the tablet's Spreadtrum audio HAL and a 50 Hz ground loop.

Full notes and the HAL disassembly are in [`why-35mm-failed.md`](why-35mm-failed.md). The next step is dropping in a cheap USB Bluetooth 5.0 dongle and moving call audio to Bluetooth HFP over PipeWire.

## Files

- `sim_notifier.py` - ADB call/SMS watcher and Discord notifier
- `ip_caller.py` and `static/index.html` - HTTPS web dialer, ADB control plane, and 16 kHz PCM bridge
- `debug/buzz_analyze.py` - Goertzel frequency check I wrote to confirm the 50 Hz mains hum and harmonics
- `debug/watch_3min.py` and `debug/plug_watch.py` - correlated ADB, ALSA, and `ACTION_HEADSET_PLUG` watchers used while debugging
- `config.env.example` and `caller.env.example` - config templates (copy to `config.env` / `caller.env` with `chmod 600`)

## Quick setup

1. Plug in an Android device with USB debugging enabled and verify `adb devices` shows it.
2. Copy `config.env.example` to `config.env` and set your Discord webhook (`chmod 600 config.env`).
3. Copy `caller.env.example` to `caller.env`, set a PIN and random session secret, and generate a self-signed cert in `certs/`.
4. Run `python3 sim_notifier.py self-test` and `python3 ip_caller.py self-test`.
