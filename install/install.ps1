# SPDX-License-Identifier: Apache-2.0 WITH LLVM-exception
param(
    [string]$Version = "latest",
    [string]$InstallDir = "",
    [switch]$Force
)

$ErrorActionPreference = "Stop"
$repo = "https://github.com/Hexadecimall/LLVM-CLI"
$headers = @{ "User-Agent" = "LLVM-CLI-installer" }

if ($Version -eq "latest" -and $env:LLVM_CLI_VERSION) {
    $Version = $env:LLVM_CLI_VERSION
}
if (-not $InstallDir) {
    $InstallDir = $env:LLVM_CLI_INSTALL_DIR
}
if (-not $InstallDir) {
    $InstallDir = Join-Path ([Environment]::GetFolderPath("LocalApplicationData")) "Programs\LLVM-CLI"
}
$InstallDir = [IO.Path]::GetFullPath($InstallDir)

switch ($env:PROCESSOR_ARCHITECTURE) {
    "AMD64" { $platform = "windows-x86_64" }
    "ARM64" { $platform = "windows-arm64" }
    default { throw "No native LLVM-CLI release for Windows/$env:PROCESSOR_ARCHITECTURE" }
}

if ($Version -eq "latest") {
    $release = Invoke-RestMethod -Uri "https://api.github.com/repos/Hexadecimall/LLVM-CLI/releases/latest" -Headers $headers
    $Version = $release.tag_name
}
if ($Version -notmatch '^[A-Za-z0-9._-]+$') {
    throw "Invalid release tag"
}
$base = "$repo/releases/download/$Version"

function Get-Fields([string]$Line) {
    return ,($Line.Trim() -split '\s+')
}

function Assert-Hash([string]$Hash) {
    if ($Hash -cnotmatch '^[0-9a-f]{64}$') {
        throw "Invalid SHA-256 in release manifest"
    }
}

function Download-Asset([string]$Name, [string]$Path) {
    Invoke-WebRequest -Uri "$base/$Name" -OutFile $Path -UseBasicParsing -Headers $headers | Out-Null
}

New-Item -ItemType Directory -Path $InstallDir -Force | Out-Null
$target = Join-Path $InstallDir "llvm.exe"
if (Test-Path -LiteralPath $target -PathType Container) {
    throw "$target is a directory"
}
if ((Test-Path -LiteralPath $target) -and -not $Force) {
    $existingVersion = @(& $target --version 2>$null) | Select-Object -First 1
    if ($existingVersion -notmatch '^LLVM-CLI ') {
        throw "$target exists and is not LLVM-CLI; use -Force to replace it"
    }
    if ($existingVersion -match '^LLVM-CLI v?([0-9]+\.[0-9]+\.[0-9]+)(?:\s|$)') {
        $installedNumber = [version]$Matches[1]
        if ($Version -match '^v?([0-9]+\.[0-9]+\.[0-9]+)$' -and
            $installedNumber -gt [version]$Matches[1]) {
            Write-Host "LLVM-CLI $installedNumber is newer than release $Version; keeping $target (use -Force to downgrade)"
            return
        }
    }
}

