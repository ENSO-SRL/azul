import json
import subprocess
import sys

def run(cmd):
    try:
        out = subprocess.check_output(cmd, shell=True, stderr=subprocess.DEVNULL)
        return json.loads(out)
    except Exception:
        return None

print("Obteniendo clusters...")
clusters = run('aws ecs list-clusters --output json')
if not clusters:
    sys.exit("No clusters found")

for cluster in clusters.get('clusterArns', []):
    print(f"\nCluster: {cluster.split('/')[-1]}")
    services = run(f'aws ecs list-services --cluster {cluster} --output json')
    if not services: continue
    
    for srv_arn in services.get('serviceArns', []):
        srv_info = run(f'aws ecs describe-services --cluster {cluster} --services {srv_arn} --output json')
        if not srv_info or not srv_info.get('services'): continue
        
        srv = srv_info['services'][0]
        if srv['runningCount'] > 0:
            name = srv['serviceName']
            launch_type = srv.get('launchType', 'N/A')
            task_def_arn = srv['taskDefinition']
            
            # Obtener CPU y Memoria del Task Definition
            td_info = run(f'aws ecs describe-task-definition --task-definition {task_def_arn} --output json')
            cpu = 'Unknown'
            mem = 'Unknown'
            if td_info and 'taskDefinition' in td_info:
                td = td_info['taskDefinition']
                cpu = td.get('cpu', 'N/A')
                mem = td.get('memory', 'N/A')
                
            print(f"  - Servicio: {name}")
            print(f"    Replicas: {srv['runningCount']}")
            print(f"    Launch Type: {launch_type}")
            print(f"    CPU Asignado: {cpu}")
            print(f"    Memoria Asignada: {mem}")
