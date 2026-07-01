import subprocess, json

albs_to_delete = [
    'atlas-prod-frontend-alb',
    'atlas-prod-user-service-alb',
    'atlas-prod-notif-alb',
    'atlas-prod-adm-reservas-alb',
    'atlas-prueba-frontend-alb',
    'atlas-prueba-user-service-alb',
    'atlas-prueba-notif-alb',
    'atlas-prueba-adm-reservas-alb'
]

r = subprocess.run(['aws','elbv2','describe-load-balancers','--output','json'], capture_output=True, text=True)
lbs = json.loads(r.stdout)['LoadBalancers']

for lb in lbs:
    if lb['LoadBalancerName'] in albs_to_delete:
        arn = lb['LoadBalancerArn']
        print(f"Deleting {lb['LoadBalancerName']}...")
        r2 = subprocess.run(['aws','elbv2','delete-load-balancer','--load-balancer-arn',arn], capture_output=True, text=True)
        if r2.returncode == 0:
            print('  OK')
        else:
            print('  ERROR: ' + r2.stderr.strip())
