#!/usr/bin/env python3
"""Run the destructive-safe local ShipSafe acceptance matrix.

This script creates clearly named local images, containers, state, synthetic
events, and a persistent Docker volume. It never runs Compose down or removes a
volume. Services are stopped in the finalizer; the volume remains for inspection.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path
from typing import Sequence

if __package__:
    from shipsafe.controller import LEGACY_REVISION, load_env_file
else:
    from controller import LEGACY_REVISION, load_env_file


VARIANTS = (
    "blue",
    "green",
    "startup-failure",
    "readiness-failure",
    "smoke-failure",
    "post-failure",
    "worker-failure",
    "interrupt",
)


def run(
    command: Sequence[str],
    *,
    environment: dict[str, str] | None = None,
    expected: set[int] | None = None,
) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(
        list(command),
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        env=environment,
        check=False,
    )
    allowed = expected or {0}
    if completed.returncode not in allowed:
        raise RuntimeError(
            f"unexpected exit {completed.returncode}: {command[0]}\n"
            f"{completed.stdout}\n{completed.stderr}"
        )
    return completed


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime-env", type=Path, required=True)
    parser.add_argument(
        "--runtime-dir",
        type=Path,
        default=Path("shipsafe/runtime/acceptance"),
    )
    parser.add_argument("--project-name", default="shipsafe-acceptance")
    arguments = parser.parse_args()

    root = Path(__file__).resolve().parents[1]
    runtime_dir = arguments.runtime_dir.resolve()
    runtime_env = arguments.runtime_env.resolve()
    state_path = runtime_dir / "state.json"
    if state_path.exists():
        existing_state = json.loads(state_path.read_text(encoding="utf-8"))
        if existing_state.get("active_release") or existing_state.get(
            "deployment"
        ):
            print(
                "acceptance state is active or interrupted; run recover or "
                "choose a new --runtime-dir",
                file=sys.stderr,
            )
            return 2
    runtime_dir.mkdir(parents=True, exist_ok=True)
    values = load_env_file(runtime_env)
    git_sha = run(["git", "rev-parse", "HEAD"]).stdout.strip()
    controller = [
        sys.executable,
        str(root / "shipsafe" / "controller.py"),
        "--root",
        str(root),
        "--runtime-dir",
        str(runtime_dir),
        "--runtime-env",
        str(runtime_env),
        "--project-name",
        arguments.project_name,
    ]
    images = {
        variant: f"eventledger:shipsafe-acceptance-{variant}"
        for variant in VARIANTS
    }
    evidence: list[dict[str, object]] = []

    def record(name: str, outcome: str, detail: str = "") -> None:
        evidence.append({"scenario": name, "outcome": outcome, "detail": detail})
        print(f"[{outcome}] {name}{': ' + detail if detail else ''}")

    def invoke(
        arguments_list: Sequence[str],
        *,
        expected: set[int] | None = None,
        inject: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        environment = os.environ.copy()
        if inject:
            environment["SHIPSAFE_ENABLE_FAILURE_INJECTION"] = "1"
        return run(
            [*controller, *arguments_list],
            environment=environment,
            expected=expected,
        )

    def state() -> dict:
        return json.loads(state_path.read_text(encoding="utf-8"))

    compose = [
        "docker",
        "compose",
        "--project-name",
        arguments.project_name,
        "--env-file",
        str(runtime_dir / "compose.env"),
        "--env-file",
        str(runtime_env),
        "-f",
        str(root / "shipsafe" / "compose.yaml"),
    ]

    try:
        for variant, image in images.items():
            run(
                [
                    "docker",
                    "build",
                    "--label",
                    f"shipsafe.acceptance.variant={variant}",
                    "--label",
                    f"org.opencontainers.image.revision={git_sha}",
                    "-t",
                    image,
                    str(root),
                ]
            )
        record("acceptance images", "passed", f"{len(images)} immutable IDs")

        invoke(
            [
                "bootstrap",
                "--image",
                images["blue"],
                "--git-sha",
                git_sha,
                "--ready-timeout",
                "90",
                "--worker-timeout",
                "60",
            ]
        )
        initial_event = state()["last_worker_probe_event"]
        initial_release = state()["active_release"]
        record("initial blue bootstrap", "passed", initial_release)

        invoke(
            [
                "deploy",
                "--image",
                images["green"],
                "--git-sha",
                git_sha,
                "--ready-timeout",
                "60",
                "--observation-seconds",
                "3",
                "--worker-timeout",
                "60",
            ]
        )
        green_release = state()["active_release"]
        if state()["active_slot"] != "green":
            raise RuntimeError("healthy deployment did not activate green")
        record("healthy blue-to-green", "passed", green_release)

        invoke(
            [
                "rollback",
                "--ready-timeout",
                "60",
                "--observation-seconds",
                "2",
                "--worker-timeout",
                "60",
            ]
        )
        if state()["active_release"] != initial_release:
            raise RuntimeError("manual rollback did not restore blue release")
        record("manual rollback", "passed", initial_release)

        failures = (
            ("candidate startup failure", "startup-failure", "candidate-startup"),
            (
                "candidate readiness failure",
                "readiness-failure",
                "candidate-readiness",
            ),
            ("candidate smoke failure", "smoke-failure", "candidate-smoke"),
            ("post-promotion reversal", "post-failure", "post-promotion"),
            ("worker failure recovery", "worker-failure", "worker-startup"),
        )
        for scenario, variant, mode in failures:
            before = state()["active_release"]
            invoke(
                [
                    "deploy",
                    "--image",
                    images[variant],
                    "--git-sha",
                    git_sha,
                    "--failure-mode",
                    mode,
                    "--ready-timeout",
                    "10",
                    "--observation-seconds",
                    "2",
                    "--worker-timeout",
                    "12",
                ],
                expected={1},
                inject=True,
            )
            if state()["active_release"] != before or state()["deployment"]:
                raise RuntimeError(f"{scenario} changed active release")
            record(scenario, "passed", "previous release retained")

        lock_code = (
            "import sys,time; from pathlib import Path; "
            "from shipsafe.controller import HostLock; "
            "lock=HostLock(Path(sys.argv[1])); lock.__enter__(); "
            "print('locked',flush=True); time.sleep(6); lock.__exit__()"
        )
        holder = subprocess.Popen(
            [sys.executable, "-c", lock_code, str(runtime_dir / "deploy.lock")],
            cwd=root,
            text=True,
            encoding="utf-8",
            errors="replace",
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        if holder.stdout is None or holder.stdout.readline().strip() != "locked":
            raise RuntimeError("unable to acquire acceptance concurrency lock")
        blocked = invoke(
            [
                "deploy",
                "--image",
                images["interrupt"],
                "--git-sha",
                git_sha,
            ],
            expected={1},
        )
        holder.wait(timeout=10)
        if "host lock" not in blocked.stderr:
            raise RuntimeError("concurrent deployment was not lock-rejected")
        record("concurrent deployment", "passed", "second command rejected")

        before_interrupt = state()["active_release"]
        interrupted = invoke(
            [
                "deploy",
                "--image",
                images["interrupt"],
                "--git-sha",
                git_sha,
                "--failure-mode",
                "interrupt-after-switch",
                "--ready-timeout",
                "60",
                "--observation-seconds",
                "2",
                "--worker-timeout",
                "20",
            ],
            expected={75},
            inject=True,
        )
        del interrupted
        if not state()["deployment"]:
            raise RuntimeError("interruption did not leave recoverable state")
        invoke(["recover", "--ready-timeout", "60"])
        if state()["active_release"] != before_interrupt or state()["deployment"]:
            raise RuntimeError("recovery did not restore previous release")
        record("interrupted deployment recovery", "passed")

        repeated = invoke(
            [
                "deploy",
                "--image",
                images["blue"],
                "--git-sha",
                git_sha,
            ]
        )
        if "already active" not in repeated.stdout:
            raise RuntimeError("repeated deployment was not idempotent")
        record("repeated deployment", "passed", "no-op")

        request = urllib.request.Request(
            f"http://127.0.0.1:{values.get('SHIPSAFE_PROXY_PORT', '8080')}"
            f"/v1/events/{initial_event}",
            headers={"X-API-Key": values["EVENTLEDGER_API_KEY"]},
        )
        with urllib.request.urlopen(request, timeout=5) as response:
            body = json.loads(response.read())
        if response.status != 200 or body.get("id") != initial_event:
            raise RuntimeError("bootstrap event did not persist across releases")
        revision = run(
            [
                *compose,
                "exec",
                "-T",
                "db",
                "psql",
                "-U",
                values["POSTGRES_USER"],
                "-d",
                values["POSTGRES_DB"],
                "-tAc",
                "SELECT version_num FROM alembic_version",
            ]
        ).stdout.strip()
        if revision != LEGACY_REVISION:
            raise RuntimeError(f"unexpected database revision: {revision}")
        record(
            "database persistence and revision",
            "passed",
            f"event retained; revision {revision}",
        )

        (runtime_dir / "acceptance-results.json").write_text(
            json.dumps(evidence, indent=2) + "\n",
            encoding="utf-8",
        )
        return 0
    except Exception as error:
        record("acceptance run", "failed", str(error).splitlines()[0][:300])
        (runtime_dir / "acceptance-results.json").write_text(
            json.dumps(evidence, indent=2) + "\n",
            encoding="utf-8",
        )
        print(f"acceptance error: {error}", file=sys.stderr)
        return 1
    finally:
        if (runtime_dir / "compose.env").exists():
            run([*compose, "stop"], expected={0, 1})


if __name__ == "__main__":
    raise SystemExit(main())
