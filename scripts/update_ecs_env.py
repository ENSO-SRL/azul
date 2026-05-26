#!/usr/bin/env python3
"""
update_ecs_env.py
Actualiza variables de entorno del servicio ECS pago-azul-5325
con credenciales de produccion para Colina del Sol.
"""
import json
import subprocess
import sys

CLUSTER   = "default"
SERVICE   = "pago-azul-5325"
TASK_DEF  = "default-pago-azul-5325:11"

# ── Cambios a variables existentes ──────────────────────────────────
CHANGES = {
    "AZUL_AUTH1":         "COLINADELSOL",
    "AZUL_AUTH1_3DS":     "COLINADELSOL",
    "AZUL_AUTH1_SPLITIT": "COLINADELSOL",
    "AZUL_AUTH2":         "#gPmt&$2RUjX",
    "AZUL_AUTH2_3DS":     "#gPmt&$2RUjX",
    "AZUL_AUTH2_SPLITIT": "#gPmt&$2RUjX",
    "AZUL_LOCAL_MODE":    "0",
    "AZUL_MERCHANT_ID":   "39644300001",
}

# ── Variables nuevas ─────────────────────────────────────────────────
NEW_VARS = [
    {"name": "AZUL_ENV", "value": "production"},
    {"name": "API_KEY",  "value": "atlas-dev-key-2026-colina-del-sol"},
]

def run(cmd):
    result = subprocess.run(cmd, capture_output=True, text=True, shell=True)
    if result.returncode != 0:
        print(f"ERROR ejecutando: {cmd}")
        print(result.stderr)
        sys.exit(1)
    return result.stdout

print("==> Obteniendo task definition actual...")
raw = run(f"aws ecs describe-task-definition --task-definition {TASK_DEF} --output json")
td  = json.loads(raw)["taskDefinition"]

# Aplicar cambios al environment
env = td["containerDefinitions"][0]["environment"]
for var in env:
    if var["name"] in CHANGES:
        print(f"    {var['name']}: {var['value']!r} -> {CHANGES[var['name']]!r}")
        var["value"] = CHANGES[var["name"]]

# Agregar nuevas variables
for nv in NEW_VARS:
    # Evitar duplicados por si ya existen
    if not any(e["name"] == nv["name"] for e in env):
        env.append(nv)
        print(f"    + {nv['name']} = {nv['value']!r}  [NUEVA]")

print(f"\n==> Total variables de entorno: {len(env)}")

# Construir payload para register-task-definition
payload = {
    "family":                  td["family"],
    "executionRoleArn":        td["executionRoleArn"],
    "networkMode":             td["networkMode"],
    "requiresCompatibilities": td["requiresCompatibilities"],
    "cpu":                     td["cpu"],
    "memory":                  td["memory"],
    "runtimePlatform":         td["runtimePlatform"],
    "volumes":                 [],
    "placementConstraints":    [],
    "containerDefinitions": [
        {
            "name":              td["containerDefinitions"][0]["name"],
            "image":             td["containerDefinitions"][0]["image"],
            "cpu":               td["containerDefinitions"][0]["cpu"],
            "memory":            td["containerDefinitions"][0]["memory"],
            "memoryReservation": td["containerDefinitions"][0]["memoryReservation"],
            "essential":         True,
            "portMappings": [{"containerPort": 8000, "hostPort": 8000, "protocol": "tcp"}],
            "command":      [],
            "environment":  env,
            "mountPoints":  [],
            "volumesFrom":  [],
            "secrets":      [],
            "logConfiguration": {
                "logDriver": "awslogs",
                "options": {
                    "awslogs-group":         "/aws/ecs/default/pago-azul-5325-e917",
                    "awslogs-region":        "us-east-2",
                    "awslogs-stream-prefix": "ecs"
                }
            }
        }
    ]
}

with open("new_task_def.json", "w", encoding="utf-8") as f:
    json.dump(payload, f, indent=2, ensure_ascii=False)
print("==> JSON guardado en new_task_def.json")

print("==> Registrando nueva revision en ECS...")
reg_out = run("aws ecs register-task-definition --cli-input-json file://new_task_def.json --output json")
reg = json.loads(reg_out)
new_arn = reg["taskDefinition"]["taskDefinitionArn"]
new_rev = reg["taskDefinition"]["revision"]
print(f"==> Nueva revision registrada: {new_arn}  (revision {new_rev})")

print(f"==> Actualizando servicio ECS '{SERVICE}' en cluster '{CLUSTER}'...")
upd_out = run(
    f"aws ecs update-service "
    f"--cluster {CLUSTER} "
    f"--service {SERVICE} "
    f"--task-definition {new_arn} "
    f"--force-new-deployment "
    f"--output json"
)
upd = json.loads(upd_out)
deploy_status = upd["service"]["deployments"][0]["status"]

print()
print("=" * 50)
print("  EXITO: Servicio actualizado correctamente")
print(f"  Task Definition revision: {new_rev}")
print(f"  ARN: {new_arn}")
print(f"  Deployment status: {deploy_status}")
print("=" * 50)
print()
print("El nuevo contenedor tardara ~1-2 minutos en arrancar.")
print("Monitorea con:")
print("  aws ecs describe-services --cluster default --services pago-azul-5325 "
      "--query \"services[0].deployments\" --output table")
