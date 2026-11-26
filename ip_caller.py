#!/usr/bin/env python3
"""Local HTTPS + WebSocket SIM IP Caller for Huawei BG2-U01 + ALC897 3.5mm audio."""

from __future__ import annotations

import argparse
from array import array
import asyncio
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass
import hashlib
import hmac
import json
import math
import os
from pathlib import Path
import re
import secrets
import subprocess
import sys
import time
from typing import Any

from fastapi import FastAPI, HTTPException, Request, Response, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
import uvicorn

import sim_notifier

BASE_DIR = Path(__file__).resolve().parent
DEFAULT_CONFIG_PATH = BASE_DIR / "caller.env"
DEFAULT_HISTORY_PATH = BASE_DIR / "calls_history.json"
DEFAULT_CERT_PATH = BASE_DIR / "certs" / "cert.pem"
DEFAULT_KEY_PATH = BASE_DIR / "certs" / "key.pem"
STATIC_INDEX_PATH = BASE_DIR / "static" / "index.html"
SERVICE_NAME = "sim-ip-caller.service"

WALLPANEL_ACTIVITY = "com.thanksmister.iot.wallpanel/.ui.activities.BrowserActivityNative"
DIAL_NUMBER_RE = re.compile(r"^[0-9+*#,;]{1,32}$")
DTMF_KEYCODES = {
    "0": 7,
    "1": 8,
    "2": 9,
    "3": 10,
    "4": 11,
    "5": 12,
    "6": 13,
    "7": 14,
    "8": 15,
    "9": 16,
    "*": 17,
    "#": 18,
}

SAMPLE_RATE = 16000
FRAME_SAMPLES = 640  # 40 ms at 16 kHz
STEREO_CHUNK_BYTES = FRAME_SAMPLES * 4  # 2560 bytes (S16_LE stereo)
COOKIE_NAME = "sim_caller_session"
SESSION_TTL_SECONDS = 30 * 86400
HPF_ALPHA = 0.9329  # ~183 Hz 1st-order high-pass at 16 kHz


@dataclass
class CallerConfig:
    """Configuration loaded from caller.env."""

    bind_host: str = "0.0.0.0"
    https_port: int = 5443
    caller_pin: str = "2580"
    session_secret: str = ""
    adb_serial: str = ""
    alsa_device: str = "plughw:0,0"
    capture_volume: int = 54
    line_out_volume: int = 45
    hum_filter_enabled: bool = True


def _strip_quotes(val: str) -> str:
    if len(val) >= 2 and val[0] == val[-1] and val[0] in ("'", '"'):
        return val[1:-1]
    return val


def load_caller_config(
    config_path: Path = DEFAULT_CONFIG_PATH, *, ensure_secret: bool = True
) -> CallerConfig:
    """Parse caller.env and ensure a persistent HMAC session secret exists."""
    raw: dict[str, str] = {}
    if config_path.exists():
        for line in config_path.read_text(encoding="utf-8").splitlines():
            s = line.strip()
            if not s or s.startswith("#") or "=" not in s:
                continue
            k, v = s.split("=", 1)
            raw[k.strip()] = _strip_quotes(v.strip())

    secret = raw.get("SESSION_SECRET", "").strip()
    if not secret and ensure_secret:
        secret = secrets.token_hex(32)
        if config_path.exists():
            sim_notifier.update_config_file(
                config_path, {"SESSION_SECRET": secret}, chmod_600=True
            )

    def _int_val(key: str, default: int, lo: int, hi: int) -> int:
        try:
            return max(lo, min(hi, int(raw.get(key) or default)))
        except ValueError:
            return default

    hum_raw = (raw.get("HUM_FILTER_ENABLED") or "true").strip().lower()
    return CallerConfig(
        bind_host=raw.get("BIND_HOST", "0.0.0.0").strip() or "0.0.0.0",
        https_port=_int_val("HTTPS_PORT", 5443, 1, 65535),
        caller_pin=raw.get("CALLER_PIN", "2580").strip() or "2580",
        session_secret=secret,
        adb_serial=raw.get("ADB_SERIAL", "").strip(),
        alsa_device=raw.get("ALSA_DEVICE", "plughw:0,0").strip() or "plughw:0,0",
        capture_volume=_int_val("CAPTURE_VOLUME", 54, 0, 63),
        line_out_volume=_int_val("LINE_OUT_VOLUME", 45, 0, 87),
        hum_filter_enabled=hum_raw in ("1", "true", "yes", "on"),
    )


def sign_session_token(secret: str, now_ts: float | None = None) -> str:
    """Create an HMAC-SHA256 signed session cookie token."""
    exp = int((time.time() if now_ts is None else now_ts) + SESSION_TTL_SECONDS)
    sig = hmac.new(secret.encode("utf-8"), str(exp).encode("utf-8"), hashlib.sha256).hexdigest()
    return f"{exp}.{sig}"


