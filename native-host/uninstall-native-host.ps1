$ErrorActionPreference = "SilentlyContinue"
$hostName = "com.gemini_web2api.host"

foreach ($root in @(
  "HKCU:\Software\Google\Chrome\NativeMessagingHosts\$hostName",
  "HKCU:\Software\Microsoft\Edge\NativeMessagingHosts\$hostName"
)) {
  if (Test-Path $root) {
    Remove-Item -Path $root -Recurse -Force
    Write-Host "Removed: $root"
  }
}
