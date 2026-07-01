import subprocess, json

prod_map = {
    'http://atlas-prod-alb-1396016093.us-east-2.elb.amazonaws.com': 'https://api.iamatlas.do',
    'http://atlas-prod-adm-reservas-alb-1363367868.us-east-2.elb.amazonaws.com': 'https://api.iamatlas.do',
    'http://atlas-prod-notif-alb-1836892526.us-east-2.elb.amazonaws.com': 'https://api.iamatlas.do',
    'https://api.iamatlas.do/pwa_cayacoa': 'https://iamatlas.do/pwa_cayacoa'
}

prueba_map = {
    'http://atlas-prueba-alb-1522765782.us-east-2.elb.amazonaws.com': 'https://api-qa.iamatlas.do',
    'http://atlas-prueba-frontend-alb-2122755123.us-east-2.elb.amazonaws.com': 'https://qa.iamatlas.do',
    'http://atlas-prueba-adm-reservas-alb-221479152.us-east-2.elb.amazonaws.com': 'https://api-qa.iamatlas.do',
    'http://atlas-prueba-notif-alb-2136418283.us-east-2.elb.amazonaws.com': 'https://api-qa.iamatlas.do',
    'https://api-qa.iamatlas.do/pwa_cayacoa': 'https://qa.iamatlas.do/pwa_cayacoa'
}

services = [
    ('Atlas-Prod', 'atlas-prod-automatizacion-svc'),
    ('Atlas-Prueba', 'frontend-prueba'),
    ('Atlas-Prueba', 'user-service-prueba'),
    ('Atlas-Prueba', 'notificaciones-prueba'),
    ('Atlas-Prueba', 'atlas-prueba-adm-reservas-svc'),
    ('Atlas-Prueba', 'atlas-prueba-automatizacion-svc'),
]

for cluster, svc in services:
    r = subprocess.run(['aws','ecs','describe-services','--cluster',cluster,'--services',svc,'--query','services[0].taskDefinition','--output','text'], capture_output=True, text=True)
    task_def_arn = r.stdout.strip()
    
    r2 = subprocess.run(['aws','ecs','describe-task-definition','--task-definition',task_def_arn,'--output','json'], capture_output=True, text=True)
    tdef = json.loads(r2.stdout)['taskDefinition']
    
    changed = False
    for container in tdef['containerDefinitions']:
        if 'environment' in container:
            for env in container['environment']:
                val = env['value']
                
                mapping = prod_map if cluster == 'Atlas-Prod' else prueba_map
                for old, new in mapping.items():
                    if old in val:
                        val = val.replace(old, new)
                
                for old, new in mapping.items():
                     if old in val:
                         val = val.replace(old, new)

                if env['value'] != val:
                    print(f"{svc} [{env['name']}]: {env['value']} -> {val}")
                    env['value'] = val
                    changed = True
                    
    if changed:
        new_def = {
            'family': tdef['family'],
            'taskRoleArn': tdef.get('taskRoleArn'),
            'executionRoleArn': tdef.get('executionRoleArn'),
            'networkMode': tdef.get('networkMode'),
            'containerDefinitions': tdef['containerDefinitions'],
            'volumes': tdef.get('volumes', []),
            'requiresCompatibilities': tdef.get('requiresCompatibilities', []),
            'cpu': tdef.get('cpu'),
            'memory': tdef.get('memory')
        }
        new_def = {k:v for k,v in new_def.items() if v is not None}
        
        with open('temp_task_def.json', 'w') as f:
            json.dump(new_def, f)
            
        r3 = subprocess.run(['aws','ecs','register-task-definition','--cli-input-json','file://temp_task_def.json','--query','taskDefinition.taskDefinitionArn','--output','text'], capture_output=True, text=True)
        new_arn = r3.stdout.strip()
        
        subprocess.run(['aws','ecs','update-service','--cluster',cluster,'--service',svc,'--task-definition',new_arn], capture_output=True, text=True)
        print(f"Updated {svc} to {new_arn}")
