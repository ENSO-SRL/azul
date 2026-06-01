"""
Fix quirúrgico: cambia AZUL_MERCHANT_ID de "39644300001" → "3964430000"
en la task definition viva de ECS y registra nueva revisión.
"""
import json, subprocess, sys

CORRECT_MERCHANT_ID = "39644300001"
REGION = "us-east-2"
CLUSTER = "default"
SERVICE = "pago-azul-5325"
OUTPUT_FILE = "scripts/_taskdef_patched.json"

# ── Cargar task definition viva ──────────────────────────────────────────────
with open("scripts/_taskdef_live.json", encoding="utf-8-sig") as f:
    raw = json.load(f)

td = raw["taskDefinition"]
container = td["containerDefinitions"][0]
env = container["environment"]

# ── Auditoría antes del parche ────────────────────────────────────────────────
key_vars = {e["name"]: e["value"] for e in env}
print("=" * 60)
print("AUDITORÍA DE VARIABLES CRÍTICAS (estado actual en ECS)")
print("=" * 60)

checks = [
    ("AZUL_ENV",          "production",          True),
    ("AZUL_LOCAL_MODE",   "0",                   True),
    ("AZUL_MERCHANT_ID",  CORRECT_MERCHANT_ID,   True),
    ("AZUL_AUTH1",        "COLINADELSOL",         True),
    ("AZUL_AUTH1_3DS",    "COLINADELSOL",         True),
    ("AZUL_AUTH1_SPLITIT","COLINADELSOL",         True),
    ("AZUL_AUTH2",        "#gPmt&$2RUjX",         False),   # no mostrar valor real
    ("AZUL_AUTH2_3DS",    "#gPmt&$2RUjX",         False),
    ("AZUL_AUTH2_SPLITIT","#gPmt&$2RUjX",         False),
    ("AZUL_CERT_PEM",     None,                   False),   # solo verificar presencia
    ("AZUL_KEY_PEM",      None,                   False),
    ("DATABASE_URL",      None,                   False),
    ("APP_BASE_URL",      None,                   False),
    ("API_KEY",           None,                   False),
]

all_ok = True
needs_patch = False

for name, expected, show in checks:
    actual = key_vars.get(name, "[MISSING]")
    present = name in key_vars

    if expected is None:
        # Solo verificar presencia
        status = "[OK]" if present else "[FAIL]"
        display = f"{'(presente)' if present else 'FALTA'}"
        if not present:
            all_ok = False
    else:
        match = (actual == expected)
        status = "[OK]" if match else "[FAIL]"
        display = actual if show else ("(ok)" if match else f"TIENE: {actual!r} → ESPERADO: {expected!r}")
        if not match:
            all_ok = False
            if name == "AZUL_MERCHANT_ID":
                needs_patch = True

    print(f"  {status}  {name:30s}  {display}")

print()

# ── Aplicar parche ────────────────────────────────────────────────────────────
if needs_patch:
    print(f"[FIX] Parcheando AZUL_MERCHANT_ID: {key_vars.get('AZUL_MERCHANT_ID')} -> {CORRECT_MERCHANT_ID}")
    for e in env:
        if e["name"] == "AZUL_MERCHANT_ID":
            e["value"] = CORRECT_MERCHANT_ID
            break
    container["environment"] = env
else:
    print("[INFO] AZUL_MERCHANT_ID ya esta correcto. No se necesita parche.")
    sys.exit(0)

# ── Construir payload para register-task-definition ───────────────────────────
new_td = {
    "family":                  td["family"],
    "executionRoleArn":        td["executionRoleArn"],
    "networkMode":             td["networkMode"],
    "containerDefinitions":    [container],
    "requiresCompatibilities": td.get("requiresCompatibilities", ["FARGATE"]),
    "cpu":                     td["cpu"],
    "memory":                  td["memory"],
}
if "taskRoleArn" in td:
    new_td["taskRoleArn"] = td["taskRoleArn"]
if "runtimePlatform" in td:
    new_td["runtimePlatform"] = td["runtimePlatform"]
if "volumes" in td:
    new_td["volumes"] = td["volumes"]

with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
    json.dump(new_td, f, indent=2, ensure_ascii=False)
print(f"[OK] Payload escrito -> {OUTPUT_FILE}")

# ── Registrar nueva revisión ──────────────────────────────────────────────────
print("\n[>>] Registrando nueva revision en ECS...")
result = subprocess.run(
    ["aws", "ecs", "register-task-definition",
     "--cli-input-json", f"file://{OUTPUT_FILE}",
     "--region", REGION,
     "--query", "taskDefinition.taskDefinitionArn",
     "--output", "text"],
    capture_output=True, text=True
)
if result.returncode != 0:
    print("❌ ERROR al registrar:", result.stderr)
    sys.exit(1)

new_arn = result.stdout.strip()
print(f"[OK] Nueva revision registrada: {new_arn}")

# ── Actualizar el servicio ECS ────────────────────────────────────────────────
print(f"\n[>>] Actualizando servicio '{SERVICE}'...")
svc = subprocess.run(
    ["aws", "ecs", "update-service",
     "--cluster", CLUSTER,
     "--service", SERVICE,
     "--task-definition", new_arn,
     "--force-new-deployment",
     "--region", REGION,
     "--query", "service.deployments[0].{status:status,taskDef:taskDefinition}",
     "--output", "json"],
    capture_output=True, text=True
)
if svc.returncode != 0:
    print("❌ ERROR al actualizar servicio:", svc.stderr)
    sys.exit(1)

deploy = json.loads(svc.stdout)
print("[OK] Deployment iniciado:")
print(f"   Status:   {deploy.get('status')}")
print(f"   Task Def: {deploy.get('taskDef')}")

print()
print("=" * 60)
print("  [OK] DEPLOYMENT COMPLETADO")
print(f"  AZUL_MERCHANT_ID = {CORRECT_MERCHANT_ID}")
print("  El contenedor tardará ~1-2 min en reiniciarse.")
print("=" * 60)
