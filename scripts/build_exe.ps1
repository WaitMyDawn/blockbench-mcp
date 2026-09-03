"""打包为免 Python 环境的单文件 exe（Windows）。

用法（项目根目录）：
    .\.venv\Scripts\python -m pip install -e ".[build]"
    powershell -ExecutionPolicy Bypass -File .\scripts\build_exe.ps1
产物：dist\blockbench-mcp.exe
"""

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

& "$root\.venv\Scripts\python.exe" -m PyInstaller `
    --noconfirm `
    --onefile `
    --name blockbench-mcp `
    --collect-all mcp `
    main.py

Write-Host "exe: $root\dist\blockbench-mcp.exe"

