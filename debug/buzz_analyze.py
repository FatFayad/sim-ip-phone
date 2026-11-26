#!/usr/bin/env python3
"""Record Line In for a few seconds and fingerprint buzz frequencies with Goertzel."""
import math, subprocess, sys
from array import array

RATE = 48000
SECS = float(sys.argv[1]) if len(sys.argv) > 1 else 4.0
raw = subprocess.run(
    ["arecord", "-q", "-D", "plughw:0,0", "-f", "S16_LE", "-r", str(RATE), "-c", "2",
     "-d", str(int(SECS)), "-t", "raw"],
    capture_output=True, check=True).stdout
st = array("h"); st.frombytes(raw[: len(raw) - len(raw) % 4])
L = st[0::2]; R = st[1::2]
x = [(a + b) / 2.0 for a, b in zip(L, R)]
n = len(x)
mean = sum(x) / n
x = [v - mean for v in x]
rms = math.sqrt(sum(v * v for v in x) / n)
peak = max(abs(v) for v in x)
print(f"samples={n} rms={20*math.log10(max(rms,1e-9)/32768):.1f}dBFS peak={20*math.log10(max(peak,1e-9)/32768):.1f}dBFS")
print(f"L rms={20*math.log10(max(math.sqrt(sum(v*v for v in L)/len(L)),1e-9)/32768):.1f}  R rms={20*math.log10(max(math.sqrt(sum(v*v for v in R)/len(R)),1e-9)/32768):.1f}")

seg = x[: RATE * 2]  # 2 s => 0.5 Hz resolution
def goertzel(f):
    w = 2 * math.pi * f / RATE; c = 2 * math.cos(w); s1 = s2 = 0.0
    for v in seg:
        s0 = v + c * s1 - s2; s2 = s1; s1 = s0
    p = s1 * s1 + s2 * s2 - c * s1 * s2
    amp = 2 * math.sqrt(max(p, 0)) / len(seg)
    return 20 * math.log10(max(amp, 1e-9) / 32768)

freqs = [50 * k for k in range(1, 21)] + [60, 120, 180, 1000, 2000, 4000, 8000]
res = sorted(((goertzel(f), f) for f in freqs), reverse=True)
print("Top tones (dBFS):")
for db, f in res[:14]:
    print(f"  {f:6.0f} Hz  {db:6.1f}")
# off-harmonic reference
print("ref 75Hz", round(goertzel(75), 1), "ref 325Hz", round(goertzel(325), 1), "ref 1234Hz", round(goertzel(1234), 1))
