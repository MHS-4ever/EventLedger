import json
import os
from pathlib import Path
import re
import shutil
import subprocess
from types import SimpleNamespace

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
MAIN = ROOT / 'shipsafe/infra/main'
BASH = shutil.which('bash')
if os.name == 'nt':
    BASH = next((str(p) for p in [Path('C:/Program Files/Git/bin/bash.exe')] if p.exists()), BASH)


@pytest.fixture(scope='module')
def offline_plan():
    result = subprocess.run(['terraform', f'-chdir={MAIN}', 'test', '-json', '-verbose'],
                            capture_output=True, text=True, encoding='utf-8', timeout=120)
    events = [json.loads(line) for line in result.stdout.splitlines() if line.startswith('{')]
    assert result.returncode == 0, result.stdout + result.stderr
    summary = next(e['test_summary'] for e in events if e['type'] == 'test_summary')
    assert summary['failed'] == 0
    plan = next(e['test_plan'] for e in events if e['type'] == 'test_plan' and e['@testrun'] == 'readiness')
    return {r['address']: r['change']['after'] for r in plan['resource_changes'] if r['mode'] == 'managed'}


@pytest.fixture(scope='module')
def documents(offline_plan):
    return {name: yaml.safe_load(offline_plan[f'aws_ssm_document.{name}']['content'])
            for name in ['deploy', 'status', 'rollback']}


def shell(script, env=None, syntax=False):
    assert BASH, 'Bash is required for offline Linux script validation'
    args = [BASH, '--noprofile', '--norc'] + (['-n'] if syntax else [])
    return subprocess.run(args, input=script, capture_output=True, text=True,
                          env=env, timeout=15)


def test_rendered_cloud_init_script_is_plain_lf_and_valid_bash(offline_plan):
    script = offline_plan['aws_instance.shipsafe']['user_data']
    assert script.startswith('#!/usr/bin/env bash\n')
    assert '\r' not in script and len(script.encode()) <= 16384
    source = (MAIN / 'ec2.tf').read_text()
    assert 'user_data = replace(templatefile(' in source
    assert 'base64encode(' not in source and 'user_data_base64 =' not in source
    assert shell(script, syntax=True).returncode == 0
    helper = script.split("<<'SECRETS_SCRIPT'\n", 1)[1].split('\nSECRETS_SCRIPT', 1)[0]
    compile(helper, 'rendered-fetch-secrets', 'exec')
    assert script.index('SSM_VERSION=') < script.index('touch /var/lib/shipsafe-bootstrap.ready')


def test_all_rendered_documents_have_safe_linux_shell_and_parameters(documents):
    for doc in documents.values():
        assert doc['schemaVersion'] == '2.2'
        step = doc['mainSteps'][0]
        assert step['precondition'] == {'StringEquals': ['platformType', 'Linux']}
        script = step['inputs']['runCommand'][0]
        assert "exec /bin/bash <<'SHIPSAFE_SSM_BASH'" in script
        assert shell(script, syntax=True).returncode == 0
        body = script.split("<<'SHIPSAFE_SSM_BASH'\n", 1)[1].rsplit('SHIPSAFE_SSM_BASH', 1)[0]
        assert shell(body, syntax=True).returncode == 0
        assert '{{' not in body
        assert 'mountpoint -q /data/shipsafe' in body
        assert "grep -Fxq 'EBS_VOLUME_ID=vol-0123456789abcdef0'" in body
        assert '/var/lib/shipsafe-bootstrap.ready' in body
        for name, param in doc['parameters'].items():
            assert param['interpolationType'] == 'ENV_VAR'
            assert f'SSM_{name}' in script
            if 'default' in param:
                assert re.fullmatch(param['allowedPattern'], param['default']) is not None


def valid_environment(doc):
    env = {k: v for k, v in os.environ.items() if not k.startswith('SSM_')}
    env.update({f'SSM_{k}': v['default'] for k, v in doc['parameters'].items() if 'default' in v})
    env.update(SSM_ImageRef='542468167362.dkr.ecr.us-east-1.amazonaws.com/shipsafe/eventledger@sha256:'+'a'*64,
               SSM_GitSha='b'*40)
    return env


def preflight(script):
    # Stop exactly before any host command, after the real runtime input checks.
    return script.split('mountpoint -q /data/shipsafe', 1)[0] + "exit 88\nSHIPSAFE_SSM_BASH\n"


