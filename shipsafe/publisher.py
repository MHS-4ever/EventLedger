"""Publish and dispatch a release. Default is a no-network, no-mutation dry run."""
from __future__ import annotations

import argparse
import ipaddress
import json
import os
from pathlib import Path
import re
import socket
import subprocess
import time
import urllib.error
import urllib.request

from shipsafe.release import package

ACCOUNT = '542468167362'
REGION = 'us-east-1'
REPOSITORY = 'shipsafe/eventledger'
REGISTRY = f'{ACCOUNT}.dkr.ecr.{REGION}.amazonaws.com'
BUCKET = f'shipsafe-artifacts-{ACCOUNT}-{REGION}'


def command(args, missing=None):
    result = subprocess.run(args, capture_output=True, text=True, check=False)
    if result.returncode:
        if missing and missing in result.stderr:
            return None
        raise RuntimeError(f'{args[0]} command failed; no output logged')
    return result.stdout.strip()


def aws(*args, missing=None):
    text = command(['aws', '--region', REGION, *args, '--output', 'json'], missing)
    return json.loads(text) if text else None


def dispatch(operation, instance, parameters):
    result = aws('ssm', 'send-command', '--document-name', 'shipsafe-'+('deploy' if operation in ('bootstrap','deploy') else operation),
                 '--instance-ids', instance, '--parameters', json.dumps(parameters), '--timeout-seconds', '600',
                 '--cloud-watch-output-config', json.dumps({'CloudWatchLogGroupName':'/shipsafe/production/controller',
                                                            'CloudWatchOutputEnabled':True}))
    command_id = result['Command']['CommandId']
    deadline = time.monotonic()+2100
    while time.monotonic() < deadline:
        result = aws('ssm', 'list-command-invocations', '--command-id', command_id, '--details')
        invocations = result.get('CommandInvocations', [])
        if invocations:
            status = invocations[0]['Status']
            if status == 'Success':
                return command_id
            if status not in ('Pending','InProgress','Delayed'):
                raise RuntimeError(f'SSM {command_id} ended with {status}; inspect restricted CloudWatch logs')
        time.sleep(3)
    raise RuntimeError(f'SSM {command_id} did not finish; inspect it before retrying')


def verify_https(domain):
    for path in ('/health','/ready'):
        with urllib.request.urlopen('https://'+domain+path, timeout=10) as response:
            if response.status != 200:
                raise RuntimeError('HTTPS health verification failed')
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *args):
            return None
    try:
        urllib.request.build_opener(NoRedirect).open('http://'+domain+'/health', timeout=10)
    except urllib.error.HTTPError as error:
        if error.code not in (301,302,307,308) or not error.headers.get('Location','').startswith('https://'+domain+'/'):
            raise RuntimeError('HTTP must redirect to the approved HTTPS hostname') from None
    else:
        raise RuntimeError('HTTP did not redirect to HTTPS')


