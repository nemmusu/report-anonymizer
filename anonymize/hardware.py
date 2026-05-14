"""Hardware detection (GPU/CPU/RAM/Disk) + VRAM estimation for presets.

Pure-Python where possible (uses ``psutil`` for CPU/RAM/disk). For GPUs we
shell out to ``nvidia-smi`` / ``rocm-smi`` and parse the CSV output. On
macOS we read ``system_profiler SPDisplaysDataType``. On Windows, when
``nvidia-smi`` is unreachable (or the host has an AMD/Intel GPU and so
no NVIDIA tooling at all), we fall back to PowerShell
``Get-CimInstance Win32_VideoController`` and finally to legacy
``wmic``. All probes are defensive: any failure returns an empty list
and the GUI keeps working.
"""
from __future__ import annotations

import os
import platform
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

try:
    import psutil  # type: ignore
except Exception:  # pragma: no cover - psutil ships with the project
    psutil = None  # type: ignore


@dataclass
class GPU:
    name: str
    backend: str  # nvidia | amd | metal | unknown
    vram_total_mb: int
    vram_free_mb: int


@dataclass
class CPU:
    model: str
    physical_cores: int
    logical_cores: int


@dataclass
class Memory:
    total_mb: int
    available_mb: int


@dataclass
class Disk:
    path: str
    total_mb: int
    free_mb: int


@dataclass
class HardwareReport:
    gpus: list[GPU] = field(default_factory=list)
    cpu: Optional[CPU] = None
    memory: Optional[Memory] = None
    disk: Optional[Disk] = None
    os_name: str = ""
    os_release: str = ""

    def short(self) -> str:
        parts = []
        if self.gpus:
            g = self.gpus[0]
            # Some Windows fallbacks (PowerShell ``Win32_VideoController`` /
            # ``wmic``) cannot read accurate VRAM (the ``AdapterRAM``
            # column is a UInt32 that caps at ~4 GB and reports 0 for
            # modern cards). When the total comes back as 0, drop the
            # "(0GB)" suffix so the user sees a clean
            # "GPU: NVIDIA GeForce RTX 5090" instead of a confusing zero.
            if g.vram_total_mb > 0:
                parts.append(
                    f"GPU: {g.name} {g.vram_total_mb // 1024}GB "
                    f"({g.vram_free_mb // 1024}GB free)"
                )
            else:
                parts.append(f"GPU: {g.name}")
        else:
            parts.append("GPU: none")
        if self.cpu:
            parts.append(f"CPU: {self.cpu.physical_cores}c/{self.cpu.logical_cores}t")
        if self.memory:
            parts.append(f"RAM: {self.memory.total_mb // 1024}GB ({self.memory.available_mb // 1024}GB free)")
        if self.disk:
            parts.append(f"Disk: {self.disk.free_mb // 1024}GB free")
        return " · ".join(parts)


# ---- GPU detection ---------------------------------------------------------


def _subprocess_kwargs() -> dict:
    """Return platform-specific ``subprocess.run`` kwargs.

    On Windows we ALWAYS pass ``creationflags=CREATE_NO_WINDOW`` for
    every probe so that, when the GUI is launched via ``pythonw.exe``
    (no console attached), the helper child process does not pop a
    transient console window. Empirically that flash also correlates
    with ``nvidia-smi`` returning garbage / failing to start under
    the GUI subsystem on some driver versions, so the flag doubles as
    a stability fix.

    On non-Windows platforms ``CREATE_NO_WINDOW`` does not exist;
    return an empty dict so the caller's ``**_subprocess_kwargs()``
    spread is a no-op.
    """
    if os.name == "nt":
        # ``CREATE_NO_WINDOW`` is defined on Windows only. Guard the
        # attribute access so a hypothetical Wine / cross-compile
        # environment without it still degrades to "no flag".
        flag = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        if flag:
            return {"creationflags": flag}
    return {}


