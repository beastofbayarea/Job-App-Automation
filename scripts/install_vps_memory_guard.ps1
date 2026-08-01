# Adds bounded swap headroom for concurrent browser workloads on the shared VPS.
param(
    [string]$ConfigPath = "config/vps_config.json",
    [ValidateRange(512, 4096)]
    [int]$SwapSizeMiB = 2048,
    [ValidateRange(1, 300)]
    [int]$TimeoutSeconds = 60
)

. "$PSScriptRoot\lib\vps_script_helpers.ps1"

if (-not (Test-Path -LiteralPath $ConfigPath)) {
    Write-Error "VPS config not found at $ConfigPath"
    exit 1
}
try {
    $Config = Get-Content -LiteralPath $ConfigPath -Raw | ConvertFrom-Json
} catch {
    Write-Error "VPS config at $ConfigPath is not valid JSON."
    exit 1
}

$VpsHost = [string]$Config.vps.host
$SshUser = [string]$Config.vps.ssh_user
$SshPassword = [string]$Config.vps.ssh_password.value
$SshHostKey = [string]$Config.vps.ssh_host_key
$SshPort = if ($null -ne $Config.vps.ssh_port) { [int]$Config.vps.ssh_port } else { 22 }
if (-not $VpsHost -or -not $SshUser -or -not $SshPassword -or -not $SshHostKey) {
    Write-Error "$ConfigPath is missing required pinned VPS connection settings."
    exit 1
}
if ($SshPort -lt 1 -or $SshPort -gt 65535) {
    Write-Error "$ConfigPath contains an invalid vps.ssh_port."
    exit 1
}

$PlinkCmd = Get-Command plink -ErrorAction SilentlyContinue
if (-not $PlinkCmd) {
    Write-Error "plink.exe must be available on PATH."
    exit 1
}

$RemoteCommand = @"
set -eu
swap_size_mib=$SwapSizeMiB
swap_path=/swapfile
if [ "`$(swapon --noheadings --show=NAME | wc -l)" -eq 0 ]; then
  required_kib=`$((swap_size_mib * 1024 + 1048576))
  available_kib=`$(df --output=avail -k / | tail -n 1 | tr -d ' ')
  if [ "`$available_kib" -lt "`$required_kib" ]; then
    printf 'Insufficient disk headroom: required=%sKiB available=%sKiB\n' \
      "`$required_kib" "`$available_kib" >&2
    exit 4
  fi
  if [ -e "`$swap_path" ] && ! file "`$swap_path" | grep -q 'swap file'; then
    printf 'Refusing to overwrite non-swap path: %s\n' "`$swap_path" >&2
    exit 5
  fi
  if [ ! -e "`$swap_path" ]; then
    if ! fallocate -l "`$swap_size_mib"M "`$swap_path"; then
      dd if=/dev/zero of="`$swap_path" bs=1M count="`$swap_size_mib" status=none
    fi
    chmod 0600 "`$swap_path"
    mkswap "`$swap_path" >/dev/null
  fi
  swapon "`$swap_path"
fi
if ! grep -Eq '^[[:space:]]*/swapfile[[:space:]]+none[[:space:]]+swap[[:space:]]' /etc/fstab; then
  printf '%s\n' '/swapfile none swap sw 0 0' >> /etc/fstab
fi
printf '%s\n' \
  '# Managed by Job App Automation VPS memory guard.' \
  'vm.swappiness=10' \
  'vm.vfs_cache_pressure=50' \
  > /etc/sysctl.d/99-job-app-memory.conf
sysctl --system >/dev/null
printf '%s\n' '=== ACTIVE SWAP ==='
swapon --show
printf '%s\n' '=== MEMORY ==='
free -h
printf 'vm.swappiness='
sysctl -n vm.swappiness
"@

$PasswordFile = Join-Path ([IO.Path]::GetTempPath()) "vps-memory-guard-$([guid]::NewGuid().ToString('N')).txt"
try {
    [IO.File]::WriteAllText(
        $PasswordFile,
        $SshPassword,
        [Text.UTF8Encoding]::new($false)
    )
    $Execution = Invoke-ExternalCommandWithTimeout `
        -FilePath $PlinkCmd.Source `
        -ArgumentList @(
            "-ssh",
            "-batch",
            "-P",
            $SshPort,
            "-hostkey",
            $SshHostKey,
            "-pwfile",
            $PasswordFile,
            "$SshUser@$VpsHost",
            $RemoteCommand
        ) `
        -TimeoutSeconds $TimeoutSeconds
    foreach ($OutputLine in $Execution.Output) {
        Write-Output ([string]$OutputLine)
    }
    if ($Execution.TimedOut) {
        Write-Error "VPS memory guard installation timed out after $TimeoutSeconds seconds."
    }
    exit $Execution.ExitCode
} finally {
    Remove-Item -LiteralPath $PasswordFile -Force -ErrorAction SilentlyContinue
}
