<#
.SYNOPSIS
  Registers a Windows Task Scheduler job that runs sync_passport.py every 60 minutes.

.USAGE
  Right-click > Run with PowerShell, or from a PowerShell prompt:
      powershell -ExecutionPolicy Bypass -File .\install_task.ps1
  Remove the task again with:
      powershell -ExecutionPolicy Bypass -File .\install_task.ps1 -Uninstall

.NOTES
  The task runs as the current user, only while that user is logged on
  (LogonType Interactive). That is the simplest option that still lets the
  script reach a network share on the machine PC with your normal credentials.
  If you need it to run while logged off, re-register with
      -RunWhenLoggedOff   (you will be asked for your Windows password)
#>
param(
    [int]$IntervalMinutes = 60,
    [string]$TaskName = "Embroidery Passport Sync",
    [switch]$RunWhenLoggedOff,
    [switch]$Uninstall
)

$ErrorActionPreference = "Stop"
$here   = Split-Path -Parent $MyInvocation.MyCommand.Path
$script = Join-Path $here "sync_passport.py"

if ($Uninstall) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
    Write-Host "Task '$TaskName' removed."
    exit 0
}

# Resolve the Python interpreter (py launcher preferred, then python.exe on PATH)
$py = (Get-Command py.exe -ErrorAction SilentlyContinue).Source
if (-not $py) { $py = (Get-Command python.exe -ErrorAction SilentlyContinue).Source }
if (-not $py) { throw "Python not found on PATH. Install Python 3.10+ first." }
$pyArgs = if ($py -like "*py.exe") { "-3 `"$script`"" } else { "`"$script`"" }

$action  = New-ScheduledTaskAction -Execute $py -Argument $pyArgs -WorkingDirectory $here
$trigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) `
            -RepetitionInterval (New-TimeSpan -Minutes $IntervalMinutes)
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Minutes 30) `
            -MultipleInstances IgnoreNew -RunOnlyIfNetworkAvailable

$user = "$env:USERDOMAIN\$env:USERNAME"
if ($RunWhenLoggedOff) {
    $cred = Get-Credential -UserName $user -Message "Windows password for the account that runs the sync"
    Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings `
        -User $cred.UserName -Password $cred.GetNetworkCredential().Password -RunLevel Limited -Force | Out-Null
} else {
    $principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Limited
    Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings `
        -Principal $principal -Force | Out-Null
}

Write-Host "Task '$TaskName' registered: runs '$py $pyArgs' every $IntervalMinutes minutes."
Write-Host "Check it in Task Scheduler, or run it now with:  Start-ScheduledTask -TaskName '$TaskName'"
