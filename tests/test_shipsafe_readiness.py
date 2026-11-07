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
    assert 'user_data = base64encode(' not in source and 'user_data_base64 =' not in source
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
                if 'allowedValues' in param:
                    assert param['default'] in param['allowedValues']
                else:
                    assert re.fullmatch(param['allowedPattern'], param['default']) is not None


def valid_environment(doc):
    env = {k: v for k, v in os.environ.items() if not k.startswith('SSM_')}
    env.update({f'SSM_{k}': v['default'] for k, v in doc['parameters'].items() if 'default' in v})
    env.update(SSM_ArtifactS3Uri='s3://shipsafe-artifacts-542468167362-us-east-1/releases/'+'b'*40+'/shipsafe-release.tar.gz', SSM_ArtifactSha256='a'*64, SSM_ImageRef='542468167362.dkr.ecr.us-east-1.amazonaws.com/shipsafe/eventledger@sha256:'+'a'*64,
               SSM_GitSha='b'*40)
    return env


def preflight(script):
    # Stop exactly before any host command, after the real runtime input checks.
    return script.split('if [ ! -x "/usr/bin/python3.12" ]; then', 1)[0] + "exit 88\nSHIPSAFE_SSM_BASH\n"


@pytest.mark.parametrize('operation,mode,accepted', [
    ('bootstrap','none',True),('deploy','none',True),('deploy','post-promotion',True),
    ('bootstrap','post-promotion',False),('status','post-promotion',False),
    ('deploy','candidate-startup',False),('deploy','$(id)',False),('deploy','',False),
])
def test_failure_mode_schema_and_runtime_gate(documents,operation,mode,accepted):
    doc=documents['deploy']
    assert doc['parameters']['FailureMode']['allowedValues']==['none','post-promotion']
    assert doc['parameters']['FailureMode']['default']=='none'
    env=valid_environment(doc)
    env.update(SSM_Operation=operation,SSM_FailureMode=mode)
    assert shell(preflight(doc['mainSteps'][0]['inputs']['runCommand'][0]),env).returncode==(88 if accepted else 1)


@pytest.mark.parametrize('operation,mode', [('bootstrap','none'),('deploy','none'),('deploy','post-promotion')])
def test_controller_failure_flag_is_invocation_only(documents,operation,mode):
    doc=documents['deploy']
    script=doc['mainSteps'][0]['inputs']['runCommand'][0]
    # Exercise the rendered validation and exact controller invocation; skip host work.
    validation=preflight(script).split("<<'SHIPSAFE_SSM_BASH'\n",1)[1].rsplit('exit 88',1)[0]
    invocation=script.split('args=("$SSM_Operation"',1)[1].split("printf '%s' \"$release_root\"",1)[0]
    invocation='args=("$SSM_Operation"'+invocation
    env=valid_environment(doc)
    env.update(SSM_Operation=operation,SSM_FailureMode=mode,SHIPSAFE_ENABLE_FAILURE_INJECTION='1')
    spy='env() { command env "$@"; }\n'
    # Replace only the executable, using Python to report environment and argv.
    invocation=invocation.replace('/usr/bin/python3.12 shipsafe/controller.py',
        "python -c 'import json,os,sys; print(json.dumps([os.getenv(\"SHIPSAFE_ENABLE_FAILURE_INJECTION\"),sys.argv[1:]]))'")
    result=shell(validation+'\nrelease_root=/offline\nRUNTIME_ENV=/offline.env\n'+spy+invocation,env)
    assert result.returncode==0,result.stderr
    flag,args=json.loads(result.stdout)
    assert flag==('1' if mode=='post-promotion' else None)
    if operation=='deploy':
        assert args[args.index('--failure-mode')+1]==mode
    else:
        assert '--failure-mode' not in args


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
    env['SSM_ArtifactSha256'] = ''
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
    assert len(logs['Resource']) == 5 and 'logs:CreateLogGroup' in logs['Action']
    ec2 = (MAIN / 'ec2.tf').read_text()
    for dependency in ['ec2_ssm_managed','ec2_ecr_pull','ec2_cloudwatch','ec2_ssm_secrets','ec2_s3_artifacts',
                       'aws_route_table_association.public','aws_vpc_security_group_egress_rule.all_outbound']:
        assert dependency in ec2.split('depends_on = [',1)[1].split(']',1)[0]