def run(args):
    sha = command(['git','rev-parse','HEAD'])
    if not re.fullmatch('[0-9a-f]{40}', sha):
        raise ValueError('Invalid source SHA')
    summary = {'mode':'execute' if args.execute else 'dry-run', 'operation':args.operation,
               'git_sha':sha, 'account':ACCOUNT, 'region':REGION,
               'steps':['verify main OIDC identity and DNS', 'build/test or reuse immutable sha image',
                        'resolve ECR digest', 'package committed allowlisted source',
                        'conditionally publish archive/checksum', 'send constrained SSM command',
                        'wait for SSM success', 'verify public HTTPS']}
    if not args.execute:
        return summary
    if os.getenv('GITHUB_ACTIONS') != 'true' or os.getenv('GITHUB_REF') != 'refs/heads/main' or os.getenv('GITHUB_REPOSITORY') != 'MHS-4ever/EventLedger':
        raise RuntimeError('Publisher execution is restricted to this repository main workflow')
    if command(['git','status','--porcelain','--untracked-files=no']):
        raise RuntimeError('Tracked source must be clean before publishing')
    if not args.instance or not re.fullmatch('i-[0-9a-f]{17}', args.instance):
        raise ValueError('Terraform EC2 instance output is required')
    if not args.domain or not re.fullmatch(r'(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}', args.domain):
        raise ValueError('Approved production DNS hostname is required')
    if str(ipaddress.IPv4Address(args.public_ip)) not in socket.gethostbyname_ex(args.domain)[2]:
        raise ValueError('DNS does not point to the provisioned EC2 public IPv4')
    if aws('sts','get-caller-identity')['Account'] != ACCOUNT:
        raise RuntimeError('Unexpected AWS account')
    if args.operation in ('status','rollback'):
        params = {'ReleaseId':[args.release_id]} if args.operation == 'rollback' else {}
        if args.release_id and not re.fullmatch('[0-9a-f]{12}-[0-9a-f]{12}', args.release_id):
            raise ValueError('Invalid rollback release ID')
        summary['command_id'] = dispatch(args.operation,args.instance,params)
        verify_https(args.domain)
        return summary
    # Credentials go to docker via stdin, never argv or logs.
    password = command(['aws','--region',REGION,'ecr','get-login-password'])
    result = subprocess.run(['docker','login','--username','AWS','--password-stdin',REGISTRY],
                            input=password, text=True, capture_output=True)
    if result.returncode:
        raise RuntimeError('ECR login failed')
    tag = 'sha-'+sha
    existing = aws('ecr','describe-images','--repository-name',REPOSITORY,'--image-ids','imageTag='+tag,
                   missing='ImageNotFoundException')
    if existing:
        digest = existing['imageDetails'][0]['imageDigest']
    else:
        image_tag = REGISTRY+'/'+REPOSITORY+':'+tag
        command(['docker','build','--platform','linux/amd64','--label','org.opencontainers.image.revision='+sha,'-t',image_tag,'.'])
        command(['docker','push',image_tag])
        digest = aws('ecr','describe-images','--repository-name',REPOSITORY,'--image-ids','imageTag='+tag)['imageDetails'][0]['imageDigest']
    image = REGISTRY+'/'+REPOSITORY+'@'+digest
    command(['docker','pull',image])
    revision = command(['docker','image','inspect','--format','{{index .Config.Labels "org.opencontainers.image.revision"}}',image])
    if revision != sha:
        raise RuntimeError('Existing immutable image does not match source commit')
    output = Path('shipsafe/runtime/publisher/shipsafe-release.tar.gz')
    release = package(Path.cwd(),sha,image,output)
    key = 'releases/'+sha+'/shipsafe-release.tar.gz'
    # A rerun may only reuse exactly the same deterministic archive.
    for path, object_key in [(output,key),(output.with_suffix('.gz.sha256'),key+'.sha256')]:
        result = subprocess.run(['aws','--region',REGION,'s3api','put-object','--bucket',BUCKET,'--key',object_key,
                                 '--body',str(path),'--if-none-match','*'],capture_output=True,text=True)
        if result.returncode:
            if 'PreconditionFailed' not in result.stderr:
                raise RuntimeError('Conditional artifact publication failed')
            prior = Path(str(path)+'.existing')
            command(['aws','--region',REGION,'s3api','get-object','--bucket',BUCKET,'--key',object_key,str(prior)])
            if prior.read_bytes() != path.read_bytes():
                raise RuntimeError('Immutable release artifact differs; use a new source commit')
    parameters = {'Operation':[args.operation],'ImageRef':[image],'GitSha':[sha],
                  'ArtifactS3Uri':['s3://'+BUCKET+'/'+key], 'ArtifactSha256':[release['artifact_sha256']]}
    summary.update(release)
    summary['command_id'] = dispatch(args.operation,args.instance,parameters)
    verify_https(args.domain)
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--execute',action='store_true')
    parser.add_argument('--operation',choices=['bootstrap','deploy','status','rollback'],default='bootstrap')
    parser.add_argument('--instance',default=os.getenv('SHIPSAFE_INSTANCE_ID'))
    parser.add_argument('--domain',default=os.getenv('SHIPSAFE_DOMAIN'))
    parser.add_argument('--public-ip',default=os.getenv('SHIPSAFE_PUBLIC_IP'))
    parser.add_argument('--release-id',default='')
    try:
        print(json.dumps(run(parser.parse_args()),sort_keys=True))
    except (RuntimeError,ValueError,OSError,KeyError,subprocess.CalledProcessError):
        parser.exit(1,'Release stopped safely; check prerequisites and restricted deployment logs.\n')


if __name__ == '__main__':
    main()
