"""Detect external tools required by the engine and produce install hints.

Used by the GUI startup banner. Each tool has:

* ``required``: bool - if False the user can still operate (e.g. tesseract).
* ``check_cmd``: how to obtain the version (or just confirm presence).
* per-distro install commands populated from ``platform.freedesktop_os_release``.
"""
from __future__ import annotations

import platform
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


@dataclass
class ToolStatus:
    name: str
    found: bool
    version: str = ""
    required: bool = False
    description: str = ""
    install_hint: str = ""


def _detect_distro() -> str:
    sys_name = platform.system()
    if sys_name == "Windows":
        return "windows"
    if sys_name == "Darwin":
        return "macos"
    osr = Path("/etc/os-release")
    if osr.exists():
        try:
            text = osr.read_text(encoding="utf-8")
            if "ID=ubuntu" in text or "ID=debian" in text or "ID_LIKE=debian" in text:
                return "debian"
            if "ID=fedora" in text or "ID=rhel" in text or "ID_LIKE=" in text and "rhel" in text:
                return "fedora"
            if "ID=arch" in text or "ID_LIKE=arch" in text:
                return "arch"
            if "ID=alpine" in text:
                return "alpine"
        except Exception:
            pass
    return "linux"


_INSTALL_HINTS: dict[str, dict[str, str]] = {
    "pandoc": {
        "debian": "sudo apt-get install -y pandoc",
        "fedora": "sudo dnf install -y pandoc",
        "arch": "sudo pacman -S --needed pandoc",
        "alpine": "sudo apk add pandoc",
        "macos": "brew install pandoc",
        "windows": "Bundled in installer (pypandoc-binary).",
    },
    "weasyprint": {
        # WeasyPrint is a Python package; the install hint covers the
        # system libs it depends on (Pango, Cairo, GDK-PixBuf). The
        # Python package itself ships in ``requirements.txt``.
        "debian": "sudo apt-get install -y libpango-1.0-0 libpangoft2-1.0-0",
        "fedora": "sudo dnf install -y pango",
        "arch": "sudo pacman -S --needed pango",
        "alpine": "sudo apk add pango",
        "macos": "brew install pango",
        "windows": "Bundled in installer (Pango/Cairo DLLs from weasyprint-windows.zip).",
    },
    "libreoffice": {
        "debian": "sudo apt-get install -y libreoffice",
        "fedora": "sudo dnf install -y libreoffice",
        "arch": "sudo pacman -S --needed libreoffice-fresh",
        "alpine": "sudo apk add libreoffice",
        "macos": "brew install --cask libreoffice",
        "windows": "winget install TheDocumentFoundation.LibreOffice (optional).",
    },
    "tesseract": {
        "debian": "sudo apt-get install -y tesseract-ocr tesseract-ocr-ita tesseract-ocr-eng",
        "fedora": "sudo dnf install -y tesseract tesseract-langpack-ita tesseract-langpack-eng",
        "arch": "sudo pacman -S --needed tesseract tesseract-data-ita tesseract-data-eng",
        "alpine": "sudo apk add tesseract-ocr tesseract-ocr-data-ita tesseract-ocr-data-eng",
        "macos": "brew install tesseract tesseract-lang",
        "windows": "winget install UB-Mannheim.TesseractOCR (optional, for OCR).",
    },
    "ocrmypdf": {
        "debian": "sudo apt-get install -y ocrmypdf",
        "fedora": "sudo dnf install -y ocrmypdf",
        "arch": "sudo pacman -S --needed ocrmypdf",
        "alpine": "pip install ocrmypdf",
        "macos": "brew install ocrmypdf",
        "windows": "pip install ocrmypdf (optional, requires Tesseract).",
    },
    "qpdf": {
        "debian": "sudo apt-get install -y qpdf",
        "fedora": "sudo dnf install -y qpdf",
        "arch": "sudo pacman -S --needed qpdf",
        "alpine": "sudo apk add qpdf",
        "macos": "brew install qpdf",
        "windows": "winget install qpdf.qpdf (optional, encrypted PDFs).",
    },
    "pdftotext": {
        "debian": "sudo apt-get install -y poppler-utils",
        "fedora": "sudo dnf install -y poppler-utils",
        "arch": "sudo pacman -S --needed poppler",
        "alpine": "sudo apk add poppler-utils",
        "macos": "brew install poppler",
        "windows": "Bundled in installer (poppler-windows pdftotext.exe).",
    },
    "llama-server": {
        "debian": "git clone https://github.com/ggml-org/llama.cpp ~/llama.cpp && cd ~/llama.cpp && cmake -B build && cmake --build build -j",
        "fedora": "git clone https://github.com/ggml-org/llama.cpp ~/llama.cpp && cd ~/llama.cpp && cmake -B build && cmake --build build -j",
        "arch": "git clone https://github.com/ggml-org/llama.cpp ~/llama.cpp && cd ~/llama.cpp && cmake -B build && cmake --build build -j",
        "macos": "brew install llama.cpp",
        "windows": "Bundled in installer (variant chosen by the Setup wizard: CPU/CUDA/Vulkan).",
    },
}


