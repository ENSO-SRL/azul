import json, subprocess, sys

# Load current task def
with open("scripts/_taskdef.json", encoding="utf-8") as f:
    td = json.load(f)["taskDefinition"]

# Get container def and add API_KEY
container = td["containerDefinitions"][0]
env = container["environment"]

# Remove API_KEY if already there, then add correct value
env = [e for e in env if e["name"] != "API_KEY"]
env.append({"name": "API_KEY", "value": "atlas-dev-key-2026-colina-del-sol"})
container["environment"] = env

# Verify key vars
key_vars = {e["name"]: e["value"] for e in env}
print("=== KEY VARS ===")
for k in ["AZUL_ENV", "AZUL_MERCHANT_ID", "AZUL_AUTH1", "AZUL_LOCAL_MODE", "API_KEY"]:
    print(f"  {k} = {key_vars.get(k, 'MISSING')}")

# Build new task def payload
new_td = {
    "family": td["family"],
    "executionRoleArn": td["executionRoleArn"],
    "networkMode": td["networkMode"],
    "containerDefinitions": [container],
    "requiresCompatibilities": td["requiresCompatibilities"],
    "cpu": td["cpu"],
    "memory": td["memory"],
    "runtimePlatform": td["runtimePlatform"],
}

with open("scripts/_taskdef_new.json", "w", encoding="utf-8") as f:
    json.dump(new_td, f, indent=2)

print("\nNew task definition written to scripts/_taskdef_new.json")
print("Registering...")

result = subprocess.run(
    ["aws", "ecs", "register-task-definition", "--cli-input-json",
     "file://scripts/_taskdef_new.json", "--region", "us-east-2",
     "--query", "taskDefinition.taskDefinitionArn", "--output", "text"],
    capture_output=True, text=True
)
if result.returncode != 0:
    print("ERROR:", result.stderr)
    sys.exit(1)

new_arn = result.stdout.strip()
print(f"Registered: {new_arn}")

# Update service
print("Updating service...")
svc_result = subprocess.run(
    ["aws", "ecs", "update-service",
     "--cluster", "default",
     "--service", "pago-azul-5325",
     "--task-definition", new_arn,
     "--region", "us-east-2",
     "--query", "service.status",
     "--output", "text"],
    capture_output=True, text=True
)
if svc_result.returncode != 0:
    print("ERROR:", svc_result.stderr)
    sys.exit(1)

print(f"Service updated! Status: {svc_result.stdout.strip()}")
print(f"\nDeployment started with: {new_arn}")
