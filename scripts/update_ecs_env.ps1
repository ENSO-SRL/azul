# =====================================================================
# Script: update_ecs_env.ps1
# Objetivo: Registrar nueva revision de task definition con credenciales
#           de produccion para Colina del Sol y actualizar el servicio ECS.
# =====================================================================

$CLUSTER   = "default"
$SERVICE   = "pago-azul-5325"
$TASK_FAM  = "default-pago-azul-5325"

Write-Host "==> Obteniendo task definition actual..." -ForegroundColor Cyan
$rawJson = aws ecs describe-task-definition --task-definition "${TASK_FAM}:11" --output json
$current = $rawJson | ConvertFrom-Json
$td = $current.taskDefinition

# Tomar el bloque de environment como lista mutable
$envList = [System.Collections.Generic.List[object]]::new()
foreach ($e in $td.containerDefinitions[0].environment) {
    $envList.Add([PSCustomObject]@{ name = $e.name; value = $e.value })
}

# Aplicar los 8 cambios existentes
foreach ($item in $envList) {
    switch ($item.name) {
        "AZUL_AUTH1"         { $item.value = "COLINADELSOL" }
        "AZUL_AUTH1_3DS"     { $item.value = "COLINADELSOL" }
        "AZUL_AUTH1_SPLITIT" { $item.value = "COLINADELSOL" }
        "AZUL_AUTH2"         { $item.value = '#gPmt&$2RUjX' }
        "AZUL_AUTH2_3DS"     { $item.value = '#gPmt&$2RUjX' }
        "AZUL_AUTH2_SPLITIT" { $item.value = '#gPmt&$2RUjX' }
        "AZUL_LOCAL_MODE"    { $item.value = "0" }
        "AZUL_MERCHANT_ID"   { $item.value = "39644300001" }
    }
}

# Agregar las 2 variables nuevas
$envList.Add([PSCustomObject]@{ name = "AZUL_ENV"; value = "production" })
$envList.Add([PSCustomObject]@{ name = "API_KEY";  value = "atlas-dev-key-2026-colina-del-sol" })

Write-Host "==> Variables actualizadas ($($envList.Count) total). Generando JSON..." -ForegroundColor Cyan

# Construir container definition manualmente
$origContainer = $td.containerDefinitions[0]

$containerDef = [ordered]@{
    name              = $origContainer.name
    image             = $origContainer.image
    cpu               = $origContainer.cpu
    memory            = $origContainer.memory
    memoryReservation = $origContainer.memoryReservation
    portMappings      = @(
        [ordered]@{
            containerPort = 8000
            hostPort      = 8000
            protocol      = "tcp"
        }
    )
    essential    = $true
    command      = @()
    environment  = @($envList)
    mountPoints  = @()
    volumesFrom  = @()
    secrets      = @()
    logConfiguration = [ordered]@{
        logDriver = "awslogs"
        options   = [ordered]@{
            "awslogs-group"         = "/aws/ecs/default/pago-azul-5325-e917"
            "awslogs-region"        = "us-east-2"
            "awslogs-stream-prefix" = "ecs"
        }
    }
}

$newTd = [ordered]@{
    family                  = $td.family
    executionRoleArn        = $td.executionRoleArn
    networkMode             = $td.networkMode
    requiresCompatibilities = @("FARGATE")
    cpu                     = $td.cpu
    memory                  = $td.memory
    containerDefinitions    = @($containerDef)
    volumes                 = @()
    placementConstraints    = @()
    runtimePlatform         = [ordered]@{
        cpuArchitecture      = "X86_64"
        operatingSystemFamily = "LINUX"
    }
}

# Serializar a JSON con profundidad suficiente
$newTdJson = $newTd | ConvertTo-Json -Depth 15
$newTdJson | Out-File -FilePath "new_task_def.json" -Encoding utf8

Write-Host "==> Registrando nueva revision en ECS..." -ForegroundColor Cyan
$regOutput = aws ecs register-task-definition --cli-input-json file://new_task_def.json --output json
$registered = $regOutput | ConvertFrom-Json

if ($LASTEXITCODE -ne 0) {
    Write-Host "ERROR al registrar. Salida:" -ForegroundColor Red
    Write-Host $regOutput
    exit 1
}

$newArn = $registered.taskDefinition.taskDefinitionArn
$newRev = $registered.taskDefinition.revision
Write-Host "==> Nueva revision registrada: revision $newRev" -ForegroundColor Green

Write-Host "==> Actualizando servicio ECS '$SERVICE'..." -ForegroundColor Cyan
$updOutput = aws ecs update-service `
    --cluster $CLUSTER `
    --service $SERVICE `
    --task-definition $newArn `
    --force-new-deployment `
    --output json
$update = $updOutput | ConvertFrom-Json

if ($LASTEXITCODE -ne 0) {
    Write-Host "ERROR al actualizar el servicio:" -ForegroundColor Red
    Write-Host $updOutput
    exit 1
}

$deployStatus = $update.service.deployments[0].status
Write-Host ""
Write-Host "==========================================" -ForegroundColor Green
Write-Host "  EXITO: Servicio actualizado" -ForegroundColor Green
Write-Host "  Task Definition revision: $newRev" -ForegroundColor Green
Write-Host "  Deployment status: $deployStatus" -ForegroundColor Green
Write-Host "==========================================" -ForegroundColor Green
Write-Host ""
Write-Host "El contenedor tardara ~1-2 min en arrancar." -ForegroundColor Yellow
