#!/usr/bin/env python3
"""
setup_prod_secrets.py — v3
"""
import json
import subprocess
import sys
import tempfile
import os

REGION  = "us-east-2"
ACCOUNT = "293926505005"
PROD_CREDS_SECRET = "iamatlas/azul/prod/api-credentials"
PROD_SECRET_ARNS = [
    f"arn:aws:secretsmanager:{REGION}:{ACCOUNT}:secret:iamatlas/azul/prod/api-credentials*",
    f"arn:aws:secretsmanager:{REGION}:{ACCOUNT}:secret:iamatlas/azul/prod/cert-pem*",
    f"arn:aws:secretsmanager:{REGION}:{ACCOUNT}:secret:iamatlas/azul/prod/cert-key*",
]
TASK_ROLE_NAME = "ecsTaskRole-pago-azul"
CLUSTER        = "default"
SERVICE        = "pago-azul-5325"
TASK_FAM       = "default-pago-azul-5325"
TASK_DEF_REV   = "13"


def write_tmp_json(data):
    fd, path = tempfile.mkstemp(suffix=".json", prefix="azul_")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    return path


def run(cmd, check=True, verbose=False):
    result = subprocess.run(cmd, capture_output=True, text=True)
    if verbose or result.returncode != 0:
        if result.stdout: print("STDOUT:", result.stdout[:500])
        if result.stderr: print("STDERR:", result.stderr[:500])
    if check and result.returncode != 0:
        print(f"FAILED: {' '.join(cmd)}")
        sys.exit(1)
    return result.stdout.strip(), result.returncode


# ─── PASO 1: Secret api-credentials de prod ──────────────────────────────────
print("==> PASO 1: Secret iamatlas/azul/prod/api-credentials...")
prod_creds = {
    "merchant_id": "39644300001",
    "auth_splitit":  {"auth1": "COLINADELSOL", "auth2": "#gPmt&$2RUjX"},
    "auth_3dsecure": {"auth1": "COLINADELSOL", "auth2": "#gPmt&$2RUjX"},
    "env": "production"
}
creds_file = write_tmp_json(prod_creds)

# Verificar si existe primero
_, rc = run(["aws", "secretsmanager", "describe-secret",
             "--secret-id", PROD_CREDS_SECRET, "--region", REGION, "--output", "json"], check=False)
if rc == 0:
    print("    Ya existe — actualizando...")
    run(["aws", "secretsmanager", "put-secret-value",
         "--secret-id", PROD_CREDS_SECRET,
         "--secret-string", f"file://{creds_file}",
         "--region", REGION, "--output", "json"])
else:
    print("    Creando...")
    run(["aws", "secretsmanager", "create-secret",
         "--name", PROD_CREDS_SECRET,
         "--description", "Credenciales API produccion AZUL - Colina del Sol",
         "--secret-string", f"file://{creds_file}",
         "--region", REGION, "--output", "json"])
os.unlink(creds_file)
print("    OK")


# ─── PASO 2: Task role ────────────────────────────────────────────────────────
print(f"\n==> PASO 2: Task role '{TASK_ROLE_NAME}'...")
trust = {
    "Version": "2012-10-17",
    "Statement": [{"Effect": "Allow",
                   "Principal": {"Service": "ecs-tasks.amazonaws.com"},
                   "Action": "sts:AssumeRole"}]
}
trust_file = write_tmp_json(trust)

# Intentar crear
create_out, rc = run(["aws", "iam", "create-role",
                      "--role-name", TASK_ROLE_NAME,
                      "--assume-role-policy-document", f"file://{trust_file}",
                      "--description", "Task role pago-azul - Secrets Manager prod",
                      "--output", "json"], check=False, verbose=True)
os.unlink(trust_file)

if rc == 0:
    role_arn = json.loads(create_out)["Role"]["Arn"]
    print(f"    Creado: {role_arn}")
else:
    # Ya existe
    get_out, _ = run(["aws", "iam", "get-role", "--role-name", TASK_ROLE_NAME, "--output", "json"])
    role_arn = json.loads(get_out)["Role"]["Arn"]
    print(f"    Ya existe: {role_arn}")

