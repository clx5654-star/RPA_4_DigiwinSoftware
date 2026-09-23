param(
    [string]$Python = "python"
)

$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$pythonRoot = & $Python -c "import sys; print(sys.prefix)"
if ($LASTEXITCODE -ne 0) {
    throw "无法确定 Python 安装目录"
}

# This Python installation ships an init.tcl whose exact-version check is not
# accepted by its own Tcl runtime. Keep the system installation untouched and
# make a build-only copy with the compatible non-exact 8.6 requirement.
$sourceTcl = Join-Path $pythonRoot "tcl\tcl8.6"
$sourceTk = Join-Path $pythonRoot "tcl\tk8.6"
$fixedRoot = Join-Path $projectRoot "build\tcl_runtime"
$fixedTcl = Join-Path $fixedRoot "tcl8.6"
if (-not (Test-Path -LiteralPath (Join-Path $sourceTcl "init.tcl"))) {
    throw "Python Tcl 运行库不完整: $sourceTcl"
}
if (Test-Path -LiteralPath $fixedTcl) {
    Remove-Item -LiteralPath $fixedTcl -Recurse -Force
}
New-Item -ItemType Directory -Force -Path $fixedRoot | Out-Null
Copy-Item -LiteralPath $sourceTcl -Destination $fixedTcl -Recurse -Force
$initPath = Join-Path $fixedTcl "init.tcl"
$content = [IO.File]::ReadAllText($initPath)
$content = $content.Replace(
    "package require -exact Tcl 8.6.12",
    "package require Tcl 8.6")
[IO.File]::WriteAllText($initPath, $content, [Text.UTF8Encoding]::new($false))

$env:TCL_LIBRARY = $fixedTcl
$env:TK_LIBRARY = $sourceTk
Push-Location $projectRoot
try {
    & $Python -m PyInstaller `
        --noconfirm `
        --clean `
        --onefile `
        --windowed `
        --name E10_RPA_Submitter `
        --distpath dist `
        --workpath build\pyinstaller `
        --specpath build `
        rpa_submit_gui.py
    if ($LASTEXITCODE -ne 0) {
        throw "PyInstaller 打包失败，退出码 $LASTEXITCODE"
    }
}
finally {
    Pop-Location
}

Write-Host "构建完成: $(Join-Path $projectRoot 'dist\E10_RPA_Submitter.exe')"