# Well-known ``nvidia-smi.exe`` install paths. ``shutil.which`` walks
# ``PATH`` + ``PATHEXT`` and is usually correct, but the Windows
# launcher (pythonw.exe spawned by ReportAnonymizer.exe) sometimes
# inherits a sanitised env where PATHEXT is empty / nvidia-smi was
# installed under a different casing, and the lookup returns ``None``.
# These two paths cover modern NVIDIA drivers (System32) and the
# legacy GeForce/Quadro install location still used on some hosts.
_WINDOWS_NVSMI_FALLBACKS: tuple[str, ...] = (
    r"%WINDIR%\System32\nvidia-smi.exe",
    r"C:\Program Files\NVIDIA Corporation\NVSMI\nvidia-smi.exe",
)


def _resolve_nvidia_smi() -> Optional[str]:
    """Return an absolute path to ``nvidia-smi``, or ``None``.

    First trusts ``shutil.which``; on Windows, if that fails, probes
    the well-known install locations explicitly (see
    :data:`_WINDOWS_NVSMI_FALLBACKS`).
    """
    smi = shutil.which("nvidia-smi")
    if smi:
        return smi
    if os.name != "nt":
        return None
    for raw in _WINDOWS_NVSMI_FALLBACKS:
        try:
            candidate = Path(os.path.expandvars(raw))
            if candidate.exists():
                return str(candidate)
        except Exception:
            continue
    return None


