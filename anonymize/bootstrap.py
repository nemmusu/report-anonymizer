"""First-run bootstrap helpers.

Materialize the default model file (``Qwen3.5-4B.Q4_K_M.gguf``, the
shipped default preset, Jackrong Claude-Opus distill of Qwen 3.5
4B, smallest GGUF that still hits Q=78 on the bench corpus) into
the global models directory by symlinking (or copying as fallback)
from a user-provided source directory.

Search order:

1. ``DOCUMENT_ANONYMIZER_MODEL_SOURCE`` env var (if set), interpreted
   as a colon-separated list of directories.
2. ``~/.local/share/document-anonymizer/import/`` (drop-in dir users
   can populate without touching the GUI).

If no source contains the file, the function returns ``False`` and
the user is expected to download the model via the Model Manager
dialog.
"""
from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Iterable, Optional

from ._paths import user_data_dir as _user_data_dir
from .server_profile import MODELS_DIR


def _env_sources() -> list[Path]:
    raw = os.environ.get("DOCUMENT_ANONYMIZER_MODEL_SOURCE", "")
    return [Path(p).expanduser() for p in raw.split(os.pathsep) if p.strip()]


_KNOWN_SOURCES: list[Path] = [
    *_env_sources(),
    _user_data_dir() / "import",
]

_DEFAULT_FILES: list[str] = [
    "Qwen3.5-4B.Q4_K_M.gguf",
]


def _try_link_or_copy(src: Path, dst: Path) -> bool:
    if dst.exists():
        return True
    dst.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.symlink(src, dst)
        return True
    except OSError:
        try:
            shutil.copy2(src, dst)
            return True
        except Exception:
            return False
    except Exception:
        return False


def materialize_default_model(
    *,
    files: Optional[Iterable[str]] = None,
    sources: Optional[Iterable[Path]] = None,
) -> dict[str, bool]:
    """Materialize default model files into ``MODELS_DIR``.

    Returns a dict ``{filename: success}`` for each requested file.
    """
    out: dict[str, bool] = {}
    target_files = list(files or _DEFAULT_FILES)
    candidates = list(sources or _KNOWN_SOURCES)
    MODELS_DIR.mkdir(parents=True, exist_ok=True)
    for fname in target_files:
        dst = MODELS_DIR / fname
        if dst.exists():
            out[fname] = True
            continue
        success = False
        for src_dir in candidates:
            src = src_dir / fname
            if src.exists():
                success = _try_link_or_copy(src, dst)
                if success:
                    break
        out[fname] = success
    return out


def is_first_run() -> bool:
    """Return True if no ``.bootstrapped`` sentinel exists in the user config."""
    from .server_profile import CONFIG_DIR

    return not (CONFIG_DIR / ".bootstrapped").exists()


def mark_bootstrapped() -> None:
    from .server_profile import CONFIG_DIR

    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    sentinel = CONFIG_DIR / ".bootstrapped"
    sentinel.write_text("ok\n", encoding="utf-8")


__all__ = [
    "materialize_default_model",
    "is_first_run",
    "mark_bootstrapped",
]
