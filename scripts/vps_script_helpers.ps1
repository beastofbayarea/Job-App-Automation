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
        $StandardOutput = $Process.StandardOutput.ReadToEndAsync()
        $StandardError = $Process.StandardError.ReadToEndAsync()
        $Completed = $Process.WaitForExit($TimeoutSeconds * 1000)
        if (-not $Completed) {
            try {
                $Process.Kill($true)
            } catch {
                try {
                    $Process.Kill()
                } catch {
                    # The process may have exited between the timeout and kill request.
                }
            }
            [void]$Process.WaitForExit(2000)
            return [pscustomobject]@{
                TimedOut = $true
                ExitCode = 124
                Output = @()
            }
        }
        $Output = @(
            $StandardOutput.GetAwaiter().GetResult(),
            $StandardError.GetAwaiter().GetResult()
        ) |
            Where-Object { -not [string]::IsNullOrEmpty($_) } |
            ForEach-Object { $_ -split "\r?\n" } |
            Where-Object { $_ -ne "" }

        return [pscustomobject]@{
            TimedOut = $false
            ExitCode = $Process.ExitCode
            Output = @($Output)
        }
    } finally {
        Remove-Item -LiteralPath $WrapperPath -Force -ErrorAction SilentlyContinue
        Remove-Item -LiteralPath $ArgumentsPath -Force -ErrorAction SilentlyContinue
    }
}
