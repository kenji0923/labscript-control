param([Parameter(Mandatory=$true)][string]$Python,
      [Parameter(Mandatory=$true)][string]$Root)
$ErrorActionPreference = 'Stop'
$account = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
$principal = New-ScheduledTaskPrincipal -UserId $account -LogonType Interactive -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
foreach ($app in @('blacs','runmanager')) {
    $taskName = if ($app -eq 'blacs') {'LabscriptAgent-BLACS'} else {'LabscriptAgent-Runmanager'}
    $action = New-ScheduledTaskAction -Execute $Python -Argument "-m $app" -WorkingDirectory $Root
    $existing = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
    if ($existing -and $existing.Description -ne 'Labscript agent control: interactive application startup') {
        throw "Task $taskName already exists and is not managed by this installer"
    }
    Register-ScheduledTask -TaskName $taskName -Description 'Labscript agent control: interactive application startup' -Action $action -Principal $principal -Settings $settings -Force | Out-Null
    Write-Output "Registered $taskName; not started"
}
