param(
    [string]$Version = "",
    [ValidateSet("production", "staging", "dev")]
    [string]$Environment = "production"
)

$ErrorActionPreference = "Stop"

if (-not $Version) {
    $VersionMatch = Select-String -Path "pyproject.toml" -Pattern '^version = "([^"]+)"$' | Select-Object -First 1
    if (-not $VersionMatch) {
        throw "Could not determine version from pyproject.toml"
    }
    $Version = $VersionMatch.Matches[0].Groups[1].Value
}

# Resolve environment-specific metadata
switch ($Environment) {
    "production" {
        $AppName    = "Focale Relay"
        $ExeName    = "focale-relay"
        $InstallDir = "{autopf}\FocaleLocalRelay\Focale Relay"
        $AppId      = "11A3125E-D7EA-487D-9998-67E95343F4A5"
    }
    "staging" {
        $AppName    = "Focale Relay Staging"
        $ExeName    = "focale-relay-staging"
        $InstallDir = "{autopf}\FocaleLocalRelay\Focale Relay Staging"
        $AppId      = "5B3A2E10-7C4D-4F9A-B1E8-2D6F0A3C5E71"
    }
    "dev" {
        $AppName    = "Focale Relay Dev"
        $ExeName    = "focale-relay-dev"
        $InstallDir = "{autopf}\FocaleLocalRelay\Focale Relay Dev"
        $AppId      = "9E7C4B20-3A5F-4D8C-C2F9-3E70B4D6F820"
    }
}

$IconPath = ""
if (Test-Path "src/focale_local_relay/assets/app-icon.ico") {
    $IconPath = (Resolve-Path "src/focale_local_relay/assets/app-icon.ico").Path
}

# Bake the environment into the source
Set-Content -Path "src/focale_local_relay/_environment.py" -Value @"
# This file is generated during the build process. Do not edit manually.
# The value is baked at build time to produce environment-specific applications.
ENVIRONMENT = "$Environment"
"@

python -m pip install --upgrade pip
python -m pip install .[dev]

$PyInstallerArgs = @(
    "--noconfirm",
    "--clean",
    "--onedir",
    "--windowed",
    "--name", $ExeName,
    "--paths", "src",
    "--collect-data", "focale_local_relay",
    "--collect-submodules", "arcsecond",
    "--collect-submodules", "focale_local_relay"
)
if ($IconPath) {
    $PyInstallerArgs += @("--icon", $IconPath)
}
$PyInstallerArgs += "src/focale_local_relay/gui_main.py"
pyinstaller @PyInstallerArgs

$IsccArgs = @(
    "/DMyAppVersion=$Version",
    "/DMyAppName=$AppName",
    "/DMyAppId=$AppId",
    "/DMyExeName=$ExeName.exe",
    "/DMyDefaultDirName=$InstallDir"
)
if ($IconPath) {
    $IsccArgs += "/DMySetupIconFile=$IconPath"
}
$IsccArgs += "packaging/windows/focale.iss"
iscc @IsccArgs
