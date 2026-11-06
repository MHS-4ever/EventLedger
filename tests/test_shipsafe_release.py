import argparse
from copy import deepcopy
import hashlib
import io
import json
from pathlib import Path
import subprocess
import sys
import tarfile
from types import SimpleNamespace

import pytest
import yaml

from shipsafe.controller import Controller, ShipSafeError
from shipsafe import publisher, release
from shipsafe.provision_secrets import NAMES, put_parameters
from tests.test_shipsafe_controller import HarnessController, ImageRunner, runtime_env

ROOT = Path(__file__).resolve().parents[1]
SHA = 'a'*40
IMAGE = '542468167362.dkr.ecr.us-east-1.amazonaws.com/shipsafe/eventledger@sha256:'+'b'*64


@pytest.fixture
def source_repo(tmp_path):
    repo = tmp_path / 'source'
    repo.mkdir()
    for name in ['shipsafe/controller.py','shipsafe/compose.production.yaml','shipsafe/compose.yaml',
                 'app/main.py','migrations/versions/initial.py','alembic.ini','pyproject.toml','README.md',
                 '.env','shipsafe/infra/main/terraform.tfvars']:
        path = repo / name
        path.parent.mkdir(parents=True,exist_ok=True)
        path.write_text('not-a-real-secret' if name.endswith(('.env','.tfvars')) else name+'\n')
    subprocess.run(['git','init','-q',str(repo)],check=True)
    subprocess.run(['git','-C',str(repo),'add','.'],check=True)
    subprocess.run(['git','-C',str(repo),'-c','user.name=Offline Test','-c','user.email=offline@example.invalid',
                    'commit','-qm','fixture'],check=True)
    sha = subprocess.check_output(['git','-C',str(repo),'rev-parse','HEAD'],text=True).strip()
    return repo, sha


def test_packaging_is_deterministic_committed_and_secret_free(source_repo,tmp_path):
    repo, sha = source_repo
    a,b = tmp_path/'a.tar.gz',tmp_path/'b.tar.gz'
    first = release.package(repo,sha,IMAGE,a)
    (repo/'app/main.py').write_text('uncommitted changes must not ship')
    second = release.package(repo,sha,IMAGE,b)
    assert first == second and a.read_bytes() == b.read_bytes()
    with tarfile.open(a) as archive:
        names = archive.getnames()
        assert '.env' not in names and not any('infra/' in n for n in names)
        manifest = json.load(archive.extractfile('deployment.json'))
        assert manifest['git_sha'] == sha and manifest['image_ref'] == IMAGE
        assert manifest['schema_revision'] == 'd4e5f6a7b8c9'
        for name,digest in manifest['files'].items():
            assert hashlib.sha256(archive.extractfile(name).read()).hexdigest() == digest
        assert 'uncommitted' not in archive.extractfile('app/main.py').read().decode()


def test_packaging_rejects_mutable_or_foreign_image(source_repo,tmp_path):
    repo,sha = source_repo
    for image in ['eventledger:latest',IMAGE.replace('542468167362','999999999999')]:
        with pytest.raises(ValueError): release.package(repo,sha,image,tmp_path/'bad.tar.gz')


def production_controller(tmp_path, domain='offline.example.invalid'):
    config = tmp_path/'deployment.json'
    config.write_text(json.dumps({'domain':domain,'region':'us-east-1','log_group':'/shipsafe/production'}))
    return Controller(root=ROOT,runtime_dir=tmp_path/'runtime',runtime_env=runtime_env(tmp_path),production_config=config)


def test_production_config_requires_actual_hostname_and_preserves_both_upstreams(tmp_path):
    with pytest.raises(ShipSafeError): production_controller(tmp_path,domain='')
    controller = production_controller(tmp_path)
    blue,green = controller._caddy_text('blue'),controller._caddy_text('green')
    assert 'auto_https off' not in blue
    assert 'offline.example.invalid {' in blue and 'http://:8080' in blue
    assert blue.count('api-blue:8000') == 2 and green.count('api-green:8000') == 2
    assert 'admin localhost:2019' in blue
    assert 'request>headers delete' in blue and 'request>uri delete' in blue
    assert controller.compose_file.name == 'compose.production.yaml'