$work = Join-Path $InstallDir (".llvm-cli-install-" + [guid]::NewGuid().ToString("N"))
New-Item -ItemType Directory -Path $work | Out-Null
try {
    $manifestPath = Join-Path $work "llvm-cli-manifest-v1.txt"
    Download-Asset "llvm-cli-manifest-v1.txt" $manifestPath
    $lines = [IO.File]::ReadAllLines($manifestPath)
    if ($lines.Count -eq 0 -or $lines[0] -cnotin @("llvm-cli-release-v1", "llvm-cli-release-v2")) {
        throw "Unsupported release manifest"
    }
    $compression = if ($lines[0] -ceq "llvm-cli-release-v2") { "gzip" } else { "none" }

    $binaryEntries = @($lines | Where-Object {
        $fields = Get-Fields $_
        $fields.Count -ge 2 -and $fields[0] -ceq "binary" -and $fields[1] -ceq $platform
    })
    if ($binaryEntries.Count -ne 1) {
        throw "Release $Version has no native $platform binary"
    }
    $binary = Get-Fields $binaryEntries[0]
    if ($compression -eq "gzip") {
        if ($binary.Count -ne 6 -or $binary[5] -cne "gzip") {
            throw "Invalid compressed binary manifest entry"
        }
    } elseif ($binary.Count -ne 5) {
        throw "Invalid binary manifest entry"
    }
    [long]$expectedSize = $binary[2]
    $expectedHash = $binary[3]
    [int]$partCount = $binary[4]
    Assert-Hash $expectedHash
    if ($expectedSize -le 0 -or $partCount -le 0 -or $partCount -gt 999) {
        throw "Invalid binary size or part count"
    }

    if ((Test-Path -LiteralPath $target -PathType Leaf) -and -not $Force -and
        (Get-Item -LiteralPath $target).Length -eq $expectedSize -and
        (Get-FileHash -LiteralPath $target -Algorithm SHA256).Hash.ToLowerInvariant() -ceq $expectedHash) {
        Write-Host "LLVM-CLI $Version is already installed in $InstallDir"
        return
    }

    $image = Join-Path $work "llvm.exe"
    $payload = if ($compression -eq "gzip") {
        Join-Path $work "llvm.exe.gz"
    } else { $image }
    $output = [IO.File]::Open(
        $payload, [IO.FileMode]::CreateNew, [IO.FileAccess]::Write,
        [IO.FileShare]::None
    )
    try {
        for ($index = 0; $index -lt $partCount; $index++) {
            $partEntries = @($lines | Where-Object {
                $fields = Get-Fields $_
                $fields.Count -ge 3 -and $fields[0] -ceq "part" -and
                    $fields[1] -ceq $platform -and $fields[2] -ceq [string]$index
            })
            if ($partEntries.Count -ne 1) { throw "Missing or duplicate part $index" }
            $part = Get-Fields $partEntries[0]
            if ($part.Count -ne 5) { throw "Invalid part $index manifest entry" }
            [long]$partSize = $part[3]
            $partHash = $part[4]
            Assert-Hash $partHash
            if ($partSize -le 0 -or $partSize -gt 1073741824) {
                throw "Invalid size for part $index"
            }
            $partName = "llvm-cli-{0}.part{1:D3}" -f $platform, $index
            $partPath = Join-Path $work $partName
            Download-Asset $partName $partPath
            if ((Get-Item -LiteralPath $partPath).Length -ne $partSize) {
                throw "Size mismatch: $partName"
            }
            if ((Get-FileHash -LiteralPath $partPath -Algorithm SHA256).Hash.ToLowerInvariant() -cne $partHash) {
                throw "SHA-256 mismatch: $partName"
            }
            $inputFile = [IO.File]::OpenRead($partPath)
            try { $inputFile.CopyTo($output) } finally { $inputFile.Dispose() }
            Remove-Item -LiteralPath $partPath
        }
    } finally {
        $output.Dispose()
    }

    if ($compression -eq "gzip") {
        $compressed = [IO.File]::OpenRead($payload)
        $decompressed = [IO.File]::Open(
            $image, [IO.FileMode]::CreateNew, [IO.FileAccess]::Write,
            [IO.FileShare]::None
        )
        $gzip = [IO.Compression.GZipStream]::new(
            $compressed, [IO.Compression.CompressionMode]::Decompress
        )
        try { $gzip.CopyTo($decompressed) } finally {
            $gzip.Dispose()
            $decompressed.Dispose()
        }
        Remove-Item -LiteralPath $payload
    }

    if ((Get-Item -LiteralPath $image).Length -ne $expectedSize) {
        throw "Assembled binary size mismatch"
    }
    if ((Get-FileHash -LiteralPath $image -Algorithm SHA256).Hash.ToLowerInvariant() -cne $expectedHash) {
        throw "Assembled binary SHA-256 mismatch"
    }
    & $image --version | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "Downloaded LLVM-CLI did not start" }

    if (Test-Path -LiteralPath $target) {
        $backup = Join-Path $work "previous-llvm.exe"
        [IO.File]::Replace($image, $target, $backup)
        Remove-Item -LiteralPath $backup
    } else {
        [IO.File]::Move($image, $target)
    }

    $userPath = [Environment]::GetEnvironmentVariable("Path", "User")
    if ($InstallDir -notin @($userPath -split ';')) {
        $updatedPath = if ($userPath) { "$userPath;$InstallDir" } else { $InstallDir }
        [Environment]::SetEnvironmentVariable("Path", $updatedPath, "User")
    }
    if ($InstallDir -notin @($env:Path -split ';')) {
        $env:Path += ";$InstallDir"
    }
    Write-Host "Installed LLVM-CLI $Version for $platform in $InstallDir"
    Write-Host "Open a new terminal to use llvm.exe."
} finally {
    if (Test-Path -LiteralPath $work) {
        Remove-Item -LiteralPath $work -Recurse -Force
    }
}
