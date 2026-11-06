"""Validate production Compose and Caddy syntax using synthetic offline settings."""
import json
import os
from pathlib import Path
import subprocess

from shipsafe.controller import Controller


def main():
    root = Path.cwd()
    runtime = root / 'shipsafe/runtime/config-check'
    runtime.mkdir(parents=True, exist_ok=True)
    # Reserved .invalid is a test hostname only, never a deployment value.
    config = runtime / 'deployment.json'
    config.write_text(json.dumps({'domain':'ci.example.invalid','region':'us-east-1','log_group':'/shipsafe/production'}))
    controller = Controller(root=root,runtime_dir=runtime,runtime_env=root/'shipsafe/runtime/ci.env',production_config=config)
    caddyfile = runtime / 'Caddyfile'
    caddyfile.write_text(controller._caddy_text('blue'),encoding='utf-8')
    env = dict(os.environ,SHIPSAFE_BLUE_IMAGE='eventledger:offline',SHIPSAFE_GREEN_IMAGE='eventledger:offline',
               SHIPSAFE_WORKER_IMAGE='eventledger:offline',SHIPSAFE_MIGRATION_IMAGE='eventledger:offline',
               SHIPSAFE_RUNTIME_DIR=str(runtime),SHIPSAFE_DOMAIN='ci.example.invalid',AWS_REGION='us-east-1',
               SHIPSAFE_LOG_GROUP='/shipsafe/production')
    subprocess.run(['docker','compose','--env-file','shipsafe/runtime/ci.env','-f','shipsafe/compose.production.yaml','config','--quiet'],env=env,check=True)
    subprocess.run(['docker','run','--rm','-v',str(caddyfile)+':/test/Caddyfile:ro','caddy:2.10.2-alpine@sha256:4c6e91c6ed0e2fa03efd5b44747b625fec79bc9cd06ac5235a779726618e530d',
                    'caddy','adapt','--config','/test/Caddyfile','--adapter','caddyfile','--validate'],
                   check=True,stdout=subprocess.DEVNULL)


if __name__ == '__main__':
    main()
