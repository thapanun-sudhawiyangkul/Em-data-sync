<#
  Embroidery Log Sync - setup window.
  Started by Setup.bat. Lets you choose the data folder, enter the Supabase
  details, test them, and switch the automatic schedule on or off.

  Silent use (no window):
    powershell -ExecutionPolicy Bypass -File setup.ps1 -Silent -DataDir "\\NAS\logs" `
        -Url "https://xxx.supabase.co" -Key "sb_secret_..." -IntervalMinutes 60 [-NoSchedule]
#>
param(
    [string]$DataDir,
    [string]$Url,
    [string]$Key,
    [int]$IntervalMinutes = 0,
    [string]$TaskName = "Embroidery Log Sync",
    [switch]$Silent,
    [switch]$NoSchedule,
    [switch]$BuildOnly      # build the window, do not show it (self test)
)

$ErrorActionPreference = "Stop"
$root       = Split-Path -Parent $MyInvocation.MyCommand.Path
$exe        = Join-Path $root "app\EmbroiSync.exe"
$configPath = Join-Path $root "config.json"
$hiddenVbs  = Join-Path $root "run_hidden.vbs"

# ------------------------------------------------------------ settings file
function Read-Settings {
    $s = [ordered]@{ data_dir = ""; url = ""; key = ""; interval = 60 }
    if (Test-Path $configPath) {
        try {
            $c = Get-Content -Raw -Encoding UTF8 $configPath | ConvertFrom-Json
            if ($c.data_dir) { $s.data_dir = [string]$c.data_dir }
            if ($c.supabase.url) { $s.url = [string]$c.supabase.url }
            if ($c.supabase.key) { $s.key = [string]$c.supabase.key }
            if ($c.interval_minutes) { $s.interval = [int]$c.interval_minutes }
        } catch { }
    }
    return $s
}

function Save-Settings([string]$dataDir, [string]$url, [string]$key, [int]$interval) {
    $cfg = [ordered]@{
        data_dir         = $dataDir
        file_pattern     = "*.MDF"
        exclude          = @()
        supabase         = [ordered]@{
            url         = $url.Trim().TrimEnd("/")
            key         = $key.Trim()
            table       = "passport_log"
            state_table = "passport_sync_state"
            run_table   = "passport_sync_run"
            batch_size  = 1000
        }
        overlap_rows     = 5
        interval_minutes = $interval
        staging_dir      = "staging"
        keep_copies      = 2
        copy_retries     = 3
        copy_retry_seconds = 20
        log_dir          = "logs"
    }
    $json = $cfg | ConvertTo-Json -Depth 5
    # UTF-8 without BOM
    [System.IO.File]::WriteAllText($configPath, $json, (New-Object System.Text.UTF8Encoding($false)))
}

function Test-Input([string]$dataDir, [string]$url, [string]$key, [int]$interval) {
    $problems = @()
    if (-not $dataDir.Trim()) { $problems += "Choose the data folder." }
    if ($url -notmatch '^https?://.+') { $problems += "Supabase URL must start with https:// (or http:// for a self-hosted server on this network)" }
    if (-not $key.Trim()) { $problems += "Enter the Supabase secret key." }
    elseif ($key -like "sb_publishable_*") { $problems += "That is the publishable key. Use the secret key (sb_secret_...)." }
    if ($interval -lt 5 -or $interval -gt 1440) { $problems += "Interval must be between 5 and 1440 minutes." }
    return $problems
}

# ------------------------------------------------------------- run program
function Invoke-Sync([string[]]$arguments) {
    if (-not (Test-Path $exe)) { return @{ Code = 99; Text = "Program not found: $exe" } }
    $psi = New-Object System.Diagnostics.ProcessStartInfo
    $psi.FileName = $exe
    $psi.Arguments = ($arguments -join " ")
    $psi.WorkingDirectory = $root
    $psi.UseShellExecute = $false
    $psi.RedirectStandardOutput = $true
    $psi.RedirectStandardError = $true
    $psi.CreateNoWindow = $true
    $p = [System.Diagnostics.Process]::Start($psi)
    $errTask = $p.StandardError.ReadToEndAsync()
    $out = $p.StandardOutput.ReadToEnd()
    $p.WaitForExit()
    $text = ($out + $errTask.Result).Trim()
    return @{ Code = $p.ExitCode; Text = $text }
}

# ---------------------------------------------------------------- schedule
function Get-SyncTask { Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue }

function Install-SyncTask([int]$interval) {
    $vbs = "CreateObject(""WScript.Shell"").Run """"""$exe"""""", 0, True"
    [System.IO.File]::WriteAllText($hiddenVbs, $vbs, [System.Text.Encoding]::ASCII)
    $action   = New-ScheduledTaskAction -Execute "wscript.exe" -Argument "`"$hiddenVbs`"" -WorkingDirectory $root
    $trigger  = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(2) -RepetitionInterval (New-TimeSpan -Minutes $interval)
    $logon    = New-ScheduledTaskTrigger -AtLogOn -User "$env:USERDOMAIN\$env:USERNAME"
    $settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -MultipleInstances IgnoreNew `
                  -ExecutionTimeLimit (New-TimeSpan -Minutes 30) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
    $principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType Interactive -RunLevel Limited
    Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger @($trigger, $logon) `
        -Settings $settings -Principal $principal -Force | Out-Null
}