def _detect_nvidia() -> list[GPU]:
    smi = _resolve_nvidia_smi()
    if not smi:
        return []
    try:
        out = subprocess.run(
            [
                smi,
                "--query-gpu=name,memory.total,memory.free",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True,
            text=True,
            timeout=5,
            **_subprocess_kwargs(),
        )
        if out.returncode != 0:
            return []
    except Exception:
        return []
    gpus: list[GPU] = []
    for line in out.stdout.splitlines():
        parts = [x.strip() for x in line.split(",")]
        if len(parts) < 3:
            continue
        try:
            gpus.append(
                GPU(
                    name=parts[0],
                    backend="nvidia",
                    vram_total_mb=int(float(parts[1])),
                    vram_free_mb=int(float(parts[2])),
                )
            )
        except Exception:
            continue
    return gpus


def _detect_amd() -> list[GPU]:
    smi = shutil.which("rocm-smi")
    if not smi:
        return []
    try:
        out = subprocess.run(
            [smi, "--showproductname", "--showmeminfo", "vram", "--csv"],
            capture_output=True,
            text=True,
            timeout=5,
            **_subprocess_kwargs(),
        )
        if out.returncode != 0:
            return []
    except Exception:
        return []
    gpus: list[GPU] = []
    for line in out.stdout.splitlines()[1:]:
        parts = [x.strip() for x in line.split(",")]
        if len(parts) < 3:
            continue
        try:
            total_b = int(parts[1])
            used_b = int(parts[2]) if len(parts) >= 3 else 0
            gpus.append(
                GPU(
                    name=parts[0] or "AMD GPU",
                    backend="amd",
                    vram_total_mb=total_b // (1024 * 1024),
                    vram_free_mb=max(0, (total_b - used_b) // (1024 * 1024)),
                )
            )
        except Exception:
            continue
    return gpus


def _detect_metal() -> list[GPU]:
    if platform.system() != "Darwin":
        return []
    sp = shutil.which("system_profiler")
    if not sp:
        return []
    try:
        out = subprocess.run(
            [sp, "SPDisplaysDataType"],
            capture_output=True,
            text=True,
            timeout=10,
            **_subprocess_kwargs(),
        )
        if out.returncode != 0:
            return []
    except Exception:
        return []
    name = "Apple GPU"
    vram = 0
    for line in out.stdout.splitlines():
        line = line.strip()
        if line.startswith("Chipset Model:"):
            name = line.split(":", 1)[1].strip() or name
        if line.startswith("VRAM"):
            m = re.search(r"(\d+)\s*(MB|GB)", line)
            if m:
                v = int(m.group(1))
                vram = v * (1024 if m.group(2) == "GB" else 1)
    return [
        GPU(name=name, backend="metal", vram_total_mb=vram, vram_free_mb=vram)
    ]


def _classify_windows_gpu_backend(name: str) -> str:
    """Map a Windows ``Win32_VideoController`` ``Name`` to a backend.

    The classification is keyword-based and intentionally permissive:
    OEM rebranded names (e.g. "NVIDIA GeForce RTX 4090 Founders
    Edition") still match the "NVIDIA"/"RTX" keywords. Returns
    ``"unknown"`` when nothing recognisable matches so the GUI can
    still show the GPU name without a misleading backend.
    """
    n = name.lower()
    if any(k in n for k in ("nvidia", "geforce", "quadro", "tesla", "rtx", "gtx")):
        return "nvidia"
    if any(k in n for k in ("amd", "radeon")):
        return "amd"
    if "intel" in n and any(
        k in n
        for k in ("arc", "iris xe", "xe graphics", "uhd graphics 7")
    ):
        return "intel"
    return "unknown"


def _vram_mb_from_adapter_ram(raw: str) -> int:
    """Parse an ``AdapterRAM`` cell into VRAM megabytes.

    ``AdapterRAM`` is a ``UInt32`` so it caps at ~4 GiB
    (``4294967295``) and reports ``0`` for modern GPUs whose VRAM
    overflows that range. We treat both pathological values
    (``0`` and "looks like exactly 4 GiB") as "unknown" and return
    ``0`` so the GUI suppresses the misleading "(4GB)" suffix.
    """
    try:
        bytes_total = int(raw)
    except (TypeError, ValueError):
        return 0
    if bytes_total <= 0:
        return 0
    # The UInt32 cap is 0xFFFFFFFF == 4_294_967_295 bytes. In practice
    # Windows reports the rounded-down value 0xFFE00000 == 4_293_918_720
    # (~4095 MiB) for any card whose true VRAM meets or exceeds 4 GiB,
    # so any value at or above ~4 GiB is almost certainly the cap, not
    # a real measurement. Per spec we'd rather show
    # "GPU: <name>" with no VRAM suffix than the misleading "(4GB)"
    # for a 24 GB card. The trade-off is that a genuine 4 GB legacy
    # card (e.g. GTX 1050 Ti) loses its accurate VRAM display, which
    # the GUI can live with - the model-fit estimator falls back to
    # CPU mode when ``vram_total_mb=0`` anyway.
    if bytes_total >= 4_000_000_000:
        return 0
    return bytes_total // (1024 * 1024)


def _parse_windows_video_csv(raw_stdout: str) -> list[GPU]:
    """Parse the CSV emitted by both PowerShell ``ConvertTo-Csv`` and
    legacy ``wmic ... /format:csv`` into ``GPU`` dataclasses.

    The two producers differ slightly:

    * PowerShell columns: ``Name,AdapterRAM`` (header on row 0).
    * wmic columns: ``Node,AdapterRAM,Name`` (header on row 0, machine
      name on column 0). We normalise by looking up the header row
      and indexing into named columns instead of fixed positions.
    """
    lines = [ln.strip() for ln in raw_stdout.splitlines() if ln.strip()]
    if not lines:
        return []
    header = [h.strip().strip('"').lower() for h in lines[0].split(",")]
    try:
        name_idx = header.index("name")
    except ValueError:
        return []
    ram_idx = header.index("adapterram") if "adapterram" in header else -1

    gpus: list[GPU] = []
    for row in lines[1:]:
        cols = [c.strip().strip('"') for c in row.split(",")]
        if len(cols) <= name_idx:
            continue
        name = cols[name_idx]
        if not name:
            continue
        # Skip Microsoft "Basic Display Adapter" / "Remote Display"
        # placeholders that show up under RDP / Hyper-V; they are not
        # real GPUs and would mask the actual hardware below them.
        low = name.lower()
        if "basic display" in low or "remote display" in low:
            continue
        ram_raw = cols[ram_idx] if 0 <= ram_idx < len(cols) else ""
        gpus.append(
            GPU(
                name=name,
                backend=_classify_windows_gpu_backend(name),
                vram_total_mb=_vram_mb_from_adapter_ram(ram_raw),
                vram_free_mb=0,
            )
        )
    return gpus


def _detect_gpu_powershell_cim() -> list[GPU]:
    """Windows fallback: ``Get-CimInstance Win32_VideoController``.

    Used when ``nvidia-smi`` is missing (e.g. AMD-only host, or the
    pythonw launcher's PATH is too lean to find it). Reads the GPU
    name and ``AdapterRAM`` for every adapter and classifies the
    backend by keyword. VRAM is best-effort: ``AdapterRAM`` caps at
    ~4 GB so for newer cards we report ``0`` and let
    :meth:`HardwareReport.short` drop the GB suffix gracefully.
    """
    if os.name != "nt":
        return []
    powershell = shutil.which("powershell.exe") or shutil.which("powershell")
    if not powershell:
        # Fall back to an absolute path; PowerShell is part of every
        # supported Windows release, so this almost always exists even
        # when PATH is mangled.
        candidate = Path(os.path.expandvars(
            r"%WINDIR%\System32\WindowsPowerShell\v1.0\powershell.exe"
        ))
        if not candidate.exists():
            return []
        powershell = str(candidate)
    try:
        out = subprocess.run(
            [
                powershell,
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-Command",
                "Get-CimInstance Win32_VideoController | "
                "Select-Object Name,AdapterRAM | "
                "ConvertTo-Csv -NoTypeInformation",
            ],
            capture_output=True,
            text=True,
            timeout=8,
            **_subprocess_kwargs(),
        )
    except Exception as exc:  # noqa: BLE001 - defensive probe
        print(f"[hardware] PowerShell GPU probe failed: {exc!r}", file=sys.stderr)
        return []
    if out.returncode != 0:
        return []
    try:
        return _parse_windows_video_csv(out.stdout)
    except Exception as exc:  # noqa: BLE001 - parser must never raise
        print(f"[hardware] PowerShell GPU parse failed: {exc!r}", file=sys.stderr)
        return []


def _detect_gpu_wmic_legacy() -> list[GPU]:
    """Windows last-resort fallback: legacy ``wmic``.

    Win11 24H2 no longer ships ``wmic.exe``; ``subprocess.run`` then
    raises ``FileNotFoundError`` and we return ``[]`` so the GUI shows
    "GPU: none" rather than crashing. On older Windows where ``wmic``
    still exists this gives one more chance to identify the GPU when
    PowerShell is also broken (e.g. ConstrainedLanguage mode).
    """
    if os.name != "nt":
        return []
    try:
        out = subprocess.run(
            [
                "wmic",
                "path",
                "Win32_VideoController",
                "get",
                "Name,AdapterRAM",
                "/format:csv",
            ],
            capture_output=True,
            text=True,
            timeout=8,
            **_subprocess_kwargs(),
        )
    except FileNotFoundError:
        return []
    except Exception as exc:  # noqa: BLE001 - defensive probe
        print(f"[hardware] wmic GPU probe failed: {exc!r}", file=sys.stderr)
        return []
    if out.returncode != 0:
        return []
    try:
        return _parse_windows_video_csv(out.stdout)
    except Exception as exc:  # noqa: BLE001 - parser must never raise
        print(f"[hardware] wmic GPU parse failed: {exc!r}", file=sys.stderr)
        return []


def detect_gpus() -> list[GPU]:
    """Cascade GPU detection.

    Order:
        1. ``nvidia-smi`` (with Windows install-path fallback).
        2. ``rocm-smi`` (Linux only - ROCm is not shipped on Windows).
        3. ``system_profiler`` (macOS).
        4. Windows: PowerShell ``Win32_VideoController`` (only if the
           previous probes returned nothing AND we're on Windows -
           covers AMD/Intel hosts and NVIDIA hosts where nvidia-smi is
           unreachable from the GUI subprocess).
        5. Windows: legacy ``wmic`` (last resort; no-op on Win11 24H2+
           where wmic is gone).

    Stops at the first probe that yields >= 1 GPU on the Windows
    fallback steps so we don't double-count adapters across PowerShell
    and wmic.
    """
    out: list[GPU] = []
    out.extend(_detect_nvidia())
    out.extend(_detect_amd())
    out.extend(_detect_metal())
    if os.name == "nt" and not out:
        ps = _detect_gpu_powershell_cim()
        if ps:
            return ps
        wmic = _detect_gpu_wmic_legacy()
        if wmic:
            return wmic
    return out


# ---- CPU / RAM / Disk ------------------------------------------------------


def detect_cpu() -> Optional[CPU]:
    model = platform.processor() or platform.machine()
    if not model and Path("/proc/cpuinfo").exists():
        try:
            text = Path("/proc/cpuinfo").read_text(encoding="utf-8")
            m = re.search(r"model name\s*:\s*(.*)", text)
            if m:
                model = m.group(1).strip()
        except Exception:
            pass
    if psutil is None:
        return CPU(model=model or "unknown", physical_cores=0, logical_cores=0)
    try:
        physical = psutil.cpu_count(logical=False) or 0
        logical = psutil.cpu_count(logical=True) or 0
    except Exception:
        physical = logical = 0
    return CPU(model=model or "unknown", physical_cores=int(physical), logical_cores=int(logical))


def detect_memory() -> Optional[Memory]:
    if psutil is None:
        return None
    try:
        v = psutil.virtual_memory()
        return Memory(
            total_mb=int(v.total // (1024 * 1024)),
            available_mb=int(v.available // (1024 * 1024)),
        )
    except Exception:
        return None


def detect_disk(path: Path | str = "/") -> Optional[Disk]:
    if psutil is None:
        try:
            st = shutil.disk_usage(str(path))
            return Disk(
                path=str(path),
                total_mb=int(st.total // (1024 * 1024)),
                free_mb=int(st.free // (1024 * 1024)),
            )
        except Exception:
            return None
    try:
        st = psutil.disk_usage(str(path))
        return Disk(
            path=str(path),
            total_mb=int(st.total // (1024 * 1024)),
            free_mb=int(st.free // (1024 * 1024)),
        )
    except Exception:
        return None


def detect_os() -> tuple[str, str]:
    return platform.system(), platform.release()


def report(disk_path: Path | str = "/") -> HardwareReport:
    osn, osr = detect_os()
    return HardwareReport(
        gpus=detect_gpus(),
        cpu=detect_cpu(),
        memory=detect_memory(),
        disk=detect_disk(disk_path),
        os_name=osn,
        os_release=osr,
    )


# ---- VRAM estimation -------------------------------------------------------


@dataclass
class Compatibility:
    level: str  # ok | tight | likely_oom | cpu_fallback
    message: str
    estimated_vram_mb: int
    available_vram_mb: int


_KV_BYTES_PER_TOKEN = {
    "f16": 2,
    "bf16": 2,
    "q8_0": 1,
    "q4_0": 0.5,
}


def estimate_vram_mb(
    *,
    model_size_bytes: int,
    n_gpu_layers: int,
    total_layers: int = 32,
    ctx_size: int = 16384,
    cache_type_k: str = "f16",
    cache_type_v: str = "f16",
    n_kv_heads: int = 8,
    head_dim: int = 128,
    overhead_mb: int = 600,
) -> int:
    """Rough VRAM estimate.

    The formula is intentionally coarse - it's used for UI "fits / tight /
    likely OOM" badges, not for capacity planning.
    """
    if total_layers <= 0:
        total_layers = 32
    layer_fraction = max(0, min(1.0, n_gpu_layers / total_layers))
    weights_mb = (model_size_bytes / (1024 * 1024)) * layer_fraction

    bytes_per_token_k = _KV_BYTES_PER_TOKEN.get(cache_type_k, 2)
    bytes_per_token_v = _KV_BYTES_PER_TOKEN.get(cache_type_v, 2)
    kv_bytes = ctx_size * n_kv_heads * head_dim * (bytes_per_token_k + bytes_per_token_v) * layer_fraction
    kv_mb = kv_bytes / (1024 * 1024)

    return int(weights_mb + kv_mb + overhead_mb)


def discover_llama_variants() -> dict[str, Path]:
    """Return ``{variant_name: binary_path}`` for every llama-server
    variant the Windows installer left on disk.

    The installer always copies ``cpu`` / ``cuda`` / ``vulkan`` (when
    bundled) under ``<install_dir>/llama-variants/<variant>/`` and
    additionally extracts the *chosen* variant into
    ``<install_dir>/tools/``. The active install dir is recovered from
    the sentinel's ``llama_path``: that path is the bundled CPU/CUDA/
    Vulkan binary copied into ``tools/`` at install time, so the
    install root is two parents up.

    Returns an empty dict on non-Windows hosts, on Windows installs
    where the sentinel is absent (manual ``pip install`` setup), or
    when the variants directory does not exist (e.g. ``-Lean`` build).
    The keys are lowercase variant names; values are absolute paths to
    each variant's ``llama-server.exe``.
    """
    sentinel = _read_installer_sentinel()
    if not isinstance(sentinel, dict):
        return {}
    llama_path = sentinel.get("llama_path")
    if not isinstance(llama_path, str) or not llama_path:
        return {}
    try:
        binary = Path(llama_path)
        app_root = binary.parent.parent
    except Exception:
        return {}
    variants_dir = app_root / "llama-variants"
    try:
        if not variants_dir.is_dir():
            return {}
    except OSError:
        return {}
    out: dict[str, Path] = {}
    try:
        children = list(variants_dir.iterdir())
    except OSError:
        return {}
    for sub in children:
        try:
            if not sub.is_dir():
                continue
        except OSError:
            continue
        name = sub.name.lower()
        if name not in ("cpu", "cuda", "vulkan"):
            continue
        exe = sub / "llama-server.exe"
        try:
            if exe.is_file():
                out[name] = exe
        except OSError:
            continue
    return out


def active_llama_variant() -> Optional[str]:
    """Return the variant chosen at install time (``cpu`` / ``cuda`` /
    ``vulkan``) or ``None`` when the sentinel is missing or malformed.
    """
    sentinel = _read_installer_sentinel()
    if isinstance(sentinel, dict):
        variant = sentinel.get("variant")
        if isinstance(variant, str) and variant in ("cpu", "cuda", "vulkan"):
            return variant
    return None


def _read_installer_sentinel() -> Optional[dict]:
    """Read the sentinel file written by the Windows installer wizard.

    The Windows Setup wizard (Inno Setup) writes a small JSON file at
    ``user_config_dir() / .installer_choice.json`` documenting the
    llama-server variant the user picked at install time (``cpu`` /
    ``cuda`` / ``vulkan``), the absolute path to the bundled
    binary, and a recommended ``n_gpu_layers``. Returning the dict
    lets :func:`suggest_deployment_mode` short-circuit the existing
    PATH/Docker probe and recommend ``local_binary`` directly, so a
    Windows user who installed via Setup is never nudged toward
    Docker (which on Windows requires the heavy WSL2 install).

    On Linux/macOS / Windows-without-installer the sentinel does not
    exist and we return ``None``. Malformed JSON or any I/O error
    also returns ``None`` so the caller falls back to the existing
    detection logic.
    """
    try:
        from ._paths import user_config_dir
    except Exception:
        return None
    try:
        sentinel = user_config_dir() / ".installer_choice.json"
        if not sentinel.exists():
            return None
        import json as _json
        data = _json.loads(sentinel.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return None
        return data
    except Exception:
        return None


def suggest_deployment_mode() -> tuple[str, str]:
    """Pick a sensible default ``deployment_mode`` for a fresh
    install based on what's already on the user's machine.

    Returns ``(mode, hint)`` where ``mode`` is one of
    ``"local_binary"`` / ``"docker"`` / ``"external"`` and
    ``hint`` is a short, plain-language sentence the GUI can
    show next to the picker so a newbie understands *why* this
    suggestion was made.

    Resolution rules (cheap, no network, no Docker daemon
    interrogation beyond ``--version``):

    0. Windows installer sentinel present -> ``local_binary``.
       Avoids recommending Docker on Windows when the Setup
       wizard already bundled and verified ``llama-server.exe``.
    1. ``llama-server`` on PATH -> ``local_binary``. Fastest path
       for users who already built llama.cpp from source.
    2. ``docker --version`` answers cleanly -> ``docker``. Newbie
       path: nothing to build, the GUI handles ``docker pull`` /
       ``docker run`` for them on first Start.
    3. Neither -> still suggest ``docker`` (smaller install
       footprint than building llama.cpp) and let the hint nudge
       the user toward installing Docker.
    """
    sentinel = _read_installer_sentinel()
    if sentinel is not None:
        variant = sentinel.get("variant")
        if isinstance(variant, str) and variant in ("cpu", "cuda", "vulkan"):
            return (
                "local_binary",
                f"Detected llama-server installed by Setup wizard "
                f"({variant.upper()} variant).",
            )

    import shutil as _shutil
    import subprocess as _subprocess

    if _shutil.which("llama-server") is not None:
        return (
            "local_binary",
            "Detected llama-server on PATH; using the local binary.",
        )
    docker_bin = _shutil.which("docker")
    if docker_bin is not None:
        try:
            res = _subprocess.run(
                [docker_bin, "--version"],
                capture_output=True, text=True, timeout=3.0, check=False,
                **_subprocess_kwargs(),
            )
            if res.returncode == 0:
                return (
                    "docker",
                    "Detected Docker; the GUI will pull the llama.cpp "
                    "image on first Start (cached locally afterwards).",
                )
        except Exception:
            pass
    return (
        "docker",
        "Neither llama-server nor Docker were found. Installing "
        "Docker is the lightest path; the GUI will then handle "
        "the rest on first Start.",
    )


def report_dict(disk_path: Path | str = ".") -> dict:
    """Return a JSON-serializable hardware report for the installer wizard.

    Mirrors :func:`report` but flattens the dataclasses into a plain
    dict so the Inno Setup Pascal Script can parse it without
    knowing about Python dataclasses. The shape (keys, types) is the
    contract with the installer; bumping the schema is a breaking
    change that needs a coordinated installer update.
    """
    r = report(disk_path)
    return {
        "gpus": [
            {
                "name": g.name,
                "backend": g.backend,
                "vram_total_mb": g.vram_total_mb,
                "vram_free_mb": g.vram_free_mb,
            }
            for g in r.gpus
        ],
        "cpu": (
            {
                "model": r.cpu.model,
                "physical_cores": r.cpu.physical_cores,
                "logical_cores": r.cpu.logical_cores,
            }
            if r.cpu
            else None
        ),
        "memory": (
            {
                "total_mb": r.memory.total_mb,
                "available_mb": r.memory.available_mb,
            }
            if r.memory
            else None
        ),
        "disk": (
            {
                "path": r.disk.path,
                "total_mb": r.disk.total_mb,
                "free_mb": r.disk.free_mb,
            }
            if r.disk
            else None
        ),
        "os_name": r.os_name,
        "os_release": r.os_release,
    }


def estimate_ram_mb(
    *,
    model_size_bytes: int,
    ctx_size: int = 16384,
    cache_type_k: str = "f16",
    cache_type_v: str = "f16",
    n_kv_heads: int = 8,
    head_dim: int = 128,
    overhead_mb: int = 400,
) -> int:
    """Rough system-RAM estimate for a CPU-only run.

    Mirrors :func:`estimate_vram_mb` but skips the GPU
    layer-fraction term: with ``n_gpu_layers=0`` the whole model
    is paged into system RAM and the KV cache lives there too.
    Used by the preset gallery's ``cpu_only`` badge so the user
    sees a real "Fits ~X GB / Y GB RAM" estimate instead of a
    generic "CPU only" string.
    """
    weights_mb = model_size_bytes / (1024 * 1024)
    bytes_per_token_k = _KV_BYTES_PER_TOKEN.get(cache_type_k, 2)
    bytes_per_token_v = _KV_BYTES_PER_TOKEN.get(cache_type_v, 2)
    kv_bytes = (
        ctx_size * n_kv_heads * head_dim
        * (bytes_per_token_k + bytes_per_token_v)
    )
    kv_mb = kv_bytes / (1024 * 1024)
    return int(weights_mb + kv_mb + overhead_mb)


def compatibility(
    *,
    model_size_bytes: int,
    n_gpu_layers: int,
    ctx_size: int,
    cache_type_k: str,
    cache_type_v: str,
    available_vram_mb: int,
    available_ram_mb: int = 0,
    total_layers: int = 32,
) -> Compatibility:
    if n_gpu_layers <= 0 or available_vram_mb <= 0:
        # CPU-only path. When the caller passes
        # ``available_ram_mb`` (read from psutil at runtime) we can
        # produce the same "Fits / Tight / Likely OOM" verdict as
        # the GPU path, with the model + KV cache estimated in
        # system RAM instead of VRAM.
        if available_ram_mb > 0 and model_size_bytes > 0:
            est_ram = estimate_ram_mb(
                model_size_bytes=model_size_bytes,
                ctx_size=ctx_size,
                cache_type_k=cache_type_k,
                cache_type_v=cache_type_v,
            )
            if est_ram <= available_ram_mb * 0.85:
                level = "ok"
                msg = f"Fits (~{est_ram // 1024} GB / {available_ram_mb // 1024} GB RAM)"
            elif est_ram <= available_ram_mb * 1.05:
                level = "tight"
                msg = f"Tight (~{est_ram // 1024} GB / {available_ram_mb // 1024} GB RAM)"
            else:
                level = "likely_oom"
                msg = (
                    f"Likely OOM (~{est_ram // 1024} GB needed / "
                    f"{available_ram_mb // 1024} GB RAM)"
                )
            return Compatibility(
                level=level,
                message=msg,
                estimated_vram_mb=est_ram,
                available_vram_mb=available_ram_mb,
            )
        return Compatibility(
            level="cpu_fallback",
            message="CPU only (n_gpu_layers=0 or no GPU)",
            estimated_vram_mb=0,
            available_vram_mb=available_vram_mb,
        )
    est = estimate_vram_mb(
        model_size_bytes=model_size_bytes,
        n_gpu_layers=n_gpu_layers,
        total_layers=total_layers,
        ctx_size=ctx_size,
        cache_type_k=cache_type_k,
        cache_type_v=cache_type_v,
    )
    # The estimate is intentionally rough (see ``estimate_vram_mb``)
    # so the badge prefixes the GB number with ``~`` to match the
    # CPU-RAM branch and signal "approximate" without putting that
    # word in the user's face.
    if est <= available_vram_mb * 0.85:
        level = "ok"
        msg = f"Fits (~{est // 1024} GB / {available_vram_mb // 1024} GB)"
    elif est <= available_vram_mb * 1.05:
        level = "tight"
        msg = f"Tight (~{est // 1024} GB / {available_vram_mb // 1024} GB)"
    else:
        level = "likely_oom"
        msg = (
            f"Likely OOM (~{est // 1024} GB needed / "
            f"{available_vram_mb // 1024} GB)"
        )
    return Compatibility(
        level=level,
        message=msg,
        estimated_vram_mb=est,
        available_vram_mb=available_vram_mb,
    )


__all__ = [
    "GPU",
    "CPU",
    "Memory",
    "Disk",
    "HardwareReport",
    "Compatibility",
    "detect_gpus",
    "detect_cpu",
    "detect_memory",
    "detect_disk",
    "detect_os",
    "report",
    "report_dict",
    "estimate_vram_mb",
    "estimate_ram_mb",
    "suggest_deployment_mode",
    "compatibility",
]


if __name__ == "__main__":
    # CLI entry point: ``python -m anonymize.hardware`` prints a single
    # JSON line with the host hardware report. Used by the Windows
    # installer wizard (Inno Setup Pascal Script) to recommend the
    # right llama.cpp variant.
    import json as _json

    print(_json.dumps(report_dict()))
