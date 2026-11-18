"""Installs the optional transcription extra (faster-whisper, + CUDA libs if
requested) from inside the running app - see worker.py, which runs it on
whichever computer was asked to (Settings → Transcription & computers).
pip installing this can take a while (ctranslate2/av/tokenizers are
sizeable downloads).

1. ``pip install`` into the running interpreter - no restart needed.
2. Only on pip's "externally-managed-environment" error (PEP 668): install
   into ``venv_paths.VENV_DIR`` (``--system-site-packages``), which main.py
   re-execs into - this needs a restart.
Any other error is reported as-is.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Callable


from ..venv_paths import VENV_DIR, VENV_PYTHON
# What uninstalling removes - faster-whisper's own heavy parts; small shared
# helpers (huggingface_hub, numpy...) stay, other programs may use them.
_UNINSTALL_PACKAGES = ["faster-whisper", "ctranslate2", "av", "onnxruntime", "tokenizers",
                       "nvidia-cublas-cu12", "nvidia-cudnn-cu12", "nvidia-cuda-nvrtc-cu12"]


def _loaded() -> bool:
    """Has transcription run in this process? Windows then keeps its .pyd files locked."""
    return any(m in sys.modules for m in ("faster_whisper", "ctranslate2", "av", "onnxruntime", "tokenizers"))


# av<19: faster-whisper 1.2.1 passes av.open(metadata_errors=...), which PyAV
# 19 removed - every transcription failed. The wheels are abi3, no source build.
_PACKAGES = ["faster-whisper>=1.2", "av<19"]
_GPU_PACKAGES = ["nvidia-cublas-cu12", "nvidia-cudnn-cu12"]
_EXTERNALLY_MANAGED_MARKER = "externally-managed-environment"


def _run_streamed(cmd: list[str], on_line: Callable[[str], None]) -> tuple[bool, str]:
    on_line("$ " + " ".join(cmd))
    try:
        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, errors="replace", bufsize=1,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except OSError as exc:
        on_line(str(exc))
        return False, str(exc)
    lines: list[str] = []
    for line in proc.stdout:
        line = line.rstrip("\n")
        lines.append(line)
        on_line(line)
    proc.wait()
    return proc.returncode == 0, "\n".join(lines)


def _hf_cache_dir() -> Path:
    """Hugging Face's own cache location rules (huggingface_hub.constants), without importing it."""
    if os.environ.get("HF_HUB_CACHE"):
        return Path(os.environ["HF_HUB_CACHE"])
    home = os.environ.get("HF_HOME") or Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache") / "huggingface"
    return Path(home) / "hub"


# faster_whisper.utils._MODELS (1.2.1) where it isn't "Systran/faster-whisper-<name>".
_MODEL_REPOS = {
    "large": "Systran/faster-whisper-large-v3",
    "distil-large-v2": "Systran/faster-distil-whisper-large-v2",
    "distil-medium.en": "Systran/faster-distil-whisper-medium.en",
    "distil-small.en": "Systran/faster-distil-whisper-small.en",
    "distil-large-v3": "Systran/faster-distil-whisper-large-v3",
    "distil-large-v3.5": "distil-whisper/distil-large-v3.5-ct2",
    "large-v3-turbo": "mobiuslabsgmbh/faster-whisper-large-v3-turbo",
    "turbo": "mobiuslabsgmbh/faster-whisper-large-v3-turbo",
}


def _model_folder(model_id: str) -> Path:
    repo = model_id if "/" in model_id else _MODEL_REPOS.get(model_id, f"Systran/faster-whisper-{model_id}")
    return _hf_cache_dir() / ("models--" + repo.replace("/", "--"))


def model_cached(model_id: str) -> bool:
    """True when the model's weights are on this computer already (a folder given by path always is)."""
    if Path(model_id).is_dir():
        return True
    return any(_model_folder(model_id).glob("snapshots/*/model.bin"))


