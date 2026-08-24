$CLUSTER  = "default"
$SERVICE  = "pago-azul-5325"
$TASK_FAM = "default-pago-azul-5325"

$REDIS_HOST     = "redis-19755.c309.us-east-2-1.ec2.redns.redis-cloud.com"
$REDIS_PORT     = "19755"
$REDIS_PASSWORD = "08xa6i9zffmsaXLo0fcUlNXxbCl3NIDa"
$REDIS_DB       = "0"
$REDIS_SSL      = "false"

$newVars = @(
    [PSCustomObject]@{ name = "REDIS_HOST";     value = $REDIS_HOST     },
    [PSCustomObject]@{ name = "REDIS_PORT";     value = $REDIS_PORT     },
    [PSCustomObject]@{ name = "REDIS_PASSWORD"; value = $REDIS_PASSWORD },
    [PSCustomObject]@{ name = "REDIS_DB";       value = $REDIS_DB       },
    [PSCustomObject]@{ name = "REDIS_SSL";      value = $REDIS_SSL      }
)

Write-Host ""
Write-Host "==> Obteniendo task definition actual..." -ForegroundColor Cyan
$rawJson = aws ecs describe-task-definition --task-definition $TASK_FAM --output json
$current = $rawJson | ConvertFrom-Json
$td      = $current.taskDefinition
Write-Host "    Revision actual: $($td.revision)" -ForegroundColor Gray

$envList = [System.Collections.Generic.List[object]]::new()
$existingNames = @{}

foreach ($e in $td.containerDefinitions[0].environment) {
    $envList.Add([PSCustomObject]@{ name = $e.name; value = $e.value })
    $existingNames[$e.name] = $true
}

$added = [System.Collections.Generic.List[string]]::new()
foreach ($v in $newVars) {
    if ($existingNames.ContainsKey($v.name)) {
        Write-Host "    Omitiendo $($v.name) - ya existe." -ForegroundColor Gray
    } else {
        $envList.Add([PSCustomObject]@{ name = $v.name; value = $v.value })
        $added.Add($v.name)
        Write-Host "    + Agregando $($v.name)" -ForegroundColor Green
    }
}

if ($added.Count -eq 0) {
    Write-Host ""
    Write-Host "  Todas las variables de Redis ya existen. Nada que hacer." -ForegroundColor Yellow
    exit 0
}

Write-Host ""
Write-Host "==> Construyendo nuevo task definition ($($envList.Count) variables)..." -ForegroundColor Cyan

$origContainer = $td.containerDefinitions[0]

$containerDef = [ordered]@{
    name              = $origContainer.name
    image             = $origContainer.image
    cpu               = $origContainer.cpu
    memory            = $origContainer.memory
    memoryReservation = $origContainer.memoryReservation
    portMappings      = @(
        [ordered]@{ containerPort = 8000; hostPort = 8000; protocol = "tcp" }
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

$outFile = "$PSScriptRoot\redis_taskdef.json"
$newTd | ConvertTo-Json -Depth 15 | Out-File -FilePath $outFile -Encoding utf8
Write-Host "    JSON guardado en: $outFile" -ForegroundColor Gray

Write-Host "==> Registrando nueva revision en ECS..." -ForegroundColor Cyan
$regOutput  = aws ecs register-task-definition --cli-input-json "file://$outFile" --output json
$registered = $regOutput | ConvertFrom-Json

if ($LASTEXITCODE -ne 0) {
    Write-Host "ERROR al registrar:" -ForegroundColor Red
    Write-Host $regOutput
    exit 1
}

$newRev = $registered.taskDefinition.revision
Write-Host "    Nueva revision: $newRev" -ForegroundColor Green

Write-Host "==> Actualizando servicio ECS '$SERVICE'..." -ForegroundColor Cyan
$updOutput = aws ecs update-service `
    --cluster $CLUSTER `
    --service $SERVICE `
    --task-definition "$($TASK_FAM):$newRev" `
    --force-new-deployment `
    --output json
$update = $updOutput | ConvertFrom-Json

if ($LASTEXITCODE -ne 0) {
    Write-Host "ERROR al actualizar servicio:" -ForegroundColor Red
    Write-Host $updOutput
    exit 1
}

$deployStatus = $update.service.deployments[0].status

Write-Host ""
Write-Host "==========================================" -ForegroundColor Green
Write-Host "  EXITO" -ForegroundColor Green
Write-Host "  Variables agregadas: $($added -join ', ')" -ForegroundColor Green
Write-Host "  Task Definition revision: $newRev" -ForegroundColor Green
Write-Host "  Deployment status: $deployStatus" -ForegroundColor Green
Write-Host "==========================================" -ForegroundColor Green
Write-Host ""
Write-Host "Espera 1-2 min y busca en los logs:" -ForegroundColor Yellow
Write-Host "  [redis] Conexion establecida. host=redis-19755..." -ForegroundColor Gray
