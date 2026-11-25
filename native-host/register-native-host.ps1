$ErrorActionPreference = "Stop"
$manifestPath = Join-Path $PSScriptRoot "com.gemini_web2api.host.json"
$hostBatPath = Join-Path $PSScriptRoot "host.bat"
$hostName = "com.gemini_web2api.host"

$manifest = [ordered]@{
  name = $hostName
  description = "Control local gemini-web2api server from Gemini Web2API Controller extension"
  path = $hostBatPath
  type = "stdio"
  allowed_origins = @(
    "chrome-extension://aelicddajdhceggpmkpghhpkfkeffgln/"
  )
}
$json = $manifest | ConvertTo-Json -Depth 4
[System.IO.File]::WriteAllText($manifestPath, $json + "`n", [System.Text.UTF8Encoding]::new($false))
Write-Host "Generated: $manifestPath -> $hostBatPath"

foreach ($root in @(
  "HKCU:\Software\Google\Chrome\NativeMessagingHosts\$hostName",
  "HKCU:\Software\Microsoft\Edge\NativeMessagingHosts\$hostName"
)) {
  if (-not (Test-Path $root)) {
    New-Item -Path $root -Force | Out-Null
  }
  Set-ItemProperty -Path $root -Name "(Default)" -Value $manifestPath
  Write-Host "Registered: $root -> $manifestPath"
}
