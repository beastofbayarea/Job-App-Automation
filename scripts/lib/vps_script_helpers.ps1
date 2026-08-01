function ConvertTo-PosixShellLiteral {
    param(
        [AllowEmptyString()]
        [string]$Value
    )

    if ($Value.Contains([char]0) -or $Value.Contains("`r") -or $Value.Contains("`n")) {
        throw "POSIX shell arguments cannot contain null or newline characters."
    }

    $SingleQuote = [string][char]39
    $DoubleQuote = [string][char]34
    $EmbeddedSingleQuote = $SingleQuote + $DoubleQuote + $SingleQuote + $DoubleQuote + $SingleQuote
    return $SingleQuote + $Value.Replace($SingleQuote, $EmbeddedSingleQuote) + $SingleQuote
}

function ConvertTo-LfLineEndings {
    param(
        [AllowEmptyString()]
        [string]$Value
    )

    return $Value.Replace("`r`n", "`n").Replace("`r", "`n")
}

function Read-VpsConnectionConfig {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Path
    )

    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        throw "VPS config not found at $Path"
    }

    try {
        $Config = Get-Content -LiteralPath $Path -Raw -ErrorAction Stop | ConvertFrom-Json
    } catch {
        throw "VPS config at $Path is not valid JSON."
    }

    $Connection = [pscustomobject]@{
        Host = [string]$Config.vps.host
        User = [string]$Config.vps.ssh_user
        Password = [string]$Config.vps.ssh_password.value
        HostKey = [string]$Config.vps.ssh_host_key
        Port = if ($null -ne $Config.vps.ssh_port) { [int]$Config.vps.ssh_port } else { 22 }
    }
    if (
        [string]::IsNullOrWhiteSpace($Connection.Host) -or
        [string]::IsNullOrWhiteSpace($Connection.User) -or
        [string]::IsNullOrWhiteSpace($Connection.Password) -or
        [string]::IsNullOrWhiteSpace($Connection.HostKey)
    ) {
        throw "$Path is missing required pinned VPS connection settings."
    }
    if ($Connection.Port -lt 1 -or $Connection.Port -gt 65535) {
        throw "$Path contains an invalid vps.ssh_port."
    }

    return $Connection
}

function Get-RequiredCommandPath {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Name
    )

    $Command = Get-Command $Name -ErrorAction SilentlyContinue
    if (-not $Command) {
        throw "$Name must be available on PATH."
    }
    if ($Command.Path) {
        return [string]$Command.Path
    }
    return [string]$Command.Source
}

function New-TemporaryPasswordFile {
    param(
        [Parameter(Mandatory = $true)]
        [AllowEmptyString()]
        [string]$Password,
        [string]$Prefix = "job-app-vps"
    )

    $Path = Join-Path ([IO.Path]::GetTempPath()) "$Prefix-$([guid]::NewGuid().ToString('N')).txt"
    [IO.File]::WriteAllText($Path, $Password, [Text.UTF8Encoding]::new($false))
    return $Path
}

function Invoke-ExternalCommandWithTimeout {
    param(
        [Parameter(Mandatory = $true)]
        [string]$FilePath,
        [Parameter(Mandatory = $true)]
        [AllowEmptyCollection()]
        [object[]]$ArgumentList,
        [ValidateRange(1, 3600)]
        [int]$TimeoutSeconds
    )

    $InvocationId = [guid]::NewGuid().ToString("N")
    $TemporaryDirectory = [IO.Path]::GetTempPath()
    $WrapperPath = Join-Path $TemporaryDirectory "external-command-$InvocationId.ps1"
    $ArgumentsPath = Join-Path $TemporaryDirectory "external-command-$InvocationId.json"
    $Wrapper = @'
param([string]$ArgumentsPath)
$Payload = Get-Content -LiteralPath $ArgumentsPath -Raw | ConvertFrom-Json
& ([string]$Payload.file_path) @($Payload.arguments)
exit $LASTEXITCODE
'@
    $Payload = @{
        file_path = $FilePath
        arguments = @(
            $ArgumentList |
                ForEach-Object { ConvertTo-LfLineEndings ([string]$_) }
        )
    } | ConvertTo-Json -Depth 4

    $Process = $null
    try {
        [IO.File]::WriteAllText(
            $WrapperPath,
            $Wrapper,
            [Text.UTF8Encoding]::new($false)
        )
        [IO.File]::WriteAllText(
            $ArgumentsPath,
            $Payload,
            [Text.UTF8Encoding]::new($false)
        )

        $PowerShellPath = (Get-Process -Id $PID).Path
        $StartInfo = [Diagnostics.ProcessStartInfo]::new()
        $StartInfo.FileName = $PowerShellPath
        $StartInfo.UseShellExecute = $false
        $StartInfo.CreateNoWindow = $true
        $StartInfo.RedirectStandardOutput = $true
        $StartInfo.RedirectStandardError = $true
        foreach ($Argument in @("-NoProfile", "-File", $WrapperPath, $ArgumentsPath)) {
            [void]$StartInfo.ArgumentList.Add($Argument)
        }
        $Process = [Diagnostics.Process]::new()
        $Process.StartInfo = $StartInfo
        [void]$Process.Start()
        $StandardOutputTask = $Process.StandardOutput.ReadToEndAsync()
        $StandardErrorTask = $Process.StandardError.ReadToEndAsync()
        $TimedOut = -not $Process.WaitForExit($TimeoutSeconds * 1000)
        if ($TimedOut) {
            try {
                if (-not $Process.HasExited) {
                    $Process.Kill($true)
                }
            } catch {
                if (-not $Process.HasExited) {
                    $Process.Kill()
                }
            }
        }

        # The parameterless wait guarantees process termination after a timeout and
        # lets redirected asynchronous readers observe closed stdout/stderr handles.
        $Process.WaitForExit()
        $Output = @(
            $StandardOutputTask.GetAwaiter().GetResult(),
            $StandardErrorTask.GetAwaiter().GetResult()
        ) |
            Where-Object { -not [string]::IsNullOrEmpty($_) } |
            ForEach-Object { $_ -split "\r?\n" } |
            Where-Object { $_ -ne "" }

        return [pscustomobject]@{
            TimedOut = $TimedOut
            ExitCode = if ($TimedOut) { 124 } else { $Process.ExitCode }
            Output = @($Output)
        }
    } finally {
        if ($null -ne $Process) {
            try {
                if (-not $Process.HasExited) {
                    $Process.Kill($true)
                    $Process.WaitForExit()
                }
            } catch {
                # Best-effort cleanup for failures before normal timeout handling.
            } finally {
                $Process.Dispose()
            }
        }
        Remove-Item -LiteralPath $WrapperPath -Force -ErrorAction SilentlyContinue
        Remove-Item -LiteralPath $ArgumentsPath -Force -ErrorAction SilentlyContinue
    }
}
