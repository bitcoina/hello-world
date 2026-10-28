"""Local transcription. Audio never leaves the Pi and is deleted as soon as it's read."""

from __future__ import annotations

import importlib.util
import logging
import shutil
import subprocess
import tempfile
from pathlib import Path

from .config import Settings

log = logging.getLogger(__name__)


class VoiceUnavailable(Exception):
    pass


class Transcriber:
    def __init__(self, settings: Settings):
        self.settings = settings
        self._model = None
        self.engine = self._detect()

    def _detect(self) -> str | None:
        if self.settings.whisper_cpp_bin and self.settings.whisper_cpp_model:
            if Path(self.settings.whisper_cpp_bin).exists() and shutil.which("ffmpeg"):
                return "whisper.cpp"
            log.warning("whisper.cpp configured but binary or ffmpeg missing")
        return "faster-whisper" if importlib.util.find_spec("faster_whisper") else None

    @property
    def available(self) -> bool:
        return self.engine is not None

    def transcribe(self, path: Path) -> str:
        if self.engine == "faster-whisper":
            return self._faster_whisper(path)
        if self.engine == "whisper.cpp":
            return self._whisper_cpp(path)
        raise VoiceUnavailable("No transcription engine installed (pip install 'barometer[voice]')")

    def _faster_whisper(self, path: Path) -> str:
        from faster_whisper import WhisperModel

        if self._model is None:
            self._model = WhisperModel(self.settings.whisper_model, device="cpu", compute_type="int8")
        segments, _ = self._model.transcribe(str(path), beam_size=1, vad_filter=True)
        return " ".join(s.text.strip() for s in segments).strip()

    def _whisper_cpp(self, path: Path) -> str:
        with tempfile.TemporaryDirectory(dir=self.settings.spool_dir) as tmp:
            wav = Path(tmp) / "audio.wav"
            subprocess.run(
                ["ffmpeg", "-loglevel", "error", "-y", "-i", str(path), "-ar", "16000", "-ac", "1", str(wav)],
                check=True,
                timeout=300,
            )
            out = subprocess.run(
                [self.settings.whisper_cpp_bin, "-m", self.settings.whisper_cpp_model, "-f", str(wav), "-nt", "-np"],
                check=True,
                capture_output=True,
                text=True,
                timeout=600,
            )
            return " ".join(line.strip() for line in out.stdout.splitlines() if line.strip())


def shred(path: Path) -> None:
    """Overwrite then unlink. Best effort on SSDs, but it keeps casual recovery off the table."""
    try:
        size = path.stat().st_size
        with open(path, "r+b") as fh:
            fh.write(b"\0" * size)
            fh.flush()
        path.unlink()
    except FileNotFoundError:
        pass