# Politica para leer los secrets
policy = {
    "Version": "2012-10-17",
    "Statement": [{"Effect": "Allow",
                   "Action": ["secretsmanager:GetSecretValue", "secretsmanager:DescribeSecret"],
                   "Resource": PROD_SECRET_ARNS}]
}
policy_file = write_tmp_json(policy)
run(["aws", "iam", "put-role-policy",
     "--role-name", TASK_ROLE_NAME,
     "--policy-name", "AllowReadProdAzulSecrets",
     "--policy-document", f"file://{policy_file}"])
os.unlink(policy_file)
print("    Politica aplicada OK")


# ─── PASO 3: Nueva task definition ───────────────────────────────────────────
print(f"\n==> PASO 3: Nueva task definition (base rev {TASK_DEF_REV})...")
raw, _ = run(["aws", "ecs", "describe-task-definition",
              "--task-definition", f"{TASK_FAM}:{TASK_DEF_REV}", "--output", "json"])
td = json.loads(raw)["taskDefinition"]

env = td["containerDefinitions"][0]["environment"]
overrides = {
    "AZUL_SECRET_CREDS": "iamatlas/azul/prod/api-credentials",
    "AZUL_SECRET_CERT":  "iamatlas/azul/prod/cert-pem",
    "AZUL_SECRET_KEY":   "iamatlas/azul/prod/cert-key",
    "AZUL_LOCAL_MODE":   "0",
}
existing = {e["name"] for e in env}
for var in env:
    if var["name"] in overrides:
        var["value"] = overrides[var["name"]]
for name, val in overrides.items():
    if name not in existing:
        env.append({"name": name, "value": val})

orig = td["containerDefinitions"][0]
payload = {
    "family":                  td["family"],
    "taskRoleArn":             role_arn,
    "executionRoleArn":        td["executionRoleArn"],
    "networkMode":             td["networkMode"],
    "requiresCompatibilities": td["requiresCompatibilities"],
    "cpu":                     td["cpu"],
    "memory":                  td["memory"],
    "runtimePlatform":         td["runtimePlatform"],
    "volumes":                 [],
    "placementConstraints":    [],
    "containerDefinitions": [{
        "name":              orig["name"],
        "image":             orig["image"],
        "cpu":               orig["cpu"],
        "memory":            orig["memory"],
        "memoryReservation": orig["memoryReservation"],
        "essential":         True,
        "portMappings":      [{"containerPort": 8000, "hostPort": 8000, "protocol": "tcp"}],
        "command":           [],
        "environment":       env,
        "mountPoints":       [],
        "volumesFrom":       [],
        "secrets":           [],
        "logConfiguration":  orig["logConfiguration"]
    }]
}
td_file = "new_task_def.json"
with open(td_file, "w", encoding="utf-8") as f:
    json.dump(payload, f, indent=2, ensure_ascii=False)

reg_out, _ = run(["aws", "ecs", "register-task-definition",
                  "--cli-input-json", f"file://{td_file}", "--output", "json"])
reg = json.loads(reg_out)
new_arn = reg["taskDefinition"]["taskDefinitionArn"]
new_rev = reg["taskDefinition"]["revision"]
print(f"    Revision: {new_rev}  |  taskRoleArn: {role_arn}")


# ─── PASO 4: Actualizar servicio ─────────────────────────────────────────────
print(f"\n==> PASO 4: update-service '{SERVICE}'...")
upd_out, _ = run(["aws", "ecs", "update-service",
                  "--cluster", CLUSTER, "--service", SERVICE,
                  "--task-definition", new_arn,
                  "--force-new-deployment", "--output", "json"])
status = json.loads(upd_out)["service"]["deployments"][0]["status"]

print()
print("=" * 58)
print(f"  EXITO")
print(f"  Task role    : {TASK_ROLE_NAME}")
print(f"  Task def rev : {new_rev}")
print(f"  Deploy status: {status}")
print("=" * 58)
