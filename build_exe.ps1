# Produces dist/ScreenRecorder/ScreenRecorder.exe (one-folder) and dist/ScreenRecorder.zip.
param(
    [switch]$SkipNative
)
$ErrorActionPreference = "Stop"
$root = $PSScriptRoot
Push-Location $root
try {
    if (-not $SkipNative) { & "$root/build_native.ps1" }
    python packaging/make_icon.py packaging/screenrecorder.ico
    if ($LASTEXITCODE -ne 0) { throw "Icon generation failed" }
    pyinstaller packaging/screenrecorder.spec --noconfirm --clean
    if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed" }

    $media = Get-ChildItem dist/ScreenRecorder -Recurse -Include *avcodec*, *avformat*, *avutil*, *swscale*, *ffmpeg*, *gstreamer*, *opencv* -ErrorAction SilentlyContinue
    if ($media) { throw "Unexpected media framework binaries in bundle: $($media.FullName -join ', ')" }

    Compress-Archive -Path dist/ScreenRecorder/* -DestinationPath dist/ScreenRecorder.zip -Force
    Write-Host "Built dist/ScreenRecorder.zip"
}
finally {
    Pop-Location
}
