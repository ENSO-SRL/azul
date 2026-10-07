# =====================================================================
# Script: fix_atlas_api_url.ps1
# Objetivo: Corregir ATLAS_API_BASE_URL en ECS — el hostname interno
#           pa-ad2039d606764cfd953ed8909489a2ec.ecs.us-east-2.on.aws
#           ya no resuelve. Se reemplaza por la URL publica del user service.
#
# Uso:
#   .\fix_atlas_api_url.ps1
#
# Si el user service tiene un dominio diferente, edita $NEW_URL abajo.
# =====================================================================

$CLUSTER   = "default"
$SERVICE   = "pago-azul-5325"
$TASK_FAM  = "default-pago-azul-5325"

# URL correcta del user service (el endpoint /invoices/service/payment_email vive aqui)
# Cambia esto si el user service usa otro dominio.
$NEW_URL = "https://api.iamatlas.do"

Write-Host "==> Obteniendo task definition actual del servicio '$SERVICE'..." -ForegroundColor Cyan
$rawJson  = aws ecs describe-task-definition --task-definition $TASK_FAM --output json
$current  = $rawJson | ConvertFrom-Json
$td       = $current.taskDefinition

Write-Host "    Revision actual: $($td.revision)" -ForegroundColor Gray

# Clonar la lista de variables de entorno como lista mutable
$envList = [System.Collections.Generic.List[object]]::new()
$changed = $false

foreach ($e in $td.containerDefinitions[0].environment) {
    if ($e.name -eq "ATLAS_API_BASE_URL") {
        Write-Host "    Actualizando ATLAS_API_BASE_URL:" -ForegroundColor Yellow
        Write-Host "      Antes: $($e.value)" -ForegroundColor Red
        Write-Host "      Ahora: $NEW_URL"    -ForegroundColor Green
        $envList.Add([PSCustomObject]@{ name = $e.name; value = $NEW_URL })
        $changed = $true
    } else {
        $envList.Add([PSCustomObject]@{ name = $e.name; value = $e.value })
    }
}

# Si no existia, la agrega
if (-not $changed) {
    Write-Host "    ATLAS_API_BASE_URL no encontrada — se agrega como variable nueva." -ForegroundColor Yellow
    $envList.Add([PSCustomObject]@{ name = "ATLAS_API_BASE_URL"; value = $NEW_URL })
}

# Construir container definition con la lista actualizada
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
    essential         = $true
    command           = @()
    environment       = @($envList)
    mountPoints       = @()
    volumesFrom       = @()
    secrets           = @()
    logConfiguration  = [ordered]@{
        logDriver = "awslogs"
        options   = [ordered]@{
            "awslogs-group"         = "/aws/ecs/default/pago-azul-5325-e917"
            "awslogs-region"        = "us-east-2"
            "awslogs-stream-prefix" = "ecs"
        }
    }
    systemControls    = @()
}

$newTd = [ordered]@{
    family                  = $td.family
    executionRoleArn        = $td.executionRoleArn
    taskRoleArn             = $td.taskRoleArn
    networkMode             = $td.networkMode
    requiresCompatibilities = @("FARGATE")
    cpu                     = $td.cpu
    memory                  = $td.memory
    containerDefinitions    = @($containerDef)
    volumes                 = @()
    placementConstraints    = @()
    runtimePlatform         = [ordered]@{
        cpuArchitecture       = "X86_64"
        operatingSystemFamily = "LINUX"
    }
}

$outFile = "$PSScriptRoot\fix_atlas_url_taskdef.json"
$newTd | ConvertTo-Json -Depth 15 | Out-File -FilePath $outFile -Encoding utf8
Write-Host "==> JSON generado: $outFile" -ForegroundColor Cyan

Write-Host "==> Registrando nueva revision en ECS..." -ForegroundColor Cyan
$regOutput = aws ecs register-task-definition --cli-input-json "file://$outFile" --output json
$registered = $regOutput | ConvertFrom-Json

if ($LASTEXITCODE -ne 0) {
    Write-Host "ERROR al registrar la task definition:" -ForegroundColor Red
    Write-Host $regOutput
    exit 1
}

$newRev = $registered.taskDefinition.revision
Write-Host "==> Nueva revision registrada: $newRev" -ForegroundColor Green

Write-Host "==> Actualizando servicio ECS '$SERVICE'..." -ForegroundColor Cyan
$updOutput = aws ecs update-service `
    --cluster $CLUSTER `
    --service $SERVICE `
    --task-definition "$($TASK_FAM):$newRev" `
    --force-new-deployment `
    --output json
$update = $updOutput | ConvertFrom-Json

if ($LASTEXITCODE -ne 0) {
    Write-Host "ERROR al actualizar el servicio ECS:" -ForegroundColor Red
    Write-Host $updOutput
    exit 1
}

$deployStatus = $update.service.deployments[0].status
Write-Host ""
Write-Host "==========================================" -ForegroundColor Green
Write-Host "  EXITO: Servicio ECS actualizado"         -ForegroundColor Green
Write-Host "  Task Definition revision: $newRev"       -ForegroundColor Green
Write-Host "  Deployment status: $deployStatus"        -ForegroundColor Green
Write-Host "==========================================" -ForegroundColor Green
Write-Host ""
Write-Host "El contenedor tardara ~1-2 min en arrancar con la URL corregida." -ForegroundColor Yellow
Write-Host "Confirma en los logs que ya no aparece '[Errno -5] No address associated with hostname'." -ForegroundColor Yellow
