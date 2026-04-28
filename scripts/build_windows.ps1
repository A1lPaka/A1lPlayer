param(
    [switch]$InstallBuildDeps,
    [switch]$DownloadRuntime,
    [string]$Version = "0.1.0",
    [string]$VlcZipUrl = "https://download.videolan.org/pub/videolan/vlc/3.0.21/win64/vlc-3.0.21-win64.zip",
    [string]$FfmpegZipUrl = "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip"
)

$ErrorActionPreference = "Stop"

$Root = Resolve-Path (Join-Path $PSScriptRoot "..")
$VendorRuntime = Join-Path $Root "vendor\runtime"
$DistRuntime = Join-Path $Root "dist\A1lPlayer\runtime"
$ReleaseDir = Join-Path $Root "release"

function New-CleanDirectory {
    param([Parameter(Mandatory = $true)][string]$Path)
    if (Test-Path -LiteralPath $Path) {
        Remove-Item -LiteralPath $Path -Recurse -Force
    }
    New-Item -ItemType Directory -Force -Path $Path | Out-Null
}

function Assert-File {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$Message
    )
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        throw $Message
    }
}

function Assert-Directory {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$Message
    )
    if (-not (Test-Path -LiteralPath $Path -PathType Container)) {
        throw $Message
    }
}

function Invoke-DownloadAndExpand {
    param(
        [Parameter(Mandatory = $true)][string]$Url,
        [Parameter(Mandatory = $true)][string]$ArchivePath,
        [Parameter(Mandatory = $true)][string]$Destination
    )
    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $ArchivePath) | Out-Null
    New-CleanDirectory -Path $Destination
    Invoke-WebRequest -Uri $Url -OutFile $ArchivePath
    Expand-Archive -LiteralPath $ArchivePath -DestinationPath $Destination -Force
}

function Copy-LibVlcRuntime {
    param(
        [Parameter(Mandatory = $true)][string]$SourceRoot,
        [Parameter(Mandatory = $true)][string]$DestinationRoot
    )

    Assert-File -Path (Join-Path $SourceRoot "libvlc.dll") -Message "VLC runtime source is missing libvlc.dll."
    Assert-File -Path (Join-Path $SourceRoot "libvlccore.dll") -Message "VLC runtime source is missing libvlccore.dll."
    Assert-Directory -Path (Join-Path $SourceRoot "plugins") -Message "VLC runtime source is missing plugins."

    if (Test-Path -LiteralPath $DestinationRoot) {
        Remove-Item -LiteralPath $DestinationRoot -Recurse -Force
    }
    New-Item -ItemType Directory -Force -Path $DestinationRoot | Out-Null
    Copy-Item -LiteralPath (Join-Path $SourceRoot "libvlc.dll") -Destination (Join-Path $DestinationRoot "libvlc.dll")
    Copy-Item -LiteralPath (Join-Path $SourceRoot "libvlccore.dll") -Destination (Join-Path $DestinationRoot "libvlccore.dll")
    Copy-Item -LiteralPath (Join-Path $SourceRoot "plugins") -Destination (Join-Path $DestinationRoot "plugins") -Recurse
}

function Optimize-VlcRuntime {
    $VlcRoot = Join-Path $VendorRuntime "vlc"
    if (-not (Test-Path -LiteralPath (Join-Path $VlcRoot "libvlc.dll") -PathType Leaf)) {
        return
    }

    $KeepNames = @("libvlc.dll", "libvlccore.dll", "plugins")
    $ExtraItems = Get-ChildItem -LiteralPath $VlcRoot -Force |
        Where-Object { $KeepNames -notcontains $_.Name }
    if ($ExtraItems.Count -eq 0) {
        return
    }

    $PrunedRoot = Join-Path $Root ".build\vlc-libvlc-runtime"
    Copy-LibVlcRuntime -SourceRoot $VlcRoot -DestinationRoot $PrunedRoot
    Copy-LibVlcRuntime -SourceRoot $PrunedRoot -DestinationRoot $VlcRoot
}

function Initialize-VlcRuntime {
    $VlcRoot = Join-Path $VendorRuntime "vlc"
    if (Test-Path -LiteralPath (Join-Path $VlcRoot "libvlc.dll") -PathType Leaf) {
        Optimize-VlcRuntime
        return
    }
    if (-not $DownloadRuntime) {
        return
    }

    $TempRoot = Join-Path $Root ".build\downloads\vlc"
    $Archive = Join-Path $Root ".build\downloads\vlc.zip"
    Invoke-DownloadAndExpand -Url $VlcZipUrl -ArchivePath $Archive -Destination $TempRoot
    $ExtractedRoot = Get-ChildItem -LiteralPath $TempRoot -Directory | Select-Object -First 1
    if ($null -eq $ExtractedRoot) {
        throw "VLC archive did not contain an extracted runtime directory."
    }
    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $VlcRoot) | Out-Null
    Copy-LibVlcRuntime -SourceRoot $ExtractedRoot.FullName -DestinationRoot $VlcRoot
}