def test_production_compose_persists_data_and_exposes_only_caddy():
    config = yaml.safe_load((ROOT/'shipsafe/compose.production.yaml').read_text(encoding='utf-8'))
    services = config['services']
    for name,item in services.items():
        assert item['logging']['driver'] == 'awslogs'
        assert 'awslogs-stream-prefix' not in item['logging']['options']
        if name != 'caddy': assert 'ports' not in item
    assert services['db']['volumes'][0] == {'type':'bind','source':'/data/shipsafe/postgres',
                                          'target':'/var/lib/postgresql','bind':{'create_host_path':False}}
    assert services['caddy']['ports'] == ['80:80','443:443','127.0.0.1:8080:8080']
    assert {v['source'] for v in services['caddy']['volumes']} >= {'/data/shipsafe/caddy/data','/data/shipsafe/caddy/config'}
    assert services['migrate']['command'][-1] == 'd4e5f6a7b8c9'
    assert services['worker']['command'][:3] == ['python','-m','app.worker']
    assert '@sha256:' in services['db']['image'] and '@sha256:' in services['caddy']['image']


def test_failed_first_bootstrap_stops_application_and_allows_explicit_retry(tmp_path,monkeypatch):
    controller = HarnessController(root=ROOT,runtime_dir=tmp_path/'state',runtime_env=runtime_env(tmp_path),
                                   runner=ImageRunner(),failure_at='worker')
    monkeypatch.setattr(controller,'_wait_service_healthy',lambda *a: None)
    monkeypatch.setattr(controller,'_wait_proxy',lambda *a: None)
    with pytest.raises(ShipSafeError):
        controller.bootstrap(image='eventledger:test',git_sha=SHA,ready_timeout=60,worker_timeout=30)
    assert any('stop:worker:caddy:api-blue:api-green' in a for a in controller.actions)
    assert controller.store.load()['deployment']['phase'] == 'failed'
    controller.failure_at = None
    controller.bootstrap(image='eventledger:test',git_sha=SHA,ready_timeout=60,worker_timeout=30)
    assert controller.store.load()['deployment'] is None
    with pytest.raises(ShipSafeError):
        controller.bootstrap(image='eventledger:test',git_sha=SHA,ready_timeout=60,worker_timeout=30)


def test_dry_run_performs_no_cloud_or_container_operations(monkeypatch):
    monkeypatch.setattr(publisher,'command',lambda args: SHA if args[:2]==['git','rev-parse'] else pytest.fail('Unexpected command'))
    monkeypatch.setattr(publisher,'aws',lambda *a,**k: pytest.fail('AWS called in dry-run'))
    result=publisher.run(argparse.Namespace(execute=False,operation='bootstrap'))
    assert result['mode']=='dry-run' and 'command_id' not in result


def test_execution_rejects_feature_branch_before_aws(monkeypatch):
    monkeypatch.setattr(publisher,'command',lambda args: SHA)
    monkeypatch.setenv('GITHUB_ACTIONS','true')
    monkeypatch.setenv('GITHUB_REF','refs/heads/feature/shipsafe')
    monkeypatch.setenv('GITHUB_REPOSITORY','MHS-4ever/EventLedger')
    monkeypatch.setattr(publisher,'aws',lambda *a,**k: pytest.fail('AWS called before branch check'))
    with pytest.raises(RuntimeError): publisher.run(argparse.Namespace(execute=True,operation='bootstrap'))


@pytest.mark.parametrize('operation',['bootstrap','deploy','rollback','status'])
def test_ssm_dispatch_waits_for_success_and_uses_only_approved_documents(monkeypatch,operation):
    calls=[]
    def aws(*args,**kwargs):
        calls.append(args)
        if args[1]=='send-command': return {'Command':{'CommandId':'offline-command'}}
        return {'CommandInvocations':[{'Status':'Success'}]}
    monkeypatch.setattr(publisher,'aws',aws)
    assert publisher.dispatch(operation,'i-'+'a'*17,{})=='offline-command'
    assert calls[0][calls[0].index('--document-name')+1]=='shipsafe-'+('deploy' if operation in ('bootstrap','deploy') else operation)
    assert '--cloud-watch-output-config' in calls[0]


def test_failed_ssm_does_not_report_deployment_success(monkeypatch):
    monkeypatch.setattr(publisher,'aws',lambda *args,**kw: {'Command':{'CommandId':'offline'}} if args[1]=='send-command' else {'CommandInvocations':[{'Status':'Failed'}]})
    with pytest.raises(RuntimeError): publisher.dispatch('bootstrap','i-'+'a'*17,{})