def test_production_controller_requires_explicit_python312(documents, offline_plan):
    for document in documents.values():
        script = document['mainSteps'][0]['inputs']['runCommand'][0]
        interpreters = re.findall(r'(?m)^(?:env "\$\{controller_env\[@\]\}" )?(\S+)\s+shipsafe/controller\.py\b', script)
        assert interpreters == ['/usr/bin/python3.12']
        assert 'if [ ! -x "/usr/bin/python3.12" ]; then' in script
        assert script.index('if [ ! -x "/usr/bin/python3.12" ]; then') < script.index('mountpoint -q /data/shipsafe')
        assert script.index('if [ ! -x "/usr/bin/python3.12" ]; then') < script.index('/usr/bin/python3.12 shipsafe/controller.py')
    userdata = offline_plan['aws_instance.shipsafe']['user_data']
    packages = userdata.split('dnf install -y', 1)[1].split('# Package installation', 1)[0]
    assert re.search(r'(?m)^\s+python3\.12\s+\\$', packages)
    assert not re.search(r'(?m)^.*(?:ln\s|alternatives\s).*python3', userdata)


def test_missing_python312_fails_before_host_or_release_changes(documents, tmp_path):
    for document in documents.values():
        script = document['mainSteps'][0]['inputs']['runCommand'][0]
        # Run only the real interpreter guard, with a guaranteed missing path.
        guard = script.split('if [ ! -x "/usr/bin/python3.12" ]; then', 1)[1]
        guard = 'if [ ! -x "/usr/bin/python3.12" ]; then' + guard.split('mountpoint -q /data/shipsafe', 1)[0]
        missing = (tmp_path / 'python312-does-not-exist').as_posix()
        guard = guard.replace('"/usr/bin/python3.12"', '"' + missing + '"', 1)
        result = shell(guard + '\nexit 88\n')
        assert result.returncode == 1
        assert 'FATAL: /usr/bin/python3.12 is required' in result.stderr
        assert 'release operation aborted' in result.stderr


def test_postgres_parent_ownership_preserves_pg18_contents(offline_plan, tmp_path):
    script = offline_plan['aws_instance.shipsafe']['user_data']
    block = script.split('# PostgreSQL 18 mounts this parent', 1)[1].split('\n', 1)[1].split('# ShipSafe controller runtime state', 1)[0]
    assert 'install -d -o 999 -g 999 -m 0700 -- "$PG_PARENT"' in block
    assert not re.search(r'(?m)^\s*(?:rm|find|chown|chmod)\b', block)
    assert block.index('mountpoint -q') < block.index('install -d')
    assert block.index('grep -Fxq "EBS_VOLUME_ID=$TARGET_VOL_ID"') < block.index('install -d')
    root = tmp_path / 'volume'
    cluster = root / 'postgres/18/docker'
    cluster.mkdir(parents=True)
    version = cluster / 'PG_VERSION'
    version.write_text('18\n')
    canary = root / '.shipsafe_ebs_mounted'
    canary.write_text('EBS_VOLUME_ID=vol-test\n')
    calls = tmp_path / 'install-calls'
    # Intercept the privileged utility: assert its exact numeric owner/mode,
    # target and nonrecursive invocation without changing host ownership.
    original_metadata = (version.stat().st_mode, version.stat().st_uid, version.stat().st_gid, version.stat().st_mtime_ns)
    setup = f'set -euo pipefail\nDATA_MOUNT="{root.as_posix()}"\nTARGET_VOL_ID=vol-test\nCANARY_FILE="{canary.as_posix()}"\n'
    spy = f'install() {{ printf "%s\\n" "$*" >> "{calls.as_posix()}"; }}\n'
    good_mount = 'mountpoint() { return 0; }\n'
    result = shell(setup + good_mount + spy + block)
    assert result.returncode == 0, result.stderr
    assert calls.read_text().splitlines() == ['-d -o 999 -g 999 -m 0700 -- ' + (root / 'postgres').as_posix()]
    assert version.read_text() == '18\n'
    assert set(cluster.iterdir()) == {version}
    assert (version.stat().st_mode, version.stat().st_uid, version.stat().st_gid, version.stat().st_mtime_ns) == original_metadata
    calls.unlink()
    # Both a missing mount and an incorrect canary must block all install calls.
    result = shell(setup + 'mountpoint() { return 1; }\n' + spy + block)
    assert result.returncode == 1 and not calls.exists()
    canary.write_text('EBS_VOLUME_ID=vol-wrong\n')
    result = shell(setup + good_mount + spy + block)
    assert result.returncode == 1 and not calls.exists()
    assert version.read_text() == '18\n'
