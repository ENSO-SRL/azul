import json
import subprocess
import sys

policy = {
  "rules": [
    {
      "rulePriority": 1,
      "description": "Mantener solo las ultimas 10 imagenes",
      "selection": {
        "tagStatus": "any",
        "countType": "imageCountMoreThan",
        "countNumber": 10
      },
      "action": {
        "type": "expire"
      }
    }
  ]
}

with open('ecr_policy.json', 'w') as f:
    json.dump(policy, f)

print("Obteniendo repositorios...")
repos_out = subprocess.check_output('aws ecr describe-repositories --region us-east-2 --output json', shell=True)
repos = json.loads(repos_out)

for r in repos.get('repositories', []):
    repo_name = r['repositoryName']
    print(f"Aplicando a: {repo_name}")
    try:
        subprocess.check_call(f'aws ecr put-lifecycle-policy --repository-name {repo_name} --lifecycle-policy-text file://ecr_policy.json --region us-east-2', shell=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        print("  Listo.")
    except Exception as e:
        print(f"  Error: {e}")

print("Todas las politicas aplicadas exitosamente.")
