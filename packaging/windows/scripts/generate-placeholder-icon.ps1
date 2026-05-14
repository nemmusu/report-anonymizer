<#
.SYNOPSIS
    Generates a placeholder app_icon.ico when cairosvg is unavailable.

.DESCRIPTION
    Emergency fallback for plan caveat N1 / Worker B caveat #2: when the
    cairosvg path in convert-assets.ps1 cannot find a usable libcairo-2.dll
    on Windows (which is the case starting from WeasyPrint v53+ since the
    upstream bundle no longer ships Cairo), we synthesise a minimal multi-
    resolution .ico using System.Drawing primitives.

    The resulting icon is intentionally low-fidelity and should be replaced
    with a properly-rendered SVG conversion before v1.1.

.PARAMETER OutputPath
    Destination .ico path. Defaults to ../launcher/app_icon.ico relative to
    this script.
#>
[CmdletBinding()]
param(
    [string]$OutputPath
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

if (-not $OutputPath) {
    $ScriptDir   = Split-Path -Parent $MyInvocation.MyCommand.Definition
    $WindowsRoot = Split-Path -Parent $ScriptDir
    $OutputPath  = Join-Path $WindowsRoot 'launcher\app_icon.ico'
}

Add-Type -AssemblyName System.Drawing

$bgColor      = [System.Drawing.Color]::FromArgb(255, 34,  38,  46)
$borderColor  = [System.Drawing.Color]::FromArgb(255, 79,  140, 201)
$accentColor  = [System.Drawing.Color]::FromArgb(255, 93,  164, 236)
$fgColor      = [System.Drawing.Color]::FromArgb(255, 245, 246, 247)

$sizes      = @(16, 24, 32, 48, 64, 128, 256)
$pngFrames  = New-Object System.Collections.ArrayList

foreach ($s in $sizes) {
    $bmp = New-Object System.Drawing.Bitmap($s, $s, [System.Drawing.Imaging.PixelFormat]::Format32bppArgb)
    $g   = [System.Drawing.Graphics]::FromImage($bmp)
    $g.SmoothingMode     = [System.Drawing.Drawing2D.SmoothingMode]::AntiAlias
    $g.InterpolationMode = [System.Drawing.Drawing2D.InterpolationMode]::HighQualityBicubic
    $g.Clear([System.Drawing.Color]::Transparent)

    $cornerRadius = [Math]::Max(2, [int]($s / 5.5))
    $rectW = $s - 2
    $rectH = $s - 2
    $rect  = New-Object System.Drawing.Rectangle(1, 1, $rectW, $rectH)
    $arcSize = $cornerRadius * 2

    $path = New-Object System.Drawing.Drawing2D.GraphicsPath
    $path.AddArc($rect.X,                       $rect.Y,                       $arcSize, $arcSize, 180, 90)
    $path.AddArc($rect.Right - $arcSize,        $rect.Y,                       $arcSize, $arcSize, 270, 90)
    $path.AddArc($rect.Right - $arcSize,        $rect.Bottom - $arcSize,       $arcSize, $arcSize,   0, 90)
    $path.AddArc($rect.X,                       $rect.Bottom - $arcSize,       $arcSize, $arcSize,  90, 90)
    $path.CloseAllFigures()

    $brushBg = New-Object System.Drawing.SolidBrush($bgColor)
    $g.FillPath($brushBg, $path)
    $borderWidth = [Math]::Max(1, [int]($s / 40))
    $pen = New-Object System.Drawing.Pen($borderColor, $borderWidth)
    $g.DrawPath($pen, $path)

    $brushAccent = New-Object System.Drawing.SolidBrush($accentColor)
    $cx = [int]($s / 2)
    $cy = [int]($s / 2)
    $arr = [int]($s * 0.28)
    $points = @(
        (New-Object System.Drawing.Point(($cx - $arr),               ($cy - [int]($arr * 0.55)))),
        (New-Object System.Drawing.Point(($cx + [int]($arr * 0.3)),  ($cy - [int]($arr * 0.55)))),
        (New-Object System.Drawing.Point(($cx + $arr),                $cy)),
        (New-Object System.Drawing.Point(($cx + [int]($arr * 0.3)),  ($cy + [int]($arr * 0.55)))),
        (New-Object System.Drawing.Point(($cx - $arr),               ($cy + [int]($arr * 0.55))))
    )
    $g.FillPolygon($brushAccent, $points)

    if ($s -ge 32) {
        $fontSize = [Math]::Max(6, [int]($s * 0.30))
        $font     = New-Object System.Drawing.Font('Segoe UI Semibold', $fontSize, [System.Drawing.FontStyle]::Bold, [System.Drawing.GraphicsUnit]::Pixel)
        $brushFg  = New-Object System.Drawing.SolidBrush($fgColor)
        $sf       = New-Object System.Drawing.StringFormat
        $sf.Alignment     = [System.Drawing.StringAlignment]::Center
        $sf.LineAlignment = [System.Drawing.StringAlignment]::Center
        $g.DrawString('R', $font, $brushFg, [single]$cx, [single]$cy, $sf)
        $font.Dispose()
        $brushFg.Dispose()
        $sf.Dispose()
    }

    $g.Dispose()
    $brushBg.Dispose()
    $brushAccent.Dispose()
    $pen.Dispose()
    $path.Dispose()

    $ms = New-Object System.IO.MemoryStream
    $bmp.Save($ms, [System.Drawing.Imaging.ImageFormat]::Png)
    $bmp.Dispose()

    [void]$pngFrames.Add(@{ Size = $s; Data = $ms.ToArray() })
    $ms.Dispose()
}

# Assemble ICONDIR + ICONDIRENTRYs + PNG payload (BMP-format frames are also
# valid in .ico but PNG-encoded frames are accepted by every Windows version
# we care about and let us reuse System.Drawing's PNG encoder).
$out = New-Object System.IO.MemoryStream
$bw  = New-Object System.IO.BinaryWriter($out)
$bw.Write([uint16]0)                           # reserved
$bw.Write([uint16]1)                           # type: icon
$bw.Write([uint16]$pngFrames.Count)            # image count

$dataOffset = 6 + (16 * $pngFrames.Count)
foreach ($p in $pngFrames) {
    $widthByte  = if ($p.Size -ge 256) { [byte]0 } else { [byte]$p.Size }
    $heightByte = $widthByte
    $bw.Write([byte]$widthByte)                # width  (0 means 256)
    $bw.Write([byte]$heightByte)               # height (0 means 256)
    $bw.Write([byte]0)                         # palette colors (0 for non-palettised)
    $bw.Write([byte]0)                         # reserved
    $bw.Write([uint16]1)                       # color planes
    $bw.Write([uint16]32)                      # bpp
    $bw.Write([uint32]$p.Data.Length)          # bytes in image data
    $bw.Write([uint32]$dataOffset)             # offset to image data
    $dataOffset += $p.Data.Length
}
foreach ($p in $pngFrames) {
    $bw.Write($p.Data)
}
$bw.Flush()
$bytes = $out.ToArray()
$bw.Dispose()
$out.Dispose()

New-Item -ItemType Directory -Force -Path (Split-Path -Parent $OutputPath) | Out-Null
[System.IO.File]::WriteAllBytes($OutputPath, $bytes)

Write-Host ("[generate-placeholder-icon] wrote {0} ({1:N0} bytes, {2} frames: {3})" -f $OutputPath, $bytes.Length, $pngFrames.Count, ($sizes -join '/')) -ForegroundColor Green
