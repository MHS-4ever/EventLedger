"""Operator-only interactive SecureString provisioning; values stay in memory."""
import argparse
import getpass
import json
import re
import subprocess
import sys

NAMES = ('POSTGRES_DB','POSTGRES_USER','POSTGRES_PASSWORD','EVENTLEDGER_API_KEY','EVENTLEDGER_SOURCE_ID')


def validate(value):
    if not value or value != value.strip() or any(c in value for c in "\r\n\x00$\"'`\\#"):
        raise ValueError('Value is incompatible with the plain runtime environment format')
    return value


def put_parameters(client, key, values, overwrite=False):
    if not re.fullmatch(r'arn:aws:kms:us-east-1:542468167362:key/[0-9a-f-]{36}', key):
        raise ValueError('Use the actual Terraform KMS key ARN output')
    if set(values) != set(NAMES):
        raise ValueError('Exactly the five required parameters must be supplied')
    # Validate the entire input set before writing any parameter.
    for value in values.values():
        validate(value)
    for name in NAMES:
        client.put_parameter(Name='/shipsafe/runtime/'+name,Type='SecureString',KeyId=key,
                             Value=values[name],Overwrite=overwrite)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--key-arn',required=True)
    parser.add_argument('--overwrite',action='store_true',help='Explicit rotation only; PostgreSQL password requires coordinated database rotation')
    args = parser.parse_args()
    try:
        import boto3
        result = subprocess.run(['aws','configure','export-credentials','--profile','shipsafe-credits','--format','process'],
                                capture_output=True,text=True,check=True)
        creds = json.loads(result.stdout)
        session = boto3.Session(aws_access_key_id=creds['AccessKeyId'],aws_secret_access_key=creds['SecretAccessKey'],
                                aws_session_token=creds['SessionToken'],region_name='us-east-1')
        creds = None
        if session.client('sts').get_caller_identity()['Account'] != '542468167362':
            raise ValueError('Wrong account')
        metadata = session.client('kms').describe_key(KeyId=args.key_arn)['KeyMetadata']
        if metadata['Arn'] != args.key_arn or metadata['KeyManager'] != 'CUSTOMER' or metadata['KeyState'] != 'Enabled':
            raise ValueError('KMS key is not the enabled Terraform customer key')
        values = {name:validate(getpass.getpass(name+' (hidden): ')) for name in NAMES}
        put_parameters(session.client('ssm'),args.key_arn,values,args.overwrite)
        values.clear()
        print('Five SecureStrings supplied. Verify parameter metadata without reading values into logs.')
    except Exception:
        # No SDK exception, CLI output, secret value or traceback is printed.
        print('Parameter provisioning stopped; check account, key, input format and operator permissions.',file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
