# Start or stop polymas2api, keeping its PID in polymas2api.pid.
#
# wmic is deprecated on recent Windows builds, so the process lookup goes
# through PowerShell instead of shelling out to a tool that is on its way out.
param(
  [ValidateSet('start', 'stop', 'status')]
  [string]$Action = 'start',
  [string]$Config = 'config.json'
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root
# One pid file per configuration, so several instances can coexist. The
# default config keeps the historical name.
$configBase = [System.IO.Path]::GetFileNameWithoutExtension($Config)
$pidFile = if ($configBase -eq 'config') {
  Join-Path $root 'polymas2api.pid'
} else {
  Join-Path $root ("polymas2api.{0}.pid" -f $configBase)
}

#: Which instance this script manages. Matching every `polymas2api.cli`
#: process made a second instance on another port look like "already running",
#: so the config path is part of the match.
$resolvedConfig = try { (Resolve-Path -LiteralPath $Config).Path } catch { $Config }

function Get-GatewayProcess {
  $names = @($Config, $resolvedConfig) | Where-Object { $_ } |
    ForEach-Object { [regex]::Escape($_) }
  $pattern = 'polymas2api\.cli.*-c\s+"?(?:' + ($names -join '|') + ')"?(\s|$)'
  Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
    Where-Object { $_.CommandLine -and $_.CommandLine -match $pattern } |
    Select-Object -First 1
}

switch ($Action) {
  'start' {
    $existing = Get-GatewayProcess
    if ($existing) {
      Set-Content -Path $pidFile -Value $existing.ProcessId -Encoding ascii
      Write-Host "already running, pid $($existing.ProcessId)"
      exit 0
    }
    if (-not (Test-Path (Join-Path $root 'logs'))) {
      New-Item -ItemType Directory -Path (Join-Path $root 'logs') | Out-Null
    }
    $suffix = if ($configBase -eq 'config') { '' } else { '.' + $configBase }
    $stdout = Join-Path $root ("logs\stdout{0}.log" -f $suffix)
    $stderr = Join-Path $root ("logs\stderr{0}.log" -f $suffix)
    $process = Start-Process -FilePath 'python' `
      -ArgumentList '-m', 'polymas2api.cli', '-c', $Config `
      -WorkingDirectory $root -RedirectStandardOutput $stdout `
      -RedirectStandardError $stderr -WindowStyle Hidden -PassThru
    Set-Content -Path $pidFile -Value $process.Id -Encoding ascii
    Write-Host "started, pid $($process.Id)"
  }
  'stop' {
    if (-not (Test-Path $pidFile)) {
      Write-Host 'no pid file; nothing to stop'
      exit 0
    }
    $pidValue = (Get-Content $pidFile -Raw).Trim()
    try {
      Stop-Process -Id $pidValue -Force -ErrorAction Stop
      Write-Host "stopped $pidValue"
    } catch {
      Write-Host "process $pidValue is not running"
    }
    Remove-Item $pidFile -ErrorAction SilentlyContinue
  }
  'status' {
    $existing = Get-GatewayProcess
    if ($existing) {
      Write-Host "running, pid $($existing.ProcessId)"
    } else {
      Write-Host 'not running'
    }
  }
}
