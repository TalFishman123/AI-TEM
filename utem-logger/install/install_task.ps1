# Registers the UTEM state logger as a scheduled task: starts at log-on of the current user, runs with
# no time limit, restarts within a minute if it stops, never starts a second copy.
# Run once in PowerShell from this folder:   powershell -ExecutionPolicy Bypass -File install_task.ps1
# Remove with:                               Unregister-ScheduledTask -TaskName "UTEM state logger"
# If the camera has to run in another environment (PyJEM only available for an older Python), pass
# -CameraPython <that env's pythonw.exe>: a second task "UTEM camera server" is registered the same way.
param(
    [string]$Python = "$env:USERPROFILE\.conda\envs\utem-logger\pythonw.exe",   # the logger's environment
    [string]$CameraPython = "",
    [string]$TaskName = "UTEM state logger"
)
$root = Split-Path -Parent $PSScriptRoot                                     # utem-logger/
$config = Join-Path $root "config.json"
if (-not (Test-Path $Python)) { throw "Python not found: $Python (pass -Python <path to pythonw.exe>)" }
if (-not (Test-Path $config)) { throw "Make $config first (copy config.example.json and set log_root)" }

$action = New-ScheduledTaskAction -Execute $Python -Argument "-m utem_logger --config `"$config`"" -WorkingDirectory $root
$trigger = New-ScheduledTaskTrigger -AtLogOn -User "$env:USERDOMAIN\$env:USERNAME"
$settings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 9999 `
    -RestartInterval (New-TimeSpan -Minutes 1) -MultipleInstances IgnoreNew -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries -StartWhenAvailable
$principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType Interactive -RunLevel Limited
Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings -Principal $principal -Force | Out-Null
Start-ScheduledTask -TaskName $TaskName
Write-Host "Registered and started '$TaskName'. Its own log: $env:LOCALAPPDATA\utem-logger\logger.log"

if ($CameraPython -ne "") {
    if (-not (Test-Path $CameraPython)) { throw "Camera Python not found: $CameraPython" }
    $camName = "UTEM camera server"
    $camAction = New-ScheduledTaskAction -Execute $CameraPython -Argument "-m utem_logger --camera-server --config `"$config`"" -WorkingDirectory $root
    Register-ScheduledTask -TaskName $camName -Action $camAction -Trigger $trigger -Settings $settings -Principal $principal -Force | Out-Null
    Start-ScheduledTask -TaskName $camName
    Write-Host "Registered and started '$camName'."
}
