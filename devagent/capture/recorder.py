"""Local meeting recorder (macOS): ffmpeg + AVFoundation from an aggregate device that mixes your
mic with system audio (BlackHole). Nothing is injected into Teams — it only records your Mac's
own audio, like a screen recorder. Plus call detection and Whisper transcription.
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import signal
from datetime import datetime
from pathlib import Path

from ..config import Config
from ..gitops import sh

log = logging.getLogger("devagent.recorder")


class RecorderError(RuntimeError):
    pass


class Recorder:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.proc: asyncio.subprocess.Process | None = None
        self.path: Path | None = None
        self.meta: dict = {}

    @property
    def recording(self) -> bool:
        return self.proc is not None and self.proc.returncode is None

    def _dir(self) -> Path:
        d = Path(self.cfg.get("meetings.recordings_dir", "~/.devagent/recordings")).expanduser()
        d.mkdir(parents=True, exist_ok=True)
        os.chmod(d, 0o700)
        return d

    async def start(self, kind: str = "adhoc", title: str = "") -> Path:
        if self.recording:
            return self.path  # type: ignore
        stamp = datetime.now().strftime("%Y%m%d-%H%M")
        self.path = self._dir() / f"{stamp}-{kind}.wav"
        device = self.cfg.get("meetings.audio_device", "DevAgent Capture")
        self.proc = await asyncio.create_subprocess_exec(
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin",
            "-f", "avfoundation", "-i", f":{device}",
            "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(self.path),
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)
        await asyncio.sleep(1.5)
        if self.proc.returncode is not None:
            err = (await self.proc.stderr.read()).decode() if self.proc.stderr else ""
            self.proc = None
            raise RecorderError(f"ffmpeg could not open audio device '{device}': {err.strip()[:300]}")
        self.meta = {"kind": kind, "title": title or kind, "started": datetime.now()}
        log.info("recording → %s", self.path)
        return self.path

    async def stop(self) -> tuple[Path | None, dict]:
        if not self.recording:
            return None, {}
        assert self.proc
        self.proc.send_signal(signal.SIGINT)  # ffmpeg finalises the WAV header on SIGINT
        try:
            await asyncio.wait_for(self.proc.wait(), 15)
        except asyncio.TimeoutError:
            self.proc.kill()
        path, meta = self.path, self.meta
        self.proc, self.path, self.meta = None, None, {}
        return path, meta


async def active_call_apps(apps: list[str]) -> list[str]:
    """Apps currently holding an audio power assertion (≈ in a call). macOS only; heuristic."""
    r = await sh(["pmset", "-g", "assertions"], check=False)
    return await _apps_from_assertions(r.out, apps)


async def _apps_from_assertions(text: str, apps: list[str]) -> list[str]:
    # coreaudiod holds assertions "Created for PID: N" on behalf of the app using audio
    audio_lines = [ln for ln in text.splitlines() if "audio" in ln.lower() or "Created for PID" in ln]
    pids = set(re.findall(r"Created for PID: (\d+)", "\n".join(audio_lines)))
    pids |= {p for p, name in re.findall(r"pid (\d+)\(([^)]+)\)", text)
             if name != "coreaudiod" and any(a.lower() in name.lower() for a in apps)}
    if not pids:
        return []
    ps = await sh(["ps", "-o", "pid=,comm=", "-p", ",".join(sorted(pids))], check=False)
    found = []
    for line in ps.out.splitlines():
        comm = line.strip().split(None, 1)[-1] if line.strip() else ""
        for app in apps:
            if app.lower() in comm.lower() and app not in found:
                found.append(app)
    return found


async def transcribe(cfg: Config, wav: Path) -> str:
    binary = cfg.get("meetings.whisper.binary", "whisper-cli")
    model = str(Path(cfg.get("meetings.whisper.model")).expanduser())
    lang = cfg.get("meetings.whisper.language", "auto")
    out_base = wav.with_suffix("")
    threads = str(max(4, (os.cpu_count() or 8) - 2))
    r = await sh([binary, "-m", model, "-f", str(wav), "-l", lang, "-t", threads,
                  "-otxt", "-of", str(out_base), "-np"], check=False, timeout=3 * 3600)
    txt = out_base.with_suffix(".txt")
    if r.code != 0 or not txt.exists():
        raise RecorderError(f"whisper failed: {r.err.strip()[-400:]}")
    return txt.read_text().strip()