def verify_session_token(token: str | None, secret: str, now_ts: float | None = None) -> bool:
    """Validate expiration and HMAC signature in constant time."""
    if not token or not secret or "." not in token:
        return False
    exp_str, sig = token.split(".", 1)
    if not exp_str.isdigit():
        return False
    expected = hmac.new(
        secret.encode("utf-8"), exp_str.encode("utf-8"), hashlib.sha256
    ).hexdigest()
    if not hmac.compare_digest(sig, expected):
        return False
    ts = time.time() if now_ts is None else now_ts
    return int(exp_str) > ts


def validate_and_encode_tel(raw_number: str) -> tuple[str, str]:
    """Validate dialable characters and encode '#' for Android tel: URI."""
    cleaned = re.sub(r"[\s().-]+", "", raw_number.strip())
    if not DIAL_NUMBER_RE.fullmatch(cleaned):
        raise ValueError("Invalid phone number (allowed digits, +, *, #, comma, semicolon)")
    return cleaned, cleaned.replace("#", "%23")


class HumFilter180Hz:
    """Stateful 1st-order ~183 Hz high-pass IIR filter for 16 kHz mono Int16 PCM."""

    def __init__(self, alpha: float = HPF_ALPHA) -> None:
        self.alpha = alpha
        self.prev_x = 0.0
        self.prev_y = 0.0

    def reset(self) -> None:
        self.prev_x = 0.0
        self.prev_y = 0.0

    def process_sample(self, x: int) -> int:
        y = self.alpha * (self.prev_y + x - self.prev_x)
        self.prev_x = float(x)
        self.prev_y = y
        if y > 32767.0:
            return 32767
        if y < -32768.0:
            return -32768
        return int(y)


def dsp_stereo_to_mono_hpf(
    stereo_bytes: bytes, hpf: HumFilter180Hz | None = None
) -> bytes:
    """Downmix S16_LE stereo PCM to mono using (L + R) >> 1 and optional HPF."""
    stereo = array("h")
    usable = len(stereo_bytes) - (len(stereo_bytes) % 4)
    stereo.frombytes(stereo_bytes[:usable])
    mono = array("h")
    for idx in range(0, len(stereo), 2):
        sample = (stereo[idx] + stereo[idx + 1]) >> 1
        if hpf is not None:
            sample = hpf.process_sample(sample)
        mono.append(sample)
    return mono.tobytes()


def dsp_mono_to_stereo(mono_bytes: bytes) -> bytes:
    """Upmix S16_LE mono PCM to stereo by duplicating each sample to L and R."""
    mono = array("h")
    usable = len(mono_bytes) - (len(mono_bytes) % 2)
    mono.frombytes(mono_bytes[:usable])
    stereo = array("h")
    for sample in mono:
        stereo.append(sample)
        stereo.append(sample)
    return stereo.tobytes()


def _adb_cmd(serial: str, *adb_args: str) -> list[str]:
    return (["adb", "-s", serial] if serial else ["adb", "-d"]) + list(adb_args)


async def run_adb(serial: str, *adb_args: str, timeout: float = 8.0) -> tuple[int, str, str]:
    proc = await asyncio.create_subprocess_exec(
        *_adb_cmd(serial, *adb_args),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        out_b, err_b = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return -1, "", f"adb timed out after {timeout}s"
    return (
        proc.returncode or 0,
        out_b.decode("utf-8", errors="replace"),
        err_b.decode("utf-8", errors="replace"),
    )


async def apply_alc897_mixer(capture_vol: int, line_out_vol: int) -> None:
    """Configure ALC897 Rear Blue (Line In) and Rear Green (Line Out) mixer levels via explicit numid cset."""
    cap_str = f"{capture_vol},{capture_vol}"
    out_str = f"{line_out_vol},{line_out_vol}"
    cmds = (
        ("amixer", "-c", "0", "cset", "numid=20", "2"),  # Input Source index 0 -> Line (Rear Blue)
        ("amixer", "-c", "0", "cset", "numid=21", "2"),  # Input Source index 1 -> Line (Rear Blue)
        ("amixer", "-c", "0", "cset", "numid=28", "0,0"),  # Line Boost Volume -> 0 dB
        ("amixer", "-c", "0", "cset", "numid=23", "on,on"),  # Capture Switch index 0 -> on
        ("amixer", "-c", "0", "cset", "numid=22", cap_str),  # Capture Volume index 0
        ("amixer", "-c", "0", "cset", "numid=25", "on,on"),  # Capture Switch index 1 -> on
        ("amixer", "-c", "0", "cset", "numid=24", cap_str),  # Capture Volume index 1
        ("amixer", "-c", "0", "cset", "numid=19", "0"),  # Auto-Mute Mode -> Disabled
        ("amixer", "-c", "0", "cset", "numid=30", "on"),  # Master Playback Switch -> on
        ("amixer", "-c", "0", "cset", "numid=29", "72"),  # Master Playback Volume
        ("amixer", "-c", "0", "cset", "numid=3", "on,on"),  # Front Playback Switch (Rear Green) -> on
        ("amixer", "-c", "0", "cset", "numid=2", out_str),  # Front Playback Volume (Rear Green)
    )
    for cmd in cmds:
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL
        )
        await proc.wait()


