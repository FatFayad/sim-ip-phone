import os
#!/usr/bin/env python3
"""Watch HEADSET_PLUG broadcasts on the tablet for N seconds and log new ones with timestamps."""
import re, subprocess, sys, time

DUR = int(sys.argv[1]) if len(sys.argv) > 1 else 240
seen = set()
pat = re.compile(r"#\d+: act=android.intent.action.HEADSET_PLUG.*?\n\s+enq=(\S+ \S+).*?\n\s+extras: Bundle\[\{(.*?)\}\]", re.S)

def snap():
    out = subprocess.run(["adb", "-s", os.environ.get("ADB_SERIAL", ""), "shell", "dumpsys activity broadcasts"],
                         capture_output=True, text=True, errors="replace").stdout
    return pat.findall(out)

for enq, extras in snap():
    seen.add((enq, extras))
print(f"[{time.strftime('%H:%M:%S')}] watching HEADSET_PLUG for {DUR}s (baseline {len(seen)} entries)", flush=True)
end = time.time() + DUR
while time.time() < end:
    time.sleep(2)
    for enq, extras in reversed(snap()):
        if (enq, extras) not in seen:
            seen.add((enq, extras))
            print(f"[{time.strftime('%H:%M:%S')}] HEADSET_PLUG enq={enq} {extras}", flush=True)
print("done", flush=True)