@pytest.mark.parametrize('parameter,valid,invalid', [
    ('ReadyTimeout', ['60','120','300'], ['0','59','301','999','60;id']),
    ('WorkerTimeout', ['30','90','180'], ['29','181','999','$(id)']),
    ('ObservationSeconds', ['5','15','60'], ['0','4','61','15\nexit 0']),
    ('GitSha', ['b'*40], ['b'*39,'../evil','$(id)']),
    ('ImageRef', ['542468167362.dkr.ecr.us-east-1.amazonaws.com/shipsafe/eventledger@sha256:'+'a'*64],
     ['999999999999.dkr.ecr.us-east-1.amazonaws.com/shipsafe/eventledger@sha256:'+'a'*64,
      '542468167362.dkr.ecr.eu-west-1.amazonaws.com/shipsafe/eventledger@sha256:'+'a'*64, '$(id)']),
])
def test_ssm_schema_and_runtime_enforce_same_input_boundaries(documents, parameter, valid, invalid):
    doc = documents['deploy']
    script = preflight(doc['mainSteps'][0]['inputs']['runCommand'][0])
    pattern = doc['parameters'][parameter]['allowedPattern']
    for value in valid + invalid:
        accepted = value in valid
        assert bool(re.fullmatch(pattern, value)) == accepted
        env = valid_environment(doc)
        env[f'SSM_{parameter}'] = value
        assert shell(script, env).returncode == (88 if accepted else 1)


def test_artifacts_require_checksum_and_matching_sha_and_old_agents_fail_closed(documents):
    doc = documents['deploy']
    script = preflight(doc['mainSteps'][0]['inputs']['runCommand'][0])
    env = valid_environment(doc)
    assert shell(script, env).returncode == 88
    env['SSM_ArtifactS3Uri'] = 's3://shipsafe-artifacts-542468167362-us-east-1/releases/'+'b'*40+'/shipsafe-release.tar.gz'
    assert shell(script, env).returncode == 1
    env['SSM_ArtifactSha256'] = 'a'*64
    assert shell(script, env).returncode == 88
    env['SSM_GitSha'] = 'c'*40
    assert shell(script, env).returncode == 1
    for parameter in doc['parameters']:
        env = valid_environment(doc)
        del env[f'SSM_{parameter}']
        assert shell(script, env).returncode != 88


def test_rollback_quotes_one_argument_and_rejects_injection(documents):
    doc = documents['rollback']
    script = preflight(doc['mainSteps'][0]['inputs']['runCommand'][0])
    for value, accepted in [('', True), ('a'*12+'-'+'b'*12, True), ('--help',False), ('$(id)',False), ('../x',False)]:
        assert bool(re.fullmatch(doc['parameters']['ReleaseId']['allowedPattern'], value)) == accepted
        assert shell(script, dict(os.environ, SSM_ReleaseId=value)).returncode == (88 if accepted else 1)
    body = doc['mainSteps'][0]['inputs']['runCommand'][0]
    assert '"${args[@]}"' in body


def test_secret_fetch_is_atomic_and_rejects_partial_or_unsafe_values(tmp_path, monkeypatch):
    code = (MAIN / 'templates/fetch-secrets.py.tpl').read_text().replace('${aws_region}', 'us-east-1').replace('${ebs_volume_id}', 'vol-test')
    namespace = {'__name__': 'offline_test'}
    exec(compile(code, 'fetch-secrets', 'exec'), namespace)
    monkeypatch.setitem(namespace, 'Path', lambda _: tmp_path)
    monkeypatch.setattr(os.path, 'ismount', lambda _: True)
    (tmp_path / '.shipsafe_ebs_mounted').write_text('EBS_VOLUME_ID=vol-test\n')
    keys = ['POSTGRES_DB','POSTGRES_USER','POSTGRES_PASSWORD','EVENTLEDGER_API_KEY','EVENTLEDGER_SOURCE_ID']
    items = [{'Name':'/shipsafe/runtime/'+k, 'Value':'safe-value', 'Type':'SecureString'} for k in keys]
    monkeypatch.setattr(subprocess, 'run', lambda *a, **kw: SimpleNamespace(stdout=json.dumps({'Parameters':items})))
    namespace['main']()
    original = (tmp_path / 'runtime.env').read_text()
    for bad in ['newline\nINJECTED=yes', '$EXPAND', ' leading', 'unsafe#comment']:
        items[0]['Value'] = bad
        with pytest.raises(ValueError): namespace['main']()
        assert (tmp_path / 'runtime.env').read_text() == original
        assert not list(tmp_path.glob('.runtime.env.*'))
    items.pop()
    items[0]['Value'] = 'safe-value'
    with pytest.raises(ValueError): namespace['main']()


def test_iam_and_first_boot_dependencies_are_scoped(offline_plan):
    policy = json.loads(offline_plan['aws_iam_policy.ssm_deploy']['policy'])
    statements = {s['Sid']:s for s in policy['Statement']}
    assert 'Condition' not in statements['SSMSendDocument']
    assert statements['SSMSendTaggedInstance']['Condition']['StringEquals'] == {'ssm:resourceTag/Project':'ShipSafe'}
    logs = json.loads(offline_plan['aws_iam_policy.cloudwatch_write']['policy'])['Statement'][0]
    assert len(logs['Resource']) == 5 and 'logs:CreateLogGroup' not in logs['Action']
    ec2 = (MAIN / 'ec2.tf').read_text()
    for dependency in ['ec2_ssm_managed','ec2_ecr_pull','ec2_cloudwatch','ec2_ssm_secrets','ec2_s3_artifacts',
                       'aws_route_table_association.public','aws_vpc_security_group_egress_rule.all_outbound']:
        assert dependency in ec2.split('depends_on = [',1)[1].split(']',1)[0]