class AlsaAudioBridge:
    """On-demand bidirectional 16 kHz PCM bridge between ALSA and WebSocket clients."""

    def __init__(self, config: CallerConfig) -> None:
        self.config = config
        self.active = False
        self.hum_filter_enabled = config.hum_filter_enabled
        self.capture_volume = config.capture_volume
        self.line_out_volume = config.line_out_volume
        self.hpf = HumFilter180Hz()
        self.clients: set[WebSocket] = set()
        self.tx_frames = 0
        self.tx_peak = 0
        self.rx_frames = 0
        self.rx_peak = 0
        self._lock = asyncio.Lock()
        self._rec_proc: asyncio.subprocess.Process | None = None
        self._play_proc: asyncio.subprocess.Process | None = None
        self._play_queue: asyncio.Queue[bytes] | None = None
        self._tasks: list[asyncio.Task[Any]] = []

    async def start(self) -> None:
        async with self._lock:
            if self.active:
                return
            await apply_alc897_mixer(self.capture_volume, self.line_out_volume)
            self.hpf.reset()
            self.tx_frames = 0
            self.tx_peak = 0
            self.rx_frames = 0
            self.rx_peak = 0
            self._play_queue = asyncio.Queue(maxsize=12)

            self._rec_proc = await asyncio.create_subprocess_exec(
                "arecord",
                "-q",
                "-D",
                self.config.alsa_device,
                "-f",
                "S16_LE",
                "-r",
                str(SAMPLE_RATE),
                "-c",
                "2",
                "--buffer-size=3200",
                "--period-size=640",
                "-t",
                "raw",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            self._play_proc = await asyncio.create_subprocess_exec(
                "aplay",
                "-q",
                "-D",
                self.config.alsa_device,
                "-f",
                "S16_LE",
                "-r",
                str(SAMPLE_RATE),
                "-c",
                "2",
                "--buffer-size=3200",
                "--period-size=640",
                "-t",
                "raw",
                stdin=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            self.active = True
            self._tasks = [
                asyncio.create_task(self._capture_loop(), name="alsa-capture"),
                asyncio.create_task(self._playback_loop(), name="alsa-playback"),
                asyncio.create_task(self._stats_loop(), name="alsa-stats"),
            ]
            # Re-apply ALC897 mixer after PCM devices open so WirePlumber cannot revert Input Source to Rear Mic
            await asyncio.sleep(0.25)
            await apply_alc897_mixer(self.capture_volume, self.line_out_volume)
            print(
                f"AlsaAudioBridge started (device={self.config.alsa_device}, "
                f"capture={self.capture_volume}, out={self.line_out_volume}, hpf={self.hum_filter_enabled})",
                flush=True,
            )

    async def stop(self) -> None:
        async with self._lock:
            if not self.active and not self._rec_proc and not self._play_proc:
                return
            self.active = False
            for t in self._tasks:
                t.cancel()
            for t in self._tasks:
                with suppress(asyncio.CancelledError):
                    await t
            self._tasks.clear()

            for proc in (self._rec_proc, self._play_proc):
                if proc and proc.returncode is None:
                    with suppress(ProcessLookupError):
                        proc.terminate()
                    try:
                        await asyncio.wait_for(proc.wait(), timeout=2.0)
                    except asyncio.TimeoutError:
                        with suppress(ProcessLookupError):
                            proc.kill()
                        await proc.wait()
            self._rec_proc = None
            self._play_proc = None
            self._play_queue = None
            print("AlsaAudioBridge stopped.", flush=True)

    def enqueue_tx_pcm(self, mono_pcm: bytes) -> None:
        if not self.active or self._play_queue is None:
            return
        if len(mono_pcm) < 2 or len(mono_pcm) > 16384 or (len(mono_pcm) % 2) != 0:
            return
        if self._play_queue.full():
            with suppress(asyncio.QueueEmpty):
                self._play_queue.get_nowait()
        with suppress(asyncio.QueueFull):
            self._play_queue.put_nowait(mono_pcm)

    async def _capture_loop(self) -> None:
        assert self._rec_proc is not None and self._rec_proc.stdout is not None
        try:
            while self.active:
                try:
                    chunk = await self._rec_proc.stdout.readexactly(STEREO_CHUNK_BYTES)
                except asyncio.IncompleteReadError:
                    break
                hpf = self.hpf if self.hum_filter_enabled else None
                mono_bytes = dsp_stereo_to_mono_hpf(chunk, hpf)
                rx_arr = array("h")
                rx_arr.frombytes(mono_bytes)
                rx_p = max((abs(s) for s in rx_arr), default=0)
                self.rx_frames += 1
                if rx_p > self.rx_peak:
                    self.rx_peak = rx_p
                if not self.clients:
                    continue
                dead: list[WebSocket] = []
                for ws in list(self.clients):
                    try:
                        await ws.send_bytes(mono_bytes)
                    except Exception:
                        dead.append(ws)
                for ws in dead:
                    self.clients.discard(ws)
        except asyncio.CancelledError:
            raise

    async def _playback_loop(self) -> None:
        assert self._play_proc is not None and self._play_proc.stdin is not None
        assert self._play_queue is not None
        try:
            while self.active:
                mono_bytes = await self._play_queue.get()
                tx_arr = array("h")
                usable = len(mono_bytes) - (len(mono_bytes) % 2)
                tx_arr.frombytes(mono_bytes[:usable])
                tx_p = max((abs(s) for s in tx_arr), default=0)
                self.tx_frames += 1
                if tx_p > self.tx_peak:
                    self.tx_peak = tx_p
                stereo_bytes = dsp_mono_to_stereo(mono_bytes)
                try:
                    self._play_proc.stdin.write(stereo_bytes)
                    await self._play_proc.stdin.drain()
                except (BrokenPipeError, ConnectionResetError, OSError):
                    break
        except asyncio.CancelledError:
            raise

    async def _stats_loop(self) -> None:
        try:
            while self.active:
                await asyncio.sleep(2.0)
                if not self.active:
                    break
                # Continuously guard ALC897 Input Source = Line (2) against PipeWire/WirePlumber resets
                await apply_alc897_mixer(self.capture_volume, self.line_out_volume)
                tx_db = (
                    20.0 * math.log10(self.tx_peak / 32768.0)
                    if self.tx_peak > 0
                    else -120.0
                )
                rx_db = (
                    20.0 * math.log10(self.rx_peak / 32768.0)
                    if self.rx_peak > 0
                    else -120.0
                )
                print(
                    f"BRIDGE_STATS tx_frames={self.tx_frames} tx_peak={tx_db:.1f}dBFS "
                    f"rx_frames={self.rx_frames} rx_peak={rx_db:.1f}dBFS",
                    flush=True,
                )
                self.tx_frames = 0
                self.tx_peak = 0
                self.rx_frames = 0
                self.rx_peak = 0
        except asyncio.CancelledError:
            raise


class CallManager:
    """Monitors ADB call/jack state and synchronizes browser UI & ALSA audio bridge."""

    def __init__(
        self, config: CallerConfig, history_path: Path = DEFAULT_HISTORY_PATH
    ) -> None:
        self.config = config
        self.history_path = history_path
        self.bridge = AlsaAudioBridge(config)
        self.control_clients: set[WebSocket] = set()

        self.adb_connected = False
        self.carrier = "Unknown"
        self.voice_state = "unknown"
        self.signal_dbm: int | None = None
        self.jack_mode = "unplugged"
        self.call_state = 0
        self.remote_number = ""
        self.call_direction = ""
        self.call_started_ts: float | None = None
        self.ring_started_ts: float | None = None
        self.manual_monitor = False
        self.dial_pending_until = 0.0
        self.recent_calls: list[dict[str, Any]] = self._load_history()

        self._poll_task: asyncio.Task[Any] | None = None
        self._stop_event = asyncio.Event()
        self._action_lock = asyncio.Lock()

    def _load_history(self) -> list[dict[str, Any]]:
        if not self.history_path.exists():
            return []
        try:
            data = json.loads(self.history_path.read_text(encoding="utf-8"))
            return list(data)[:30] if isinstance(data, list) else []
        except (OSError, json.JSONDecodeError):
            return []

    def _save_history(self) -> None:
        tmp = self.history_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(self.recent_calls[:30], indent=2), encoding="utf-8")
        tmp.replace(self.history_path)

    def snapshot_dict(self) -> dict[str, Any]:
        return {
            "type": "state",
            "adb_connected": self.adb_connected,
            "carrier": self.carrier,
            "voice_state": self.voice_state,
            "signal_dbm": self.signal_dbm,
            "jack_mode": self.jack_mode,
            "call_state": self.call_state,
            "remote_number": self.remote_number,
            "call_direction": self.call_direction,
            "call_started_ts": self.call_started_ts,
            "audio_bridge_active": self.bridge.active,
            "manual_monitor": self.manual_monitor,
            "capture_volume": self.bridge.capture_volume,
            "line_out_volume": self.bridge.line_out_volume,
            "hum_filter": self.bridge.hum_filter_enabled,
            "recent_calls": self.recent_calls[:20],
        }

    async def broadcast_state(self) -> None:
        if not self.control_clients:
            return
        payload = self.snapshot_dict()
        dead: list[WebSocket] = []
        for ws in list(self.control_clients):
            try:
                await ws.send_json(payload)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.control_clients.discard(ws)

    async def sync_bridge(self) -> None:
        if not self.bridge.clients:
            self.manual_monitor = False
        should_run = (
            self.call_state == 2
            or self.manual_monitor
            or time.time() < self.dial_pending_until
        ) and bool(self.bridge.clients)
        if should_run and not self.bridge.active:
            await self.bridge.start()
            await self.broadcast_state()
        elif not should_run and self.bridge.active:
            await self.bridge.stop()
            await self.broadcast_state()

    async def start(self) -> None:
        self._stop_event.clear()
        self._poll_task = asyncio.create_task(self._poll_loop(), name="adb-poll")

    async def stop(self) -> None:
        self._stop_event.set()
        if self._poll_task:
            self._poll_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._poll_task
        await self.bridge.stop()

    async def _restore_wallpanel_later(self) -> None:
        await asyncio.sleep(1.5)
        rc, out, _ = await run_adb(self.config.adb_serial, "shell", "dumpsys power")
        if rc == 0 and "mWakefulness=Awake" in out:
            await run_adb(
                self.config.adb_serial, "shell", f"am start -n {WALLPANEL_ACTIVITY}"
            )

    async def _poll_once(self) -> None:
        rc, out, _ = await run_adb(
            self.config.adb_serial,
            "shell",
            "dumpsys telephony.registry; echo ===AUDIO_SPLIT===; dumpsys audio",
        )
        if rc != 0 or "===AUDIO_SPLIT===" not in out:
            changed = self.adb_connected
            self.adb_connected = False
            if changed:
                await self.broadcast_state()
            return

        tel_raw, audio_raw = out.split("===AUDIO_SPLIT===", 1)
        tel = sim_notifier.parse_telephony(tel_raw)
        m_main = re.search(r"mMainType=(0x[0-9a-fA-F]+)", audio_raw)
        main_hex = m_main.group(1).lower() if m_main else "0x0"
        jack_mode = (
            "headset"
            if main_hex == "0x1"
            else ("headphone" if main_hex == "0x2" else "unplugged")
        )

        new_state = int(tel["call_state"])
        incoming_num = str(tel["incoming_number"] or "").strip()
        prev_state = self.call_state
        now_ts = time.time()

        changed = (
            not self.adb_connected
            or self.carrier != str(tel["carrier"])
            or self.voice_state != str(tel["voice_state"])
            or self.signal_dbm != tel["signal_dbm"]
            or self.jack_mode != jack_mode
            or new_state != prev_state
        )

        self.adb_connected = True
        self.carrier = str(tel["carrier"])
        self.voice_state = str(tel["voice_state"])
        self.signal_dbm = tel["signal_dbm"]  # type: ignore[assignment]
        self.jack_mode = jack_mode

        if new_state == 1:
            if prev_state != 1:
                self.call_direction = "incoming"
                self.ring_started_ts = now_ts
                self.call_started_ts = None
            if incoming_num and incoming_num != self.remote_number:
                self.remote_number = incoming_num
                changed = True
        elif new_state == 2:
            if prev_state != 2:
                self.call_started_ts = now_ts
                if not self.call_direction:
                    self.call_direction = "outgoing"
                if incoming_num and not self.remote_number:
                    self.remote_number = incoming_num
            elif incoming_num and not self.remote_number:
                self.remote_number = incoming_num
                changed = True
        elif new_state == 0 and prev_state in (1, 2):
            dur = (
                max(0, int(now_ts - self.call_started_ts))
                if self.call_started_ts
                else 0
            )
            status = "answered" if prev_state == 2 else "missed"
            if self.call_direction == "outgoing" and prev_state == 2:
                status = "outgoing"
            self.recent_calls.insert(
                0,
                {
                    "id": f"{int(now_ts * 1000)}",
                    "number": self.remote_number or "Unknown",
                    "direction": self.call_direction or "incoming",
                    "status": status,
                    "ts": now_ts,
                    "duration_s": dur,
                },
            )
            self.recent_calls = self.recent_calls[:30]
            self._save_history()
            self.remote_number = ""
            self.call_direction = ""
            self.call_started_ts = None
            self.ring_started_ts = None
            self.dial_pending_until = 0.0
            asyncio.create_task(self._restore_wallpanel_later())

        self.call_state = new_state
        await self.sync_bridge()
        if changed:
            await self.broadcast_state()

    async def _poll_loop(self) -> None:
        try:
            while not self._stop_event.is_set():
                await self._poll_once()
                delay = 0.8 if (self.control_clients or self.call_state != 0) else 2.5
                try:
                    await asyncio.wait_for(self._stop_event.wait(), timeout=delay)
                except asyncio.TimeoutError:
                    pass
        except asyncio.CancelledError:
            raise

    async def dial(self, raw_number: str) -> str:
        cleaned, encoded = validate_and_encode_tel(raw_number)
        async with self._action_lock:
            self.call_direction = "outgoing"
            self.remote_number = cleaned
            self.dial_pending_until = time.time() + 8.0
            await self.sync_bridge()
            rc, _, err = await run_adb(
                self.config.adb_serial,
                "shell",
                f'am start -a android.intent.action.CALL -d "tel:{encoded}"',
            )
            if rc != 0:
                raise RuntimeError(err.strip() or "Failed to initiate call via ADB")
            await asyncio.sleep(0.5)
            await self._poll_once()
            await self.broadcast_state()
        return cleaned

    async def answer(self) -> None:
        async with self._action_lock:
            await run_adb(self.config.adb_serial, "shell", "input keyevent 5")
            await asyncio.sleep(0.8)
            await self._poll_once()
            if self.call_state == 1:
                await run_adb(self.config.adb_serial, "shell", "input keyevent 79")
                await asyncio.sleep(0.6)
                await self._poll_once()

    async def hangup(self) -> None:
        async with self._action_lock:
            await run_adb(self.config.adb_serial, "shell", "input keyevent 6")
            await asyncio.sleep(0.5)
            await self._poll_once()

    async def dtmf(self, digit: str) -> None:
        keycode = DTMF_KEYCODES.get(digit)
        if keycode is None:
            raise ValueError("Invalid DTMF digit")
        await run_adb(self.config.adb_serial, "shell", f"input keyevent {keycode}")

    async def set_audio_test(self, enable: bool) -> None:
        self.manual_monitor = bool(enable)
        await self.sync_bridge()
        await self.broadcast_state()

    async def update_mixer(
        self,
        capture_volume: int | None = None,
        line_out_volume: int | None = None,
        hum_filter: bool | None = None,
    ) -> None:
        if capture_volume is not None:
            self.bridge.capture_volume = max(0, min(63, int(capture_volume)))
        if line_out_volume is not None:
            self.bridge.line_out_volume = max(0, min(87, int(line_out_volume)))
        if hum_filter is not None:
            self.bridge.hum_filter_enabled = bool(hum_filter)
        await apply_alc897_mixer(
            self.bridge.capture_volume, self.bridge.line_out_volume
        )
        await self.broadcast_state()


def build_app(config: CallerConfig) -> FastAPI:
    manager = CallManager(config)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        await manager.start()
        try:
            yield
        finally:
            await manager.stop()

    app = FastAPI(title="H0MES SIM IP Caller", lifespan=lifespan)
    app.state.manager = manager
    app.state.config = config

    def _is_authed(request: Request) -> bool:
        return verify_session_token(
            request.cookies.get(COOKIE_NAME), config.session_secret
        )

    def _require_auth(request: Request) -> None:
        if not _is_authed(request):
            raise HTTPException(status_code=401, detail="Authentication required")

    @app.get("/")
    async def index() -> FileResponse:
        return FileResponse(
            STATIC_INDEX_PATH,
            media_type="text/html",
            headers={"Cache-Control": "no-store"},
        )

    @app.get("/api/health")
    async def health(request: Request) -> JSONResponse:
        return JSONResponse({"ok": True, "authenticated": _is_authed(request)})

    @app.post("/api/login")
    async def login(request: Request, response: Response) -> JSONResponse:
        body = await request.json()
        pin = str(body.get("pin", "")).strip()
        if not hmac.compare_digest(pin, config.caller_pin):
            raise HTTPException(status_code=401, detail="Incorrect PIN")
        token = sign_session_token(config.session_secret)
        resp = JSONResponse({"ok": True})
        resp.set_cookie(
            key=COOKIE_NAME,
            value=token,
            max_age=SESSION_TTL_SECONDS,
            httponly=True,
            secure=True,
            samesite="strict",
            path="/",
        )
        return resp

    @app.post("/api/logout")
    async def logout() -> JSONResponse:
        resp = JSONResponse({"ok": True})
        resp.delete_cookie(key=COOKIE_NAME, path="/")
        return resp

    @app.get("/api/status")
    async def api_status(request: Request) -> JSONResponse:
        _require_auth(request)
        return JSONResponse(manager.snapshot_dict())

    @app.post("/api/call/dial")
    async def api_dial(request: Request) -> JSONResponse:
        _require_auth(request)
        body = await request.json()
        try:
            cleaned = await manager.dial(str(body.get("number", "")))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        return JSONResponse({"ok": True, "number": cleaned})

    @app.post("/api/call/answer")
    async def api_answer(request: Request) -> JSONResponse:
        _require_auth(request)
        await manager.answer()
        return JSONResponse({"ok": True})

    @app.post("/api/call/hangup")
    async def api_hangup(request: Request) -> JSONResponse:
        _require_auth(request)
        await manager.hangup()
        return JSONResponse({"ok": True})

    @app.post("/api/call/dtmf")
    async def api_dtmf(request: Request) -> JSONResponse:
        _require_auth(request)
        body = await request.json()
        try:
            await manager.dtmf(str(body.get("digit", "")))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return JSONResponse({"ok": True})

    @app.post("/api/audio/test")
    async def api_audio_test(request: Request) -> JSONResponse:
        _require_auth(request)
        body = await request.json()
        await manager.set_audio_test(bool(body.get("enable", False)))
        return JSONResponse({"ok": True})

    @app.post("/api/audio/mixer")
    async def api_audio_mixer(request: Request) -> JSONResponse:
        _require_auth(request)
        body = await request.json()
        await manager.update_mixer(
            capture_volume=body.get("capture_volume"),
            line_out_volume=body.get("line_out_volume"),
            hum_filter=body.get("hum_filter"),
        )
        return JSONResponse({"ok": True})

    @app.websocket("/ws/control")
    async def ws_control(websocket: WebSocket) -> None:
        token = websocket.cookies.get(COOKIE_NAME)
        if not verify_session_token(token, config.session_secret):
            await websocket.close(code=4401)
            return
        await websocket.accept()
        manager.control_clients.add(websocket)
        try:
            await websocket.send_json(manager.snapshot_dict())
            while True:
                msg = await websocket.receive_json()
                action = msg.get("action")
                try:
                    if action == "dial":
                        await manager.dial(str(msg.get("number", "")))
                    elif action == "answer":
                        await manager.answer()
                    elif action == "hangup":
                        await manager.hangup()
                    elif action == "dtmf":
                        await manager.dtmf(str(msg.get("digit", "")))
                    elif action == "audio_test":
                        await manager.set_audio_test(bool(msg.get("enable", False)))
                    elif action == "set_mixer":
                        await manager.update_mixer(
                            capture_volume=msg.get("capture_volume"),
                            line_out_volume=msg.get("line_out_volume"),
                            hum_filter=msg.get("hum_filter"),
                        )
                    elif action == "ping":
                        await websocket.send_json({"type": "pong"})
                except (ValueError, RuntimeError) as exc:
                    await websocket.send_json({"type": "error", "message": str(exc)})
        except WebSocketDisconnect:
            pass
        finally:
            manager.control_clients.discard(websocket)

    @app.websocket("/ws/audio")
    async def ws_audio(websocket: WebSocket) -> None:
        token = websocket.cookies.get(COOKIE_NAME)
        if not verify_session_token(token, config.session_secret):
            await websocket.close(code=4401)
            return
        await websocket.accept()
        manager.bridge.clients.add(websocket)
        await manager.sync_bridge()
        try:
            while True:
                pcm_bytes = await websocket.receive_bytes()
                manager.bridge.enqueue_tx_pcm(pcm_bytes)
        except WebSocketDisconnect:
            pass
        finally:
            manager.bridge.clients.discard(websocket)
            await manager.sync_bridge()

    return app


def _service_active_status() -> str:
    try:
        proc = subprocess.run(
            ["systemctl", "--user", "is-active", SERVICE_NAME],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        return (proc.stdout or proc.stderr or "unknown").strip() or "unknown"
    except OSError as exc:
        return f"unavailable ({exc})"


def cmd_run(args: argparse.Namespace) -> int:
    config = load_caller_config(args.config, ensure_secret=True)
    if not DEFAULT_CERT_PATH.exists() or not DEFAULT_KEY_PATH.exists():
        print(
            f"Missing TLS cert/key at {DEFAULT_CERT_PATH} and {DEFAULT_KEY_PATH}",
            file=sys.stderr,
        )
        return 1

    app = build_app(config)
    uv_cfg = uvicorn.Config(
        app,
        host=config.bind_host,
        port=config.https_port,
        ssl_keyfile=str(DEFAULT_KEY_PATH),
        ssl_certfile=str(DEFAULT_CERT_PATH),
        ws="wsproto",
        log_level="info",
    )
    server = uvicorn.Server(uv_cfg)
    print(
        f"Starting {SERVICE_NAME} on https://{config.bind_host}:{config.https_port} "
        f"(LAN https://192.168.x.200:{config.https_port})",
        flush=True,
    )
    server.run()
    return 0


def cmd_set_pin(args: argparse.Namespace) -> int:
    pin = args.pin.strip()
    if not re.fullmatch(r"[A-Za-z0-9]{4,32}", pin):
        print("PIN must be 4–32 alphanumeric characters.", file=sys.stderr)
        return 1
    new_secret = secrets.token_hex(32)
    sim_notifier.update_config_file(
        args.config,
        {"CALLER_PIN": pin, "SESSION_SECRET": new_secret},
        chmod_600=True,
    )
    print(f"Updated CALLER_PIN and rotated SESSION_SECRET in {args.config} (mode 600).")
    if _service_active_status() == "active":
        proc = subprocess.run(
            ["systemctl", "--user", "restart", SERVICE_NAME],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        if proc.returncode == 0:
            print(f"Restarted {SERVICE_NAME}.")
        else:
            print(
                f"Warning: failed to restart {SERVICE_NAME}: {proc.stderr.strip()}",
                file=sys.stderr,
            )
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    config = load_caller_config(args.config, ensure_secret=False)
    print(f"Service ({SERVICE_NAME}): {_service_active_status()}")
    print(f"LAN URL: https://192.168.x.200:{config.https_port}")
    print(f"Tailscale URL: https://<TAILSCALE_IP>:{config.https_port}")
    print(f"TLS cert: {DEFAULT_CERT_PATH} ({'present' if DEFAULT_CERT_PATH.exists() else 'missing'})")

    try:
        proc = subprocess.run(
            _adb_cmd(
                config.adb_serial,
                "shell",
                "dumpsys telephony.registry; echo ===AUDIO_SPLIT===; dumpsys audio",
            ),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=8,
            check=False,
        )
    except OSError as exc:
        print(f"ADB status: ERROR ({exc})", file=sys.stderr)
        return 1

    if proc.returncode != 0 or "===AUDIO_SPLIT===" not in proc.stdout:
        print(f"ADB status: DISCONNECTED ({proc.stderr.strip()})", file=sys.stderr)
        return 1

    tel_raw, audio_raw = proc.stdout.split("===AUDIO_SPLIT===", 1)
    tel = sim_notifier.parse_telephony(tel_raw)
    m_main = re.search(r"mMainType=(0x[0-9a-fA-F]+)", audio_raw)
    main_hex = m_main.group(1).lower() if m_main else "0x0"
    jack = (
        "headset (audio+mic)"
        if main_hex == "0x1"
        else ("headphone (no mic)" if main_hex == "0x2" else "unplugged")
    )
    print("ADB status: CONNECTED")
    print(f"SIM carrier: {tel['carrier']} ({tel['voice_state']}), signal={tel['signal_dbm']} dBm")
    print(f"Call state: {sim_notifier.CALL_STATE_NAMES.get(int(tel['call_state']), tel['call_state'])}")
    print(f"3.5mm jack mode: {jack} ({main_hex})")
    return 0


def _rms_db(pcm_bytes: bytes) -> float:
    samples = array("h")
    samples.frombytes(pcm_bytes)
    if not samples:
        return -120.0
    mean_sq = sum(s * s for s in samples) / len(samples)
    return 20.0 * math.log10(math.sqrt(mean_sq) / 32768.0) if mean_sq > 0 else -120.0


def cmd_self_test(_args: argparse.Namespace) -> int:
    """Verify session HMAC tokens, phone validation/# encoding, and Python 3.14 PCM DSP."""
    secret = "test_secret_hex_123456"
    tok = sign_session_token(secret, now_ts=1000.0)
    assert verify_session_token(tok, secret, now_ts=1500.0)
    assert not verify_session_token(tok, "wrong_secret", now_ts=1500.0)
    assert not verify_session_token(tok, secret, now_ts=1000.0 + SESSION_TTL_SECONDS + 5)

    cleaned, encoded = validate_and_encode_tel(" *121# ")
    assert cleaned == "*121#" and encoded == "*121%23", (cleaned, encoded)
    cleaned2, encoded2 = validate_and_encode_tel("+880 1712-345678")
    assert cleaned2 == "+8801712345678" and encoded2 == "+8801712345678"
    for bad in ("", "123;rm -rf", "abc123"):
        try:
            validate_and_encode_tel(bad)
            raise AssertionError(f"Expected ValueError for {bad!r}")
        except ValueError:
            pass

    def _make_stereo(freq: float, frames: int = 1600) -> bytes:
        arr = array("h")
        amp = 10000
        for i in range(frames):
            val = int(amp * math.sin(2.0 * math.pi * freq * i / SAMPLE_RATE))
            arr.append(val)
            arr.append(val)
        return arr.tobytes()

    in_50 = _make_stereo(50.0)
    out_50 = dsp_stereo_to_mono_hpf(in_50, HumFilter180Hz())
    in_50_db = _rms_db(dsp_stereo_to_mono_hpf(in_50, None))
    out_50_db = _rms_db(out_50)
    atten_50 = in_50_db - out_50_db
    assert atten_50 > 10.0, f"50 Hz attenuation too small: {atten_50:.2f} dB"

    in_1k = _make_stereo(1000.0)
    out_1k = dsp_stereo_to_mono_hpf(in_1k, HumFilter180Hz())
    in_1k_db = _rms_db(dsp_stereo_to_mono_hpf(in_1k, None))
    out_1k_db = _rms_db(out_1k)
    assert abs(in_1k_db - out_1k_db) < 1.0, f"1 kHz altered too much: {in_1k_db:.2f} vs {out_1k_db:.2f}"

    stereo_round = dsp_mono_to_stereo(out_1k)
    assert len(stereo_round) == len(out_1k) * 2

    print("IP-CALLER SELF-TEST OK")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="SIM Web IP Caller HTTPS Server")
    parser.add_argument(
        "--config", type=Path, default=DEFAULT_CONFIG_PATH, help="Path to caller.env"
    )
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("run", help="Run the HTTPS/WebSocket IP Caller server (default)")
    sub.add_parser("status", help="Show service, HTTPS URLs, ADB, SIM, and jack status")
    sub.add_parser("self-test", help="Run auth, number encoding, and PCM DSP checks")
    sp = sub.add_parser("set-pin", help="Update CALLER_PIN and rotate session secret")
    sp.add_argument("pin", help="New 4-32 character PIN")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    cmd = args.command or "run"
    if cmd == "run":
        return cmd_run(args)
    if cmd == "status":
        return cmd_status(args)
    if cmd == "set-pin":
        return cmd_set_pin(args)
    if cmd == "self-test":
        return cmd_self_test(args)
    parser.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
