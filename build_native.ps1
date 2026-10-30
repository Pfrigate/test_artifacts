# Builds screenrecorder/_srnative.pyd with CMake + MSVC (Visual Studio 2022 or newer with the
# "Desktop development with C++" workload and a Windows 10/11 SDK).
param(
    [string]$Config = "Release"
)
$ErrorActionPreference = "Stop"
$root = $PSScriptRoot
$python = (Get-Command python).Source
$pybind = (& python -m pybind11 --cmakedir).Trim()
if ($LASTEXITCODE -ne 0) { throw "pybind11 is not installed: pip install -r requirements-dev.txt" }

cmake -S $root -B "$root/build" -A x64 "-Dpybind11_DIR=$pybind" "-DPython_EXECUTABLE=$python"
if ($LASTEXITCODE -ne 0) { throw "CMake configure failed" }
cmake --build "$root/build" --config $Config --parallel
if ($LASTEXITCODE -ne 0) { throw "Native build failed" }

python -c "import screenrecorder._srnative as n; print('native OK', n.__version__)"
if ($LASTEXITCODE -ne 0) { throw "Native extension does not import" }
