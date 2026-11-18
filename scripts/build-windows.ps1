$ErrorActionPreference = "Stop"
$JdxRoot = Split-Path -Parent $PSScriptRoot
Set-Location $JdxRoot

foreach ($tool in @("uv", "pnpm", "cargo")) {
    if (-not (Get-Command $tool -ErrorAction SilentlyContinue)) {
        throw "Missing build tool: $tool. See docs/DESKTOP.md."
    }
}

function Invoke-Checked {
    param([string]$Program, [string[]]$Arguments)
    & $Program @Arguments
    if ($LASTEXITCODE -ne 0) { throw "$Program failed with exit code $LASTEXITCODE" }
}

Invoke-Checked "uv" @("sync", "--project", "backend", "--group", "dev", "--locked")
Invoke-Checked "pnpm" @("install", "--frozen-lockfile")
Invoke-Checked "backend/.venv/Scripts/python.exe" @("scripts/build-backend.py")
Invoke-Checked "pnpm" @("exec", "tauri", "icon", "src-tauri/icons/app-icon.svg")
Invoke-Checked "pnpm" @("exec", "tauri", "build", "--bundles", "nsis")
Write-Output "Windows installer: src-tauri/target/release/bundle/nsis/"
