@echo off
setlocal EnableExtensions DisableDelayedExpansion

pushd "%~dp0.."
if errorlevel 1 exit /b 1

for %%T in (uv pnpm cargo) do (
    where %%T >nul 2>nul
    if errorlevel 1 (
        echo Missing build tool: %%T. See docs/DESKTOP.md. 1>&2
        popd
        exit /b 1
    )
)

call uv sync --project backend --group dev --locked
if errorlevel 1 goto :failed

call pnpm install --frozen-lockfile
if errorlevel 1 goto :failed

"backend\.venv\Scripts\python.exe" "scripts\build-backend.py"
if errorlevel 1 goto :failed

call pnpm exec tauri icon src-tauri/icons/app-icon.svg
if errorlevel 1 goto :failed

call pnpm exec tauri build --bundles nsis
if errorlevel 1 goto :failed

echo Windows installer: src-tauri\target\release\bundle\nsis\
popd
exit /b 0

:failed
set "JDX_BUILD_EXIT=%errorlevel%"
echo Windows build failed with exit code %JDX_BUILD_EXIT%. 1>&2
popd
exit /b %JDX_BUILD_EXIT%