def test_interactive_secret_provisioning_is_exact_and_never_uses_argv():
    calls=[]
    client=SimpleNamespace(put_parameter=lambda **kw: calls.append(kw))
    key='arn:aws:kms:us-east-1:542468167362:key/'+'a'*8+'-'+'b'*4+'-'+'c'*4+'-'+'d'*4+'-'+'e'*12
    values={name:'offline-value' for name in NAMES}
    put_parameters(client,key,values)
    assert {c['Name'] for c in calls}=={'/shipsafe/runtime/'+n for n in NAMES}
    assert all(c['Type']=='SecureString' and c['KeyId']==key and not c['Overwrite'] for c in calls)
    calls.clear()
    values['POSTGRES_PASSWORD']='unsafe\nvalue'
    with pytest.raises(ValueError): put_parameters(client,key,values)
    assert not calls


def test_workflow_oidc_and_concurrency_are_main_only():
    workflow=yaml.load((ROOT/'.github/workflows/shipsafe-release.yml').read_text(encoding='utf-8'),Loader=yaml.BaseLoader)
    assert set(workflow['on'])=={'workflow_dispatch'}
    assert workflow['concurrency']['cancel-in-progress']=='false'
    job=workflow['jobs']['publish']
    assert "github.ref == 'refs/heads/main'" in job['if']
    assert 'environment' not in job
    assert job['permissions']['id-token']=='write'
    oidc=next(step for step in job['steps'] if step.get('uses','').startswith('aws-actions/'))
    assert oidc['with']['audience']=='sts.amazonaws.com' and oidc['with']['allowed-account-ids']=='542468167362'
    for step in job['steps']:
        if 'uses' in step: assert len(step['uses'].split('@')[1])==40

def test_remote_archive_verifier_checks_identity_hashes_and_traversal(source_repo,tmp_path):
    repo,sha = source_repo
    archive = tmp_path/'release.tar.gz'
    release.package(repo,sha,IMAGE,archive)
    template = (ROOT/'shipsafe/infra/main/templates/ssm-deploy.sh.tpl').read_text(encoding='utf-8')
    script = template.split("<<'SHIPSAFE_ARCHIVE'\n",1)[1].split('\nSHIPSAFE_ARCHIVE',1)[0]
    def verify(path,destination):
        return subprocess.run([sys.executable,'-',str(path),str(destination),sha,IMAGE],input=script,text=True,capture_output=True)
    assert verify(archive,tmp_path/'safe').returncode == 0
    with tarfile.open(archive) as original:
        entries = [(member,original.extractfile(member).read()) for member in original.getmembers()]
    for case in ['traversal','link','tamper']:
        bad = tmp_path/(case+'.tar.gz')
        with tarfile.open(bad,'w:gz') as output:
            for member,data in entries:
                if case=='tamper' and member.name=='app/main.py':
                    data=b'tampered'
                    member=deepcopy(member)
                    member.size=len(data)
                output.addfile(member,io.BytesIO(data))
            if case in ('traversal','link'):
                item=tarfile.TarInfo('../escaped' if case=='traversal' else 'link')
                if case=='link': item.type,item.linkname=tarfile.SYMTYPE,'/etc/passwd'
                output.addfile(item,io.BytesIO(b''))
        assert verify(bad,tmp_path/case).returncode != 0
    assert not (tmp_path/'escaped').exists()


def test_production_compose_expands_without_a_docker_daemon(tmp_path):
    import os
    env=dict(os.environ,POSTGRES_DB='offline',POSTGRES_USER='offline',POSTGRES_PASSWORD='offline-only',
             EVENTLEDGER_API_KEY='offline-only',EVENTLEDGER_SOURCE_ID='offline',SHIPSAFE_BLUE_IMAGE=IMAGE,
             SHIPSAFE_GREEN_IMAGE=IMAGE,SHIPSAFE_WORKER_IMAGE=IMAGE,SHIPSAFE_MIGRATION_IMAGE=IMAGE,
             SHIPSAFE_DOMAIN='offline.example.invalid',SHIPSAFE_RUNTIME_DIR=str(tmp_path),
             AWS_REGION='us-east-1',SHIPSAFE_LOG_GROUP='/shipsafe/production')
    blank=tmp_path/'blank.env'
    blank.touch()
    result=subprocess.run(['docker','compose','--env-file',str(blank),'-f',str(ROOT/'shipsafe/compose.production.yaml'),
                           'config','--quiet'],env=env,capture_output=True,text=True)
    assert result.returncode==0,result.stderr


