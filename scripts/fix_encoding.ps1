$content = Get-Content "user_service_taskdef_updated.json" -Raw
$utf8NoBom = New-Object System.Text.UTF8Encoding($false)
[System.IO.File]::WriteAllText(
    (Resolve-Path "user_service_taskdef_updated.json").Path,
    $content,
    $utf8NoBom
)
Write-Host "Re-encoded to UTF-8 without BOM"
