$td = Get-Content "user_service_taskdef.json" | ConvertFrom-Json

# Add new env vars
$env_vars = [System.Collections.ArrayList]@($td.containerDefinitions[0].environment)
$env_vars.Add([pscustomobject]@{name="CARDS_API_URL"; value="https://pa-ad2039d606764cfd953ed8909489a2ec.ecs.us-east-2.on.aws"}) | Out-Null
$env_vars.Add([pscustomobject]@{name="CARDS_API_KEY"; value="atlas-dev-key-2026-colina-del-sol"}) | Out-Null
$td.containerDefinitions[0].environment = $env_vars.ToArray()

# Build clean task def - only include non-null fields
$clean = [ordered]@{
    family = $td.family
    containerDefinitions = $td.containerDefinitions
    cpu = $td.cpu
    memory = $td.memory
    networkMode = $td.networkMode
    requiresCompatibilities = $td.requiresCompatibilities
    executionRoleArn = $td.executionRoleArn
}

# Only add optional fields if they exist
if ($td.taskRoleArn) { $clean["taskRoleArn"] = $td.taskRoleArn }
if ($td.runtimePlatform) { $clean["runtimePlatform"] = $td.runtimePlatform }

$utf8NoBom = New-Object System.Text.UTF8Encoding($false)
$json = $clean | ConvertTo-Json -Depth 10
[System.IO.File]::WriteAllText(
    (Join-Path $PWD "user_service_taskdef_updated.json"),
    $json,
    $utf8NoBom
)

Write-Host "Done. Environment variables count: $($env_vars.Count)"
