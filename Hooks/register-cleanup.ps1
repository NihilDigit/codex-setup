[CmdletBinding(SupportsShouldProcess)]
param(
    [string]$Python = (Get-Command python -ErrorAction Stop).Source,
    [string]$StateDirectory = $(if ($env:CODEX_SETUP_STATE) { $env:CODEX_SETUP_STATE } else { Join-Path $env:USERPROFILE 'AppData\Local\CodexSetup' })
)

$cleanupScript = Join-Path $PSScriptRoot 'cleanup.py'
if (-not (Test-Path -LiteralPath $cleanupScript)) { throw 'cleanup.py is missing' }
$statePath = [IO.Path]::GetFullPath($StateDirectory)
$pythonPath = [IO.Path]::GetFullPath($Python)
$cleanupPath = [IO.Path]::GetFullPath($cleanupScript)
$launcherPath = Join-Path $PSScriptRoot 'scheduled-cleanup.ps1'
$launcherText = '$env:CODEX_SETUP_STATE = ''' + $statePath.Replace("'", "''") + "'`n" +
    '& ''' + $pythonPath.Replace("'", "''") + ''' ''' + $cleanupPath.Replace("'", "''") + "'`nexit `$LASTEXITCODE`n"

if ($PSCmdlet.ShouldProcess('CodexSetup-Cleanup', 'Register daily cleanup at 03:00')) {
    $existingTask = Get-ScheduledTask -TaskName 'CodexSetup-Cleanup' -ErrorAction SilentlyContinue
    if ($existingTask -and -not ($existingTask.Actions.Arguments -match [regex]::Escape($launcherPath))) {
        throw 'An unrelated task already uses the name CodexSetup-Cleanup'
    }
    [IO.File]::WriteAllText($launcherPath, $launcherText, [Text.UTF8Encoding]::new($false))
    $pwshPath = (Get-Command pwsh -ErrorAction Stop).Source
    $action = New-ScheduledTaskAction -Execute $pwshPath -Argument ('-NoProfile -NonInteractive -WindowStyle Hidden -File "' + $launcherPath + '"')
    $trigger = New-ScheduledTaskTrigger -Daily -At '03:00'
    $settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -MultipleInstances IgnoreNew
    $principal = New-ScheduledTaskPrincipal -UserId ([Security.Principal.WindowsIdentity]::GetCurrent().Name) -LogonType Interactive -RunLevel Limited
    Register-ScheduledTask -TaskName 'CodexSetup-Cleanup' -Action $action -Trigger $trigger -Settings $settings -Principal $principal -Force | Out-Null
}