function Remove-SyncTask { Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue }

function Get-TaskText {
    $t = Get-SyncTask
    if (-not $t) { return "Automatic sync: OFF" }
    $i = Get-ScheduledTaskInfo -TaskName $TaskName -ErrorAction SilentlyContinue
    $next = if ($i -and $i.NextRunTime) { $i.NextRunTime.ToString("yyyy-MM-dd HH:mm") } else { "-" }
    return "Automatic sync: ON   (next run $next)"
}

# ------------------------------------------------------------------ silent
if ($Silent) {
    $s = Read-Settings
    if ($DataDir) { $s.data_dir = $DataDir }
    if ($Url) { $s.url = $Url }
    if ($Key) { $s.key = $Key }
    if ($IntervalMinutes -gt 0) { $s.interval = $IntervalMinutes }
    $problems = Test-Input $s.data_dir $s.url $s.key $s.interval
    if ($problems) { $problems | ForEach-Object { Write-Host "FAIL  $_" }; exit 2 }
    Save-Settings $s.data_dir $s.url $s.key $s.interval
    Write-Host "Settings saved to $configPath"
    $r = Invoke-Sync @("--check")
    Write-Host $r.Text
    if ($r.Code -ne 0) { exit 1 }
    if (-not $NoSchedule) { Install-SyncTask $s.interval; Write-Host (Get-TaskText) }
    exit 0
}

# ------------------------------------------------------------------ window
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
[System.Windows.Forms.Application]::EnableVisualStyles()

$s = Read-Settings
$font  = New-Object System.Drawing.Font("Segoe UI", 9.5)
$bold  = New-Object System.Drawing.Font("Segoe UI", 9.5, [System.Drawing.FontStyle]::Bold)

$form = New-Object System.Windows.Forms.Form
$form.Text = "Embroidery Log Sync - Setup"
$form.ClientSize = New-Object System.Drawing.Size(720, 610)
$form.StartPosition = "CenterScreen"
$form.FormBorderStyle = "FixedDialog"
$form.MaximizeBox = $false
$form.Font = $font

function Add-Label([string]$text, [int]$x, [int]$y, [int]$w = 680, $f = $font) {
    $l = New-Object System.Windows.Forms.Label
    $l.Text = $text; $l.Location = New-Object System.Drawing.Point($x, $y)
    $l.Size = New-Object System.Drawing.Size($w, 20); $l.Font = $f
    $form.Controls.Add($l); return $l
}
function Add-Text([int]$x, [int]$y, [int]$w, [string]$value) {
    $t = New-Object System.Windows.Forms.TextBox
    $t.Location = New-Object System.Drawing.Point($x, $y); $t.Size = New-Object System.Drawing.Size($w, 24)
    $t.Text = $value; $form.Controls.Add($t); return $t
}
function Add-Button([string]$text, [int]$x, [int]$y, [int]$w) {
    $b = New-Object System.Windows.Forms.Button
    $b.Text = $text; $b.Location = New-Object System.Drawing.Point($x, $y)
    $b.Size = New-Object System.Drawing.Size($w, 30); $form.Controls.Add($b); return $b
}

[void](Add-Label "1. Folder with the machine log files (.MDF)" 20 15 680 $bold)
$txtDir = Add-Text 20 40 570 $s.data_dir
$btnBrowse = Add-Button "Browse..." 600 37 100
[void](Add-Label "A network folder can be typed directly, for example  \\MYNAS\embroidery\logs" 20 68)

[void](Add-Label "2. Supabase" 20 105 680 $bold)
[void](Add-Label "Project URL" 20 132 120)
$txtUrl = Add-Text 140 129 560 $s.url
[void](Add-Label "Secret key" 20 164 120)
$txtKey = Add-Text 140 161 450 $s.key
$txtKey.UseSystemPasswordChar = $true
$chkShow = New-Object System.Windows.Forms.CheckBox
$chkShow.Text = "Show"; $chkShow.Location = New-Object System.Drawing.Point(605, 161)
$chkShow.Size = New-Object System.Drawing.Size(90, 24); $form.Controls.Add($chkShow)
[void](Add-Label "Supabase > Project Settings > API Keys. Use the secret key (sb_secret_...)." 140 190 560)

[void](Add-Label "3. Automatic sync" 20 228 680 $bold)
[void](Add-Label "Run every" 20 256 80)
$numInterval = New-Object System.Windows.Forms.NumericUpDown
$numInterval.Location = New-Object System.Drawing.Point(100, 253); $numInterval.Size = New-Object System.Drawing.Size(70, 24)
$numInterval.Minimum = 5; $numInterval.Maximum = 1440; $numInterval.Increment = 5
$numInterval.Value = [Math]::Min(1440, [Math]::Max(5, $s.interval)); $form.Controls.Add($numInterval)
[void](Add-Label "minutes" 178 256 70)
$lblTask = Add-Label (Get-TaskText) 270 256 430

