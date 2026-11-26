#!/usr/bin/env python3
"""3-minute full-stack call, ADB audio route, ALSA jack, and WebSocket audio logger."""

from datetime import datetime
from pathlib import Path
import re
import subprocess
import sys
import threading
import time

SERIAL = os.environ.get("ADB_SERIAL", "")
DURATION_SECONDS = 180.0
LOG_PATH = Path(__file__).resolve().parent / "call_debug_3min.log"

_stop = threading.Event()
_log_lock = threading.Lock()

stats = {
    "total_tx_frames": 0,
    "max_tx_db": -120.0,
    "total_rx_frames": 0,
    "max_rx_db": -120.0,
    "main_types": set(),
    "audio_routes": [],
    "call_states": [],
    "ws_events": [],
}


def log_line(fp, msg: str, *, echo: bool = True) -> None:
    ts = datetime.now().strftime("%H:%M:%S.%f")[:-3]
    line = f"[{ts}] {msg}"
    with _log_lock:
        fp.write(line + "\n")
        fp.flush()
    if echo:
        print(line, flush=True)


def journal_tail_worker(fp) -> None:
    proc = subprocess.Popen(
        ["journalctl", "--user", "-u", "sim-ip-caller.service", "-f", "-n", "0", "-o", "short-iso"],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    assert proc.stdout is not None
    try:
        for line in proc.stdout:
            if _stop.is_set():
                break
            s = line.strip()
            if not s:
                continue
            m_stats = re.search(
                r"BRIDGE_STATS tx_frames=(\d+) tx_peak=([-\d.]+)dBFS rx_frames=(\d+) rx_peak=([-\d.]+)dBFS",
                s,
            )
            if m_stats:
                tx_f = int(m_stats.group(1))
                tx_db = float(m_stats.group(2))
                rx_f = int(m_stats.group(3))
                rx_db = float(m_stats.group(4))
                stats["total_tx_frames"] += tx_f
                stats["total_rx_frames"] += rx_f
                if tx_db > stats["max_tx_db"]:
                    stats["max_tx_db"] = tx_db
                if rx_db > stats["max_rx_db"]:
                    stats["max_rx_db"] = rx_db
                log_line(
                    fp,
                    f"BRIDGE_STATS tx_frames={tx_f} tx_peak={tx_db:.1f}dBFS rx_frames={rx_f} rx_peak={rx_db:.1f}dBFS",
                )
            elif any(
                k in s
                for k in ("/ws/control", "/ws/audio", "AlsaAudioBridge", "ERROR", "Traceback", "POST /api/")
            ):
                stats["ws_events"].append(s)
                log_line(fp, f"SVC {s}")
    finally:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                proc.kill()


def poll_server_alsa() -> str:
    proc = subprocess.run(
        ["amixer", "-c", "0", "contents"],
        capture_output=True,
        text=True,
        timeout=4,
        check=False,
    )
    if proc.returncode != 0:
        return "alsa=error"
    txt = proc.stdout

    def _val(numid: int) -> str:
        m = re.search(rf"numid={numid},[\s\S]*?:\s*values=([^\r\n]+)", txt)
        return m.group(1).strip() if m else "?"

    return (
        f"blue_line_jack={_val(33)} green_out_jack={_val(34)} "
        f"pink_mic_jack={_val(31)} capture={_val(22)} front_out={_val(2)}"
    )


def poll_adb() -> tuple[str, str, str]:
    cmd = (
        "dumpsys telephony.registry | grep -E 'mCallState|mCallIncomingNumber'; "
        "echo ===AUD===; "
        "dumpsys audio | grep -E 'mMainType=|STREAM_VOICE_CALL' -A 3; "
        "echo ===TEL===; "
        "dumpsys telecom | grep -E 'Foreground call:|AUDIO_ROUTE' | tail -n 6"
    )
    proc = subprocess.run(
        ["adb", "-s", SERIAL, "shell", cmd],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=6,
        check=False,
    )
    if proc.returncode != 0:
        return f"ADB_ERR({proc.stderr.strip()})", "?", ""
    parts = proc.stdout.split("===AUD===")
    tel_part = parts[0] if parts else ""
    rest = parts[1] if len(parts) > 1 else ""
    aud_parts = rest.split("===TEL===")
    aud_part = aud_parts[0] if aud_parts else ""
    telecom_part = aud_parts[1] if len(aud_parts) > 1 else ""

    m_cs = re.search(r"mCallState=(\d+)", tel_part)
    m_num = re.search(r"mCallIncomingNumber=(.*)", tel_part)
    cs = m_cs.group(1) if m_cs else "?"
    num = m_num.group(1).strip() if m_num else ""

    m_mt = re.search(r"mMainType=(0x[0-9a-fA-F]+)", aud_part)
    mt = m_mt.group(1) if m_mt else "?"
    m_dev = re.search(r"Devices:\s*([^\r\n]+)", aud_part)
    dev = m_dev.group(1).strip() if m_dev else "?"

    routes = " | ".join(
        line.strip() for line in telecom_part.splitlines() if line.strip()
    )
    return f"call_state={cs} num={num or '-'}", f"mMainType={mt} voice_dev={dev}", routes


def main() -> int:
    with LOG_PATH.open("w", encoding="utf-8") as fp:
        log_line(fp, f"WATCH_START duration={DURATION_SECONDS:.0f}s log={LOG_PATH}")
        t_j = threading.Thread(target=journal_tail_worker, args=(fp,), daemon=True)
        t_j.start()

        prev_alsa = ""
        prev_call = ""
        prev_aud = ""
        prev_routes = ""
        deadline = time.monotonic() + DURATION_SECONDS

        try:
            while time.monotonic() < deadline:
                alsa = poll_server_alsa()
                call_s, aud_s, routes = poll_adb()

                if aud_s != prev_aud:
                    stats["main_types"].add(aud_s)
                    log_line(fp, f"JACK_MAIN_TYPE {aud_s}")
                    prev_aud = aud_s
                if call_s != prev_call:
                    stats["call_states"].append(call_s)
                    log_line(fp, f"CALL_STATE {call_s}")
                    prev_call = call_s
                if alsa != prev_alsa:
                    log_line(fp, f"SERVER_JACKS {alsa}")
                    prev_alsa = alsa
                if routes and routes != prev_routes:
                    stats["audio_routes"].append(routes)
                    log_line(fp, f"TELECOM_ROUTE {routes}")
                    prev_routes = routes

                time.sleep(2.0)
        finally:
            _stop.set()
            t_j.join(timeout=2.0)

        log_line(fp, "=== SUMMARY (3 MIN) ===")
        log_line(
            fp,
            f"TX_TOTAL_FRAMES={stats['total_tx_frames']} MAX_TX_PEAK={stats['max_tx_db']:.1f}dBFS",
        )
        log_line(
            fp,
            f"RX_TOTAL_FRAMES={stats['total_rx_frames']} MAX_RX_PEAK={stats['max_rx_db']:.1f}dBFS",
        )
        log_line(fp, f"OBSERVED_JACK_MODES={sorted(stats['main_types'])}")
        log_line(fp, f"OBSERVED_CALL_STATES={stats['call_states']}")
        log_line(fp, f"OBSERVED_TELECOM_ROUTES={stats['audio_routes'][-5:]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