def _version(cmd: list[str]) -> str:
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=5)
        text = (out.stdout or out.stderr or "").strip().splitlines()
        return text[0] if text else ""
    except Exception:
        return ""


def _hint(tool: str) -> str:
    distro = _detect_distro()
    return _INSTALL_HINTS.get(tool, {}).get(distro, "") or ""


def check_tool(
    name: str,
    *,
    required: bool = False,
    description: str = "",
    binary: Optional[str] = None,
    version_args: Optional[list[str]] = None,
) -> ToolStatus:
    binary = binary or name
    path = shutil.which(binary)
    if not path:
        return ToolStatus(
            name=name,
            found=False,
            required=required,
            description=description,
            install_hint=_hint(name),
        )
    version = _version([path] + (version_args or ["--version"]))
    return ToolStatus(
        name=name,
        found=True,
        version=version,
        required=required,
        description=description,
        install_hint=_hint(name),
    )


def _check_weasyprint() -> "ToolStatus":
    """Probe WeasyPrint via Python import + a probe of its system libs.

    WeasyPrint isn't a CLI binary (it's a Python package), so the
    generic ``check_tool`` shape doesn't apply. We import it and call
    ``weasyprint.__version__``; on ImportError we still report the
    install hint for the system Pango/Cairo libs because that's the
    most common cause of WeasyPrint refusing to start at runtime.
    """
    try:
        import weasyprint  # type: ignore[import-not-found]
        version = getattr(weasyprint, "__version__", "")
        return ToolStatus(
            name="weasyprint",
            found=True,
            version=version,
            required=False,
            description="HTML -> PDF rendering (replaces wkhtmltopdf)",
            install_hint=_hint("weasyprint"),
        )
    except Exception as e:
        return ToolStatus(
            name="weasyprint",
            found=False,
            version=str(e)[:80],
            required=False,
            description="HTML -> PDF rendering (replaces wkhtmltopdf)",
            install_hint=_hint("weasyprint"),
        )


@dataclass
class EnvReport:
    tools: list[ToolStatus] = field(default_factory=list)

    @property
    def missing_required(self) -> list[ToolStatus]:
        return [t for t in self.tools if t.required and not t.found]

    @property
    def missing_optional(self) -> list[ToolStatus]:
        return [t for t in self.tools if not t.required and not t.found]

    def summary(self) -> str:
        lines: list[str] = []
        for t in self.tools:
            mark = "OK " if t.found else "-- "
            ver = f"  ({t.version})" if t.version else ""
            lines.append(f"{mark} {t.name}{ver}")
            if not t.found and t.install_hint:
                lines.append(f"     install: {t.install_hint}")
        if self.missing_required:
            lines.append("")
            lines.append("Missing REQUIRED tools - some features will not work.")
        return "\n".join(lines) or "No tools detected."


def run() -> EnvReport:
    rep = EnvReport()
    rep.tools.append(
        check_tool(
            "llama-server",
            required=False,
            description="LLM server (llama.cpp) for Tier-1 detection",
        )
    )
    rep.tools.append(
        check_tool("pandoc", required=False, description="Markdown -> HTML/PDF rendering")
    )
    rep.tools.append(_check_weasyprint())
    rep.tools.append(
        check_tool("libreoffice", binary="libreoffice", required=False, description="legacy .doc support")
    )
    rep.tools.append(
        check_tool("pdftotext", required=False, description="PDF text extraction for verifier")
    )
    rep.tools.append(
        check_tool("tesseract", required=False, description="OCR for scanned PDFs")
    )
    rep.tools.append(
        check_tool("ocrmypdf", required=False, description="OCR pipeline for scanned PDFs")
    )
    rep.tools.append(check_tool("qpdf", required=False, description="encrypted PDF utilities"))
    return rep


__all__ = ["ToolStatus", "EnvReport", "check_tool", "run"]