$btnTest    = Add-Button "Test settings" 20 295 150
$btnInstall = Add-Button "Save and switch ON" 180 295 170
$btnRun     = Add-Button "Sync now" 360 295 110
$btnOff     = Add-Button "Switch OFF" 480 295 110
$btnLog     = Add-Button "Open log" 600 295 100
$btnInstall.Font = $bold

$out = New-Object System.Windows.Forms.TextBox
$out.Multiline = $true; $out.ReadOnly = $true; $out.ScrollBars = "Vertical"; $out.WordWrap = $false
$out.Font = New-Object System.Drawing.Font("Consolas", 9)
$out.Location = New-Object System.Drawing.Point(20, 340); $out.Size = New-Object System.Drawing.Size(680, 250)
$out.BackColor = [System.Drawing.Color]::White
$form.Controls.Add($out)

function Write-Out([string]$text) {
    $out.AppendText(($text -replace "(?<!`r)`n", "`r`n") + "`r`n")
    $out.SelectionStart = $out.TextLength; $out.ScrollToCaret()
    [System.Windows.Forms.Application]::DoEvents()
}
function Save-FromForm {
    $problems = Test-Input $txtDir.Text $txtUrl.Text $txtKey.Text ([int]$numInterval.Value)
    if ($problems) { Write-Out (($problems | ForEach-Object { "FAIL  $_" }) -join "`n"); return $false }
    Save-Settings $txtDir.Text.Trim() $txtUrl.Text $txtKey.Text ([int]$numInterval.Value)
    return $true
}
function Invoke-Busy([scriptblock]$work) {
    $form.Cursor = [System.Windows.Forms.Cursors]::WaitCursor
    foreach ($b in @($btnTest, $btnInstall, $btnRun, $btnOff)) { $b.Enabled = $false }
    try { & $work } catch { Write-Out "ERROR  $($_.Exception.Message)" }
    finally {
        foreach ($b in @($btnTest, $btnInstall, $btnRun, $btnOff)) { $b.Enabled = $true }
        $form.Cursor = [System.Windows.Forms.Cursors]::Default
        $lblTask.Text = Get-TaskText
    }
}

$chkShow.Add_CheckedChanged({ $txtKey.UseSystemPasswordChar = -not $chkShow.Checked })

$btnBrowse.Add_Click({
    $d = New-Object System.Windows.Forms.FolderBrowserDialog
    $d.Description = "Choose the folder that contains the machine .MDF files"
    if ($txtDir.Text -and (Test-Path $txtDir.Text)) { $d.SelectedPath = $txtDir.Text }
    if ($d.ShowDialog() -eq "OK") { $txtDir.Text = $d.SelectedPath }
})

$btnTest.Add_Click({ Invoke-Busy {
    $out.Clear(); Write-Out "Testing settings..."
    if (Save-FromForm) { $r = Invoke-Sync @("--check"); Write-Out $r.Text }
}})

$btnInstall.Add_Click({ Invoke-Busy {
    $out.Clear(); Write-Out "Saving and testing settings..."
    if (-not (Save-FromForm)) { return }
    $r = Invoke-Sync @("--check"); Write-Out $r.Text
    if ($r.Code -ne 0) { Write-Out "`nNot switched on. Fix the problems above and try again."; return }
    Install-SyncTask ([int]$numInterval.Value)
    Write-Out "`nAutomatic sync is ON, every $([int]$numInterval.Value) minutes while you are logged in."
    Write-Out "First run starts in about 2 minutes. You can close this window."
}})

$btnRun.Add_Click({ Invoke-Busy {
    $out.Clear(); Write-Out "Syncing now, please wait..."
    if (Save-FromForm) { $r = Invoke-Sync @(); Write-Out $r.Text }
}})

$btnOff.Add_Click({ Invoke-Busy {
    Remove-SyncTask; $out.Clear(); Write-Out "Automatic sync is OFF. Settings and uploaded data are kept."
}})

$btnLog.Add_Click({
    $log = Join-Path $root "logs\sync_passport.log"
    if (Test-Path $log) { Start-Process notepad.exe $log } else { Write-Out "No log yet. It appears after the first sync." }
})

if (-not (Test-Path $exe)) { Write-Out "Program not found: $exe`nCopy the whole EmbroiSync folder, including the 'app' folder." }
elseif (-not $s.data_dir) { Write-Out "Choose the data folder, fill in Supabase, press 'Test settings', then 'Save and switch ON'." }
else { Write-Out "Current settings loaded. Press 'Test settings' to check them." }

if ($BuildOnly) { Write-Host "window built OK: $($form.Controls.Count) controls"; $form.Dispose(); exit 0 }
[void]$form.ShowDialog()
