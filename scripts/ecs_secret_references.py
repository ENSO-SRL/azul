"""Build a task definition using existing Secrets Manager references, never values.

This writes a reviewable JSON file only. It does not register a task, rotate a
credential, change IAM, or update a running service.
"""
import argparse
import copy
import json
from pathlib import Path

SENSITIVE_NAMES = {
    'DATABASE_URL', 'API_KEY', 'SECRET_KEY', 'ATLAS_TUNNEL_SECRET_KEY',
    'USER_SERVICE_KEY', 'REDIS_PASSWORD', 'COMPANY_CARD_SECRET',
    'AZUL_KEY_PEM', 'AZUL_AUTH1', 'AZUL_AUTH2',
}
TASK_FIELDS = {
    'family', 'taskRoleArn', 'executionRoleArn', 'networkMode', 'containerDefinitions',
    'volumes', 'placementConstraints', 'requiresCompatibilities', 'cpu', 'memory',
    'tags', 'pidMode', 'ipcMode', 'proxyConfiguration', 'inferenceAccelerators',
    'ephemeralStorage', 'runtimePlatform', 'enableFaultInjection',
}


def with_secret_references(definition, bundle_arn, available_keys):
    if ':secretsmanager:' not in bundle_arn:
        raise ValueError('Provide a Secrets Manager secret ARN')
    task = copy.deepcopy({key:value for key,value in definition.items() if key in TASK_FIELDS})
    for container in task['containerDefinitions']:
        secret_fields = {item['name'] for item in container.get('environment', []) if item['name'] in SENSITIVE_NAMES}
        if not secret_fields.issubset(available_keys):
            raise ValueError('Secret bundle does not contain every required credential')
        container['environment'] = [item for item in container.get('environment', []) if item['name'] not in secret_fields]
        refs = {item['name']:item for item in container.get('secrets', [])}
        refs.update({name:{'name':name,'valueFrom':f'{bundle_arn}:{name}::'} for name in secret_fields})
        container['secrets'] = list(refs.values())
    return task


def main():
    import boto3
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--task-definition', required=True)
    parser.add_argument('--bundle-arn', required=True)
    parser.add_argument('--region', default='us-east-2')
    parser.add_argument('--output', required=True)
    args = parser.parse_args()
    ecs = boto3.client('ecs', region_name=args.region)
    secrets = boto3.client('secretsmanager', region_name=args.region)
    definition = ecs.describe_task_definition(taskDefinition=args.task_definition)['taskDefinition']
    # Validate in memory; do not save, display or log SecretString.
    bundle = json.loads(secrets.get_secret_value(SecretId=args.bundle_arn)['SecretString'])
    task = with_secret_references(definition, args.bundle_arn, set(bundle))
    Path(args.output).write_text(json.dumps(task, indent=2), encoding='utf-8')
    print('Task definition prepared with secret references. No running service changed.')


if __name__ == '__main__':
    main()
