$ErrorActionPreference = "Stop"

if ($env:OS -ne "Windows_NT") {
    throw "Windows-EXEs müssen auf Windows gebaut werden."
}

$ProjectRoot = $PSScriptRoot
$BuildVenv = Join-Path $ProjectRoot ".build-venv"
$BuildPython = Join-Path $BuildVenv "Scripts\python.exe"
$BuildDir = Join-Path $ProjectRoot "build"
$DistDir = Join-Path $ProjectRoot "dist"
$SpecDir = Join-Path $BuildDir "spec"

if (-not (Test-Path -LiteralPath $BuildPython)) {
    $PyLauncher = Get-Command "py.exe" -ErrorAction SilentlyContinue
    if ($PyLauncher) {
        & $PyLauncher.Source -3 -m venv $BuildVenv
    } else {
        $PythonCommand = Get-Command "python.exe" -ErrorAction SilentlyContinue
        if (-not $PythonCommand) {
            throw "Zum Erstellen der EXE wird Python benötigt. Installiere Python 3 und starte dieses Skript erneut."
        }
        & $PythonCommand.Source -m venv $BuildVenv
    }
    if ($LASTEXITCODE -ne 0) {
        throw "Die Build-Umgebung konnte nicht erstellt werden."
    }
}

New-Item -ItemType Directory -Force -Path $BuildDir, $DistDir, $SpecDir | Out-Null

& $BuildPython -m pip install --disable-pip-version-check -r (Join-Path $ProjectRoot "requirements-build.txt")
if ($LASTEXITCODE -ne 0) {
    throw "Die Build-Abhängigkeiten konnten nicht installiert werden. Prüfe die Internetverbindung und starte das Skript erneut."
}

$MainSource = Join-Path $ProjectRoot "cudy_auto_refill.py"
$FirstStartSource = Join-Path $ProjectRoot "cudy_auto_refill_first_start.py"
$BuildTargets = @(
    @{
        Name = "CudyAutoRefill"
        Entry = $MainSource
        ExtraArgs = @()
    },
    @{
        Name = "CudyAutoRefill_FirstStart"
        Entry = $FirstStartSource
        ExtraArgs = @(
            "--add-data"
            "$MainSource;."
            "--hidden-import"
            "cudy_auto_refill"
        )
    }
)

foreach ($Target in $BuildTargets) {
    $TargetWorkDir = Join-Path (Join-Path $BuildDir "pyinstaller") $Target.Name
    $PyInstallerArgs = @(
        "--noconfirm"
        "--clean"
        "--onefile"
        "--windowed"
        "--name"
        $Target.Name
        "--distpath"
        $DistDir
        "--workpath"
        $TargetWorkDir
        "--specpath"
        $SpecDir
    )
    $PyInstallerArgs += $Target.ExtraArgs
    $PyInstallerArgs += $Target.Entry

    & $BuildPython -m PyInstaller @PyInstallerArgs
    if ($LASTEXITCODE -ne 0) {
        throw "PyInstaller konnte $($Target.Name).exe nicht erstellen."
    }

    $Executable = Join-Path $DistDir "$($Target.Name).exe"
    if (-not (Test-Path -LiteralPath $Executable)) {
        throw "Der Build wurde beendet, aber $($Target.Name).exe wurde nicht gefunden."
    }

    $SizeMb = [math]::Round((Get-Item -LiteralPath $Executable).Length / 1MB, 1)
    Write-Host "EXE erstellt: $Executable ($SizeMb MB)"
}
