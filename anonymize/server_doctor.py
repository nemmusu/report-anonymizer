"""Parse llama-server failure logs and suggest user-actionable fixes.

Used by the GUI to translate cryptic stderr into a friendly dialog with
fix buttons (e.g. "Switch to cpu_only", "Re-download model").
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class Diagnosis:
    cause: str  # cuda_oom | file_not_found | binary_not_found | port_in_use |
    #             unsupported_arch | mmproj_mismatch | permission_denied |
    #             unknown
    message: str
    suggested_actions: list[str] = field(default_factory=list)


_PATTERNS: list[tuple[str, re.Pattern[str], str, list[str]]] = [
    (
        "cuda_oom",
        re.compile(r"(out of memory|CUDA error.*out of memory|cudaErrorMemoryAllocation)", re.I),
        "Memoria GPU insufficiente per questo preset.",
        ["switch_preset:cpu_only", "open_preset_editor", "reduce_ngl"],
    ),
    (
        "file_not_found",
        re.compile(r"(no such file or directory|failed to open .* file|model file does not exist)", re.I),
        "Il file modello indicato non esiste sul disco.",
        ["redownload_model", "open_model_manager"],
    ),
    (
        "binary_not_found",
        re.compile(r"(llama-server.*not found|cannot execute binary|exec format error)", re.I),
        "Il binario llama-server non e' raggiungibile o non eseguibile.",
        ["browse_binary", "install_llama_cpp"],
    ),
    (
        "port_in_use",
        re.compile(r"(address already in use|bind:.*EADDRINUSE|port already)", re.I),
        "La porta scelta e' gia' occupata da un altro processo.",
        ["free_port", "change_port"],
    ),
    (
        "unsupported_arch",
        re.compile(r"(unknown model architecture|unsupported model type|unknown model arch)", re.I),
        "L'architettura del modello non e' supportata da questa versione di llama.cpp.",
        ["update_llama_cpp", "switch_preset:default"],
    ),
    (
        "mmproj_mismatch",
        re.compile(r"(mmproj.*mismatch|incompatible mmproj|wrong mmproj)", re.I),
        "Il file mmproj non corrisponde al modello selezionato.",
        ["redownload_model", "remove_mmproj"],
    ),
    (
        "permission_denied",
        re.compile(r"permission denied", re.I),
        "Permessi insufficienti su file/binario/porta.",
        ["browse_binary", "open_log"],
    ),
]


def diagnose(
    log_tail: str,
    *,
    profile: Optional[object] = None,
    last_returncode: Optional[int] = None,
) -> Diagnosis:
    text = log_tail or ""
    for cause, pat, message, actions in _PATTERNS:
        if pat.search(text):
            return Diagnosis(cause=cause, message=message, suggested_actions=list(actions))
    if last_returncode and last_returncode != 0:
        return Diagnosis(
            cause="unknown",
            message=f"Server uscito con codice {last_returncode}.",
            suggested_actions=["open_log", "switch_preset:cpu_only"],
        )
    return Diagnosis(
        cause="unknown",
        message="Causa non identificata. Controlla il log per dettagli.",
        suggested_actions=["open_log"],
    )


__all__ = ["Diagnosis", "diagnose"]