def test_complete_publisher_simulation_reuses_verified_digest_and_packages_commit(monkeypatch,tmp_path):
    monkeypatch.chdir(tmp_path)
    for key,value in {'GITHUB_ACTIONS':'true','GITHUB_REF':'refs/heads/main','GITHUB_REPOSITORY':'MHS-4ever/EventLedger'}.items():
        monkeypatch.setenv(key,value)
    monkeypatch.setattr(publisher.socket,'gethostbyname_ex',lambda domain:(domain,[],['192.0.2.1']))
    commands=[]
    def command(args,*a,**kw):
        commands.append(args)
        if args[:2]==['git','rev-parse']: return SHA
        if 'get-login-password' in args: return 'ephemeral-offline-token'
        if args[:3]==['docker','image','inspect']: return SHA
        return ''
    monkeypatch.setattr(publisher,'command',command)
    def aws(*args,**kwargs):
        if args[0]=='sts': return {'Account':publisher.ACCOUNT}
        if args[0]=='ecr': return {'imageDetails':[{'imageDigest':'sha256:'+'b'*64}]}
        raise AssertionError('unexpected AWS call')
    monkeypatch.setattr(publisher,'aws',aws)
    puts=[]
    def execute(args,**kwargs):
        if 'put-object' in args: puts.append(args)
        return SimpleNamespace(returncode=0,stdout='',stderr='')
    monkeypatch.setattr(publisher.subprocess,'run',execute)
    def package(root,sha,image,output):
        assert sha==SHA and image==IMAGE
        output.parent.mkdir(parents=True)
        output.write_bytes(b'offline-archive')
        output.with_suffix('.gz.sha256').write_text('c'*64+'\n')
        return {'artifact_sha256':'c'*64,'git_sha':sha,'image_ref':image}
    monkeypatch.setattr(publisher,'package',package)
    sent=[]
    monkeypatch.setattr(publisher,'dispatch',lambda *a: sent.append(a) or 'offline-command')
    monkeypatch.setattr(publisher,'verify_https',lambda domain:None)
    args=argparse.Namespace(execute=True,operation='bootstrap',instance='i-'+'a'*17,
                            domain='offline.example.invalid',public_ip='192.0.2.1',release_id='')
    result=publisher.run(args)
    assert result['command_id']=='offline-command'
    assert sent[0][2]['Operation']==['bootstrap'] and sent[0][2]['ImageRef']==[IMAGE]
    assert len(puts)==2 and all('--if-none-match' in p and '*' in p for p in puts)
    assert 'ephemeral-offline-token' not in json.dumps(result)
    assert not any('push' in c for c in commands)

def test_production_bootstrap_verifies_both_slots_before_independent_worker(tmp_path,monkeypatch):
    from shipsafe.controller import CommandResult
    from tests.test_shipsafe_controller import IMAGE_ID_A
    class ProductionRunner(ImageRunner):
        def run(self,command,**kwargs):
            if '.Config.Labels' in ' '.join(command):
                return CommandResult(SHA,'',0)
            return super().run(command,**kwargs)
    config=tmp_path/'production.json'
    config.write_text(json.dumps({'domain':'offline.example.invalid','region':'us-east-1','log_group':'/shipsafe/production'}))
    controller=HarnessController(root=ROOT,runtime_dir=tmp_path/'state',runtime_env=runtime_env(tmp_path),
                                 production_config=config,runner=ProductionRunner())
    monkeypatch.setattr(controller,'_wait_service_healthy',lambda *a:None)
    monkeypatch.setattr(controller,'_wait_proxy',lambda *a:None)
    controller.bootstrap(image=IMAGE,git_sha=SHA,ready_timeout=60,worker_timeout=30)
    green=controller.actions.index('candidate-smoke:green:False')
    worker=controller.actions.index('worker-probe')
    assert green < worker
    manifest=controller.store.load_release(controller.store.load()['active_release'])
    assert manifest['source_image_ref']==IMAGE and manifest['image_id']==IMAGE_ID_A
    assert manifest['worker_verified'] and manifest['candidate_verified']
