[CmdletBinding()]
param([string]$Name = 'PaperAgent（源码）')

$ErrorActionPreference = 'Stop'
$Root = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$Shell = New-Object -ComObject WScript.Shell
$ShortcutPath = Join-Path ([Environment]::GetFolderPath('Desktop')) "$Name.lnk"
$Shortcut = $Shell.CreateShortcut($ShortcutPath)
$Shortcut.TargetPath = (Get-Command powershell.exe).Source
$Shortcut.Arguments = "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$Root\scripts\start-local.ps1`" Start"
$Shortcut.WorkingDirectory = $Root
$Shortcut.Description = '启动本地 PaperAgent；无需手动维护前后端终端。'
$Shortcut.Save()
Write-Host "已创建桌面快捷方式：$ShortcutPath"
