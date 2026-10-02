# Setup script for the JobApplier Watchlist Refresh scheduled task.
# Idempotent, Windows PowerShell 5.1 compatible.
# Run with -DryRun to see what would be done without making changes.

param(
    [switch]$DryRun
)

# Resolve the repo root from this script's parent directory
$repoRoot = Split-Path -Parent $PSScriptRoot

Write-Host "Repo root: $repoRoot"

# The command to run: cmd.exe /c followed by the full path to refresh.cmd, quoted correctly
$refreshCmd = Join-Path -Path $repoRoot -ChildPath 'scripts\refresh.cmd'
$taskAction = New-ScheduledTaskAction -Execute 'cmd.exe' -Argument ('/c "' + $refreshCmd + '"') -WorkingDirectory $repoRoot

# Trigger 1: Daily at 09:00
$dailyTrigger = New-ScheduledTaskTrigger -Daily -At 09:00

# Trigger 2: At logon for the current user with a 10-minute delay
$me = "$env:USERDOMAIN\$env:USERNAME"
$atLogonTrigger = New-ScheduledTaskTrigger -AtLogon -User $me
# Set the delay to 10 minutes
$atLogonTrigger.Delay = 'PT10M'

# Settings: WakeToRun + StartWhenAvailable + battery/power settings + timeout + IgnoreNew
$taskSettings = New-ScheduledTaskSettingsSet `
    -WakeToRun `
    -StartWhenAvailable `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit (New-TimeSpan -Hours 1) `
    -MultipleInstances IgnoreNew

# Principal: current user, Interactive LogonType
# WakeToRun allows the task to wake the machine; StartWhenAvailable + the at-logon trigger
# (with a 10-minute delay) ensure we catch up even if the machine sleeps through 09:00.
# Interactive LogonType preserves the network profile without requiring password prompts.
$principal = New-ScheduledTaskPrincipal -UserId $me -LogonType Interactive

# Build the task object
$taskParams = @{
    'Action' = $taskAction
    'Trigger' = @($dailyTrigger, $atLogonTrigger)
    'Settings' = $taskSettings
    'Principal' = $principal
    'Description' = 'Daily watchlist refresh for JobApplier; runs at 09:00 and on logon (10m delay) to catch up if the machine was asleep.'
}

# Task name
$taskName = 'JobApplier Watchlist Refresh'

if ($DryRun) {
    Write-Host "`n[DRY RUN] Would register/update task: $taskName"
    Write-Host "  Command: cmd.exe /c `"$refreshCmd`""
    Write-Host "  Working directory: $repoRoot"
    Write-Host "  Triggers:"
    Write-Host "    - Daily at 09:00"
    Write-Host "    - At logon (10m delay)"
    Write-Host "  Settings:"
    Write-Host "    - WakeToRun: True"
    Write-Host "    - StartWhenAvailable: True"
    Write-Host "    - AllowStartIfOnBatteries: True"
    Write-Host "    - DontStopIfGoingOnBatteries: True"
    Write-Host "    - ExecutionTimeLimit: 1 hour"
    Write-Host "    - MultipleInstances: IgnoreNew"
    Write-Host "  Principal:"
    Write-Host "    - User: $me"
    Write-Host "    - LogonType: Interactive"
    Write-Host ""
    Write-Host "Wake timers check (powercfg /q SCHEME_CURRENT SUB_SLEEP RTCWAKE):"
    & powercfg /q SCHEME_CURRENT SUB_SLEEP RTCWAKE
    Write-Host ""
    Write-Host "[DRY RUN] No changes made."
    exit 0
}

# Register-ScheduledTask -Force creates or replaces, so re-running is idempotent.
# (Set-ScheduledTask has no -Description parameter, so it is not used.)
Write-Host "Registering '$taskName' (create or update)..."
Register-ScheduledTask @taskParams -TaskName $taskName -Force | Out-Null
Write-Host "Task registered."

# Print the resulting settings
Write-Host "`nFinal task settings:"
$finalTask = Get-ScheduledTask -TaskName $taskName
$finalTask | Select-Object -ExpandProperty Settings | Format-List
Write-Host "Triggers:"
$finalTask | Select-Object -ExpandProperty Triggers | Format-List

# Print wake-timer status reminder
Write-Host "`n--- Wake Timers Status ---"
Write-Host "NOTE: WakeToRun requires 'Allow wake timers' to be enabled in the power plan; this script does not change it."
Write-Host "Checking current status with 'powercfg /q SCHEME_CURRENT SUB_SLEEP RTCWAKE':"
Write-Host ""
& powercfg /q SCHEME_CURRENT SUB_SLEEP RTCWAKE
Write-Host ""
