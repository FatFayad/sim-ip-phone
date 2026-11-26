# Why 3.5mm analog audio failed on the BG2-U01

Before giving up on the 3.5mm splitter and ordering a USB Bluetooth dongle, I spent a night tracing why voice wouldn't pass cleanly between the Huawei MediaPad T3 7 (`BG2-U01`, Spreadtrum `SC7731G`, Android 7.0) and my Debian box (`Realtek ALC897`, PipeWire 1.4).

## Bugs I did fix along the way

1. WirePlumber kept silently switching the ALC897 `Input Source` (`numid=20` and `numid=21`) back to `Rear Mic` (unplugged pink jack) instead of `Line` (blue jack). Fixed in `ip_caller.py` by enforcing `amixer -c 0 cset numid=20 2` and `numid=21 2` when the bridge opens and every 2 seconds.
2. Browser TX sat at `tx_frames=0` until you clicked the page because `await audioCtx.resume()` blocked the WebSocket setup. Made `resume()` non-blocking and kept `micStream` alive across calls.

Even after both fixes, calls still had a loud buzz on RX and dead TX on the remote phone.

## The actual hardware blockers

### 1. Spreadtrum `headset_detect` comparator bounce + C-level HAL mic lockout

Checking `/proc/interrupts` on the tablet showed `irq 433 (headset_detect)` had fired 7,574 times in about 14 hours. Triggering a single UI sound effect over ADB without touching the cable bumped `irq 433` from 7574 to 7580 and fired `ACTION_HEADSET_PLUG state=0` within a second.

The Spreadtrum detect pin expects a real low-impedance load (around 32 ohm headphone coils plus a ~1.5k ohm electret mic capsule) to hold `/sys/class/switch/h2w/state` at `1`. A passive splitter wired into high-impedance PC line jacks (~20k ohm) leaves the detect pin floating right on the edge, so it drops back to `0` (unplugged).

I pulled `/system/lib/hw/audio.primary.sc8830.so` off the tablet and disassembled `headset_no_mic` (`0x117b4`) and `SetParas_Route_Incall` (`0x0ebe2`). In C, `headset_no_mic()` reads `/sys/class/switch/h2w/state` directly and only enables the external mic path when `state == 1`. Because Android's audio HAL checks sysfs straight from C, trying to force `WIRED_HEADSET` from Java (`service call audio 46`) only lied to the UI while the baseband kept the 3.5mm mic muted.

### 2. 50 Hz USB to 3.5mm ground loop

Both the USB cable (for ADB and power) and the 3.5mm audio cable shared ground between the Debian box and the tablet. Running `debug/buzz_analyze.py` (a small Goertzel scan on `Line In`) showed a strong 50 Hz Dhaka mains fundamental at `-45.5 dBFS`, 150 Hz at `-59.1 dBFS`, and harmonics up to 2 kHz. Muting `Line Out` changed the noise floor by less than 3 dB, confirming a ground loop rather than digital noise from the server.

## Next step

Instead of stacking ground-loop isolators and dummy load resistors onto a flaky 3.5mm jack, I am moving voice transport to a USB Bluetooth 5.0 dongle on Debian (Bluetooth HFP over PipeWire). That removes the analog ground loop and lets the tablet route call audio digitally over Bluetooth without touching `h2w`.