def model_download_bytes(model_id: str) -> int:
    """How much of a model is on disk so far - what a download in progress has fetched."""
    try:
        return sum(f.stat().st_size for f in _model_folder(model_id).rglob("*") if f.is_file() and not f.is_symlink())
    except OSError:
        return 0


def delete_models(model_ids: list[str], on_line: Callable[[str], None]) -> None:
    """Deletes the given Whisper models (faster-whisper names like "large-v3"
    or Hugging Face repo ids) from the Hugging Face cache - only these: the
    cache is shared with any other program on this computer. Never imports
    faster-whisper: an uninstall is often what fixes a broken one."""
    if "faster_whisper" in sys.modules:
        from .transcription_engine import unload_models
        unload_models()  # loaded models keep their files open
    cache = _hf_cache_dir()
    for model_id in model_ids:
        repo = model_id if "/" in model_id else _MODEL_REPOS.get(model_id, f"Systran/faster-whisper-{model_id}")
        folder = cache / ("models--" + repo.replace("/", "--"))
        if not folder.exists():
            on_line(f"model {repo}: not downloaded")
            continue
        size = sum(f.stat().st_size for f in folder.rglob("*") if f.is_file() and not f.is_symlink())
        shutil.rmtree(folder, ignore_errors=True)
        if folder.exists():
            on_line(f"model {repo}: some files are in use - delete {folder} by hand")
        else:
            on_line(f"deleted model {repo} ({size / 1e9:.1f} GB)")


def uninstall_transcription(model_ids: list[str], on_line: Callable[[str], None]) -> tuple[bool, bool, str]:
    """Removes what install_transcription() added, plus the given models
    (empty list = keep them). Returns (success, needs_restart, error_message)."""
    if model_ids:
        try:
            delete_models(model_ids, on_line)
        except Exception as exc:  # models are a bonus - never block removing the packages
            on_line(f"models: could not delete ({exc})")
    if sys.platform == "win32" and _loaded():
        message = ("Transcription has run in this session and Windows keeps its files locked - "
                   'turn off "Transcribe here", restart AliSDR, then uninstall.')
        on_line(message)
        return False, False, message
    python = str(VENV_PYTHON) if VENV_PYTHON.exists() else sys.executable
    ok, output = _run_streamed([python, "-m", "pip", "uninstall", "-y", *_UNINSTALL_PACKAGES], on_line)
    return ok, False, "" if ok else output


def install_transcription(with_gpu: bool, on_line: Callable[[str], None]) -> tuple[bool, bool, str]:
    """Returns (success, needs_restart, error_message - "" on success);
    every line of pip's output goes to ``on_line`` as it arrives."""
    packages = _PACKAGES + (_GPU_PACKAGES if with_gpu else [])

    def run_streamed(cmd: list[str]) -> tuple[bool, str]:
        return _run_streamed(cmd, on_line)

    def pip_install(python_exe: str) -> tuple[bool, str]:
        return run_streamed([python_exe, "-m", "pip", "install", "--upgrade", *packages])

    in_venv = Path(sys.prefix).resolve() == VENV_DIR.resolve()
    # An existing .venv means this machine already needed the fallback
    # once before - go straight there instead of re-discovering PEP 668.
    if VENV_PYTHON.exists():
        ok, output = pip_install(str(VENV_PYTHON))
        return ok, ok and not in_venv, "" if ok else output

    ok, output = pip_install(sys.executable)
    if ok:
        return True, False, ""
    if _EXTERNALLY_MANAGED_MARKER not in output:
        return False, False, output  # a real failure unrelated to PEP 668 - a venv wouldn't fix it
    on_line(
        "-- direct install blocked (externally-managed-environment) - "
        "creating a dedicated venv instead --"
    )
    ok, venv_output = run_streamed([sys.executable, "-m", "venv", "--system-site-packages", str(VENV_DIR)])
    if not ok:
        return False, False, venv_output
    ok, output = pip_install(str(VENV_PYTHON))
    return ok, ok, "" if ok else output