function Initialize-FfmpegRuntime {
    $FfmpegRoot = Join-Path $VendorRuntime "ffmpeg"
    if ((Test-Path -LiteralPath (Join-Path $FfmpegRoot "bin\ffmpeg.exe") -PathType Leaf) -and
        (Test-Path -LiteralPath (Join-Path $FfmpegRoot "bin\ffprobe.exe") -PathType Leaf)) {
        return
    }
    if (-not $DownloadRuntime) {
        return
    }

    $TempRoot = Join-Path $Root ".build\downloads\ffmpeg"
    $Archive = Join-Path $Root ".build\downloads\ffmpeg.zip"
    Invoke-DownloadAndExpand -Url $FfmpegZipUrl -ArchivePath $Archive -Destination $TempRoot
    $BinRoot = Get-ChildItem -LiteralPath $TempRoot -Directory -Recurse |
        Where-Object { Test-Path -LiteralPath (Join-Path $_.FullName "ffmpeg.exe") } |
        Select-Object -First 1
    if ($null -eq $BinRoot) {
        throw "FFmpeg archive did not contain ffmpeg.exe."
    }
    if (Test-Path -LiteralPath $FfmpegRoot) {
        Remove-Item -LiteralPath $FfmpegRoot -Recurse -Force
    }
    New-Item -ItemType Directory -Force -Path (Join-Path $FfmpegRoot "bin") | Out-Null
    Copy-Item -LiteralPath (Join-Path $BinRoot.FullName "ffmpeg.exe") -Destination (Join-Path $FfmpegRoot "bin\ffmpeg.exe")
    Copy-Item -LiteralPath (Join-Path $BinRoot.FullName "ffprobe.exe") -Destination (Join-Path $FfmpegRoot "bin\ffprobe.exe")
}

function Initialize-SmallModel {
    $ModelTarget = Join-Path $VendorRuntime "models\faster-whisper-small"
    if (Test-Path -LiteralPath (Join-Path $ModelTarget "model.bin") -PathType Leaf) {
        return
    }

    $ProjectRuntimeModel = Join-Path $Root "runtime\models\faster-whisper-small"
    if (Test-Path -LiteralPath (Join-Path $ProjectRuntimeModel "model.bin") -PathType Leaf) {
        New-Item -ItemType Directory -Force -Path (Split-Path -Parent $ModelTarget) | Out-Null
        if (Test-Path -LiteralPath $ModelTarget) {
            Remove-Item -LiteralPath $ModelTarget -Recurse -Force
        }
        Copy-Item -LiteralPath $ProjectRuntimeModel -Destination $ModelTarget -Recurse
        return
    }

    $CacheRoot = Join-Path $env:USERPROFILE ".cache\huggingface\hub\models--Systran--faster-whisper-small\snapshots"
    if (-not (Test-Path -LiteralPath $CacheRoot -PathType Container)) {
        throw "faster-whisper small was not found in runtime\models or Hugging Face cache. Run the app once with model=small or download the model before building."
    }

    $Snapshot = Get-ChildItem -LiteralPath $CacheRoot -Directory |
        Where-Object { Test-Path -LiteralPath (Join-Path $_.FullName "model.bin") } |
        Sort-Object LastWriteTime -Descending |
        Select-Object -First 1
    if ($null -eq $Snapshot) {
        throw "Hugging Face cache exists, but no complete faster-whisper small snapshot with model.bin was found."
    }

    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $ModelTarget) | Out-Null
    if (Test-Path -LiteralPath $ModelTarget) {
        Remove-Item -LiteralPath $ModelTarget -Recurse -Force
    }
    Copy-Item -LiteralPath $Snapshot.FullName -Destination $ModelTarget -Recurse
}

function Optimize-BundledWhisperModels {
    $ModelsRoot = Join-Path $VendorRuntime "models"
    if (-not (Test-Path -LiteralPath $ModelsRoot -PathType Container)) {
        return
    }

    Get-ChildItem -LiteralPath $ModelsRoot -Directory |
        Where-Object { $_.Name -ne "faster-whisper-small" } |
        ForEach-Object { Remove-Item -LiteralPath $_.FullName -Recurse -Force }
}

function Find-InnoCompiler {
    $Command = Get-Command ISCC.exe -ErrorAction SilentlyContinue
    if ($null -ne $Command) {
        return $Command.Source
    }

    $Candidates = @(
        "$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe",
        "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe",
        "$env:ProgramFiles\Inno Setup 6\ISCC.exe"
    )
    foreach ($Candidate in $Candidates) {
        if (Test-Path -LiteralPath $Candidate -PathType Leaf) {
            return $Candidate
        }
    }
    throw "Inno Setup compiler was not found. Install Inno Setup 6 and rerun this script."
}

Push-Location $Root
try {
    if ($InstallBuildDeps) {
        py -m pip install -r requirements-build.txt
    }

    Initialize-VlcRuntime
    Initialize-FfmpegRuntime
    Initialize-SmallModel
    Optimize-BundledWhisperModels

    Assert-File -Path (Join-Path $VendorRuntime "vlc\libvlc.dll") -Message "Missing vendor\runtime\vlc\libvlc.dll. Rerun with -DownloadRuntime or add portable VLC manually."
    Assert-File -Path (Join-Path $VendorRuntime "ffmpeg\bin\ffmpeg.exe") -Message "Missing vendor\runtime\ffmpeg\bin\ffmpeg.exe. Rerun with -DownloadRuntime or add FFmpeg manually."
    Assert-File -Path (Join-Path $VendorRuntime "ffmpeg\bin\ffprobe.exe") -Message "Missing vendor\runtime\ffmpeg\bin\ffprobe.exe. Rerun with -DownloadRuntime or add FFprobe manually."
    Assert-File -Path (Join-Path $VendorRuntime "models\faster-whisper-small\model.bin") -Message "Missing bundled faster-whisper small model."

    py -m PyInstaller A1lPlayer.spec --clean --noconfirm

    New-CleanDirectory -Path $DistRuntime
    Copy-Item -Path (Join-Path $VendorRuntime "*") -Destination $DistRuntime -Recurse

    New-Item -ItemType Directory -Force -Path $ReleaseDir | Out-Null
    $env:A1LPLAYER_VERSION = $Version
    $Iscc = Find-InnoCompiler
    & $Iscc (Join-Path $Root "installer\A1lPlayer.iss")

    Write-Host "Release installer is in: $ReleaseDir"
}
finally {
    Pop-Location
}
