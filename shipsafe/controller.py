#!/usr/bin/env python3
"""Local single-host ShipSafe blue/green deployment controller.

The controller stores no credentials. It passes the operator-owned runtime
environment file directly to Docker Compose and records only release identity,
slot, phase, and sanitized outcome metadata under shipsafe/runtime.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence
from uuid import uuid4


STATE_VERSION = 1
LEGACY_REVISION = "d4e5f6a7b8c9"
VALID_SLOTS = frozenset({"blue", "green"})
DEFAULT_PROJECT = "shipsafe-local"
DEFAULT_READY_TIMEOUT = 60
DEFAULT_OBSERVATION_SECONDS = 15
MAX_FAILURE_REASON = 300


class ShipSafeError(RuntimeError):
    pass


class DeploymentLocked(ShipSafeError):
    pass


class CommandFailed(ShipSafeError):
    def __init__(self, command: Sequence[str], returncode: int) -> None:
        self.command = tuple(command)
        self.returncode = returncode
        super().__init__(
            f"command failed with exit code {returncode}: {command[0]}"
        )


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def sanitize_reason(value: object) -> str:
    text = " ".join(str(value).split())
    return text[:MAX_FAILURE_REASON] or "unspecified failure"


def atomic_write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        dir=path.parent,
        text=True,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(value, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def atomic_write_text(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        dir=path.parent,
        text=True,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(value)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def load_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for line_number, raw_line in enumerate(
        path.read_text(encoding="utf-8").splitlines(),
        start=1,
    ):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise ShipSafeError(
                f"invalid runtime environment line {line_number}"
            )
        name, value = line.split("=", 1)
        name = name.strip()
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
            raise ShipSafeError(
                f"invalid runtime environment name on line {line_number}"
            )
        values[name] = value.strip()
    required = {
        "POSTGRES_DB",
        "POSTGRES_USER",
        "POSTGRES_PASSWORD",
        "EVENTLEDGER_API_KEY",
        "EVENTLEDGER_SOURCE_ID",
    }
    missing = sorted(name for name in required if not values.get(name))
    if missing:
        raise ShipSafeError(
            "runtime environment is missing required values: "
            + ", ".join(missing)
        )
    return values


class HostLock(AbstractContextManager["HostLock"]):
    def __init__(self, path: Path) -> None:
        self.path = path
        self.handle: Any = None

    def __enter__(self) -> "HostLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.handle = self.path.open("a+", encoding="utf-8")
        try:
            if os.name == "nt":
                import msvcrt

                self.handle.seek(0)
                if not self.handle.read(1):
                    self.handle.write("\0")
                    self.handle.flush()
                self.handle.seek(0)
                msvcrt.locking(self.handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(
                    self.handle.fileno(),
                    fcntl.LOCK_EX | fcntl.LOCK_NB,
                )
        except (BlockingIOError, OSError):
            self.handle.close()
            self.handle = None
            raise DeploymentLocked(
                "another ShipSafe deployment command holds the host lock"
            ) from None

        self.handle.seek(0)
        self.handle.truncate()
        json.dump(
            {"pid": os.getpid(), "acquired_at": utc_now()},
            self.handle,
        )
        self.handle.flush()
        return self

    def __exit__(self, *exc: object) -> None:
        del exc
        if self.handle is None:
            return
        try:
            if os.name == "nt":
                import msvcrt

                self.handle.seek(0)
                msvcrt.locking(self.handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
        finally:
            self.handle.close()
            self.handle = None


@dataclass(frozen=True, slots=True)
class CommandResult:
    stdout: str
    stderr: str
    returncode: int


class Runner:
    def run(
        self,
        command: Sequence[str],
        *,
        input_text: str | None = None,
        timeout: float | None = None,
        check: bool = True,
    ) -> CommandResult:
        completed = subprocess.run(
            list(command),
            input=input_text,
            text=True,
            capture_output=True,
            timeout=timeout,
            check=False,
        )
        result = CommandResult(
            completed.stdout.strip(),
            completed.stderr.strip(),
            completed.returncode,
        )
        if check and completed.returncode != 0:
            raise CommandFailed(command, completed.returncode)
        return result


class StateStore:
    def __init__(self, runtime_dir: Path) -> None:
        self.runtime_dir = runtime_dir
        self.state_path = runtime_dir / "state.json"
        self.releases_dir = runtime_dir / "releases"

    def load(self, *, required: bool = True) -> dict[str, Any]:
        if not self.state_path.exists():
            if required:
                raise ShipSafeError("ShipSafe is not bootstrapped")
            return {
                "version": STATE_VERSION,
                "active_slot": None,
                "active_release": None,
                "previous_release": None,
                "worker_release": None,
                "slot_releases": {"blue": None, "green": None},
                "deployment": None,
                "history": [],
            }
        state = json.loads(self.state_path.read_text(encoding="utf-8"))
        if state.get("version") != STATE_VERSION:
            raise ShipSafeError("unsupported ShipSafe state version")
        return state

    def save(self, state: dict[str, Any]) -> None:
        atomic_write_json(self.state_path, state)

    def save_release(self, manifest: dict[str, Any]) -> None:
        release_id = manifest["release_id"]
        atomic_write_json(self.releases_dir / f"{release_id}.json", manifest)

    def load_release(self, release_id: str) -> dict[str, Any]:
        path = self.releases_dir / f"{release_id}.json"
        if not path.exists():
            raise ShipSafeError(f"release is not recorded: {release_id}")
        return json.loads(path.read_text(encoding="utf-8"))


class Controller:
    def __init__(
        self,
        *,
        root: Path,
        runtime_dir: Path,
        runtime_env: Path,
        runner: Runner | None = None,
        project_name: str = DEFAULT_PROJECT,
    ) -> None:
        self.root = root.resolve()
        self.shipsafe_dir = self.root / "shipsafe"
        self.runtime_dir = runtime_dir.resolve()
        self.runtime_env = runtime_env.resolve()
        self.compose_file = self.shipsafe_dir / "compose.yaml"
        self.project_name = project_name
        self.runner = runner or Runner()
        self.store = StateStore(self.runtime_dir)
        self.runtime_values = load_env_file(self.runtime_env)

    @property
    def proxy_url(self) -> str:
        port = self.runtime_values.get("SHIPSAFE_PROXY_PORT", "8080")
        if not port.isdigit():
            raise ShipSafeError("SHIPSAFE_PROXY_PORT must be numeric")
        return f"http://127.0.0.1:{port}"

    def _compose_env_path(self) -> Path:
        return self.runtime_dir / "compose.env"

    def _compose_command(
        self,
        *arguments: str,
        extra_files: Sequence[Path] = (),
    ) -> list[str]:
        command = [
            "docker",
            "compose",
            "--project-name",
            self.project_name,
            "--env-file",
            str(self._compose_env_path()),
            "--env-file",
            str(self.runtime_env),
            "-f",
            str(self.compose_file),
        ]
        for extra_file in extra_files:
            command.extend(["-f", str(extra_file)])
        command.extend(arguments)
        return command

    def _compose(
        self,
        *arguments: str,
        extra_files: Sequence[Path] = (),
        check: bool = True,
        timeout: float | None = None,
        input_text: str | None = None,
    ) -> CommandResult:
        return self.runner.run(
            self._compose_command(*arguments, extra_files=extra_files),
            check=check,
            timeout=timeout,
            input_text=input_text,
        )

    def _write_compose_env(self, state: dict[str, Any]) -> None:
        releases: dict[str, dict[str, Any]] = {}
        for release_id in set(
            value
            for value in [
                *state["slot_releases"].values(),
                state.get("worker_release"),
                state.get("active_release"),
            ]
            if value
        ):
            releases[release_id] = self.store.load_release(release_id)

        fallback = next(iter(releases.values()), None)
        if fallback is None:
            raise ShipSafeError("no release image is available for Compose")

        def image_for(release_id: str | None) -> str:
            return releases.get(release_id, fallback)["image_ref"]

        values = {
            "SHIPSAFE_BLUE_IMAGE": image_for(
                state["slot_releases"].get("blue")
            ),
            "SHIPSAFE_GREEN_IMAGE": image_for(
                state["slot_releases"].get("green")
            ),
            "SHIPSAFE_WORKER_IMAGE": image_for(state.get("worker_release")),
            "SHIPSAFE_MIGRATION_IMAGE": image_for(
                state.get("active_release")
            ),
            "SHIPSAFE_RUNTIME_DIR": self.runtime_dir.as_posix(),
        }
        lines = []
        for name, value in values.items():
            if "\n" in value or "\r" in value:
                raise ShipSafeError("invalid newline in Compose environment")
            escaped = value.replace("\\", "\\\\").replace('"', '\\"')
            lines.append(f'{name}="{escaped}"')
        atomic_write_text(self._compose_env_path(), "\n".join(lines) + "\n")

    def _register_release(
        self,
        image: str,
        git_sha: str,
    ) -> dict[str, Any]:
        normalized_sha = git_sha.lower()
        if not re.fullmatch(r"[0-9a-f]{7,64}", normalized_sha):
            raise ShipSafeError("git SHA must be 7-64 hexadecimal characters")
        inspected = self.runner.run(
            ["docker", "image", "inspect", "--format", "{{.Id}}", image]
        ).stdout.strip()
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", inspected):
            raise ShipSafeError("Docker returned an invalid image ID")
        release_id = f"{normalized_sha[:12]}-{inspected.removeprefix('sha256:')[:12]}"
        immutable_ref = f"eventledger:shipsafe-{release_id}"
        self.runner.run(["docker", "image", "tag", inspected, immutable_ref])
        resolved = self.runner.run(
            [
                "docker",
                "image",
                "inspect",
                "--format",
                "{{.Id}}",
                immutable_ref,
            ]
        ).stdout.strip()
        if resolved != inspected:
            raise ShipSafeError("immutable release tag does not resolve to image ID")

        manifest_path = self.store.releases_dir / f"{release_id}.json"
        if manifest_path.exists():
            existing = self.store.load_release(release_id)
            if (
                existing.get("git_sha") != normalized_sha
                or existing.get("image_id") != inspected
            ):
                raise ShipSafeError("release identity collision")
            return existing

        manifest = {
            "manifest_version": 1,
            "release_id": release_id,
            "git_sha": normalized_sha,
            "image_id": inspected,
            "image_ref": immutable_ref,
            "created_at": utc_now(),
            "schema_mode": "legacy",
            "legacy_revision": LEGACY_REVISION,
            "candidate_verified": False,
            "worker_verified": False,
            "status": "registered",
            "failure_reason": None,
        }
        self.store.save_release(manifest)
        return manifest

    def _assert_release_image(self, manifest: dict[str, Any]) -> None:
        result = self.runner.run(
            [
                "docker",
                "image",
                "inspect",
                "--format",
                "{{.Id}}",
                manifest["image_ref"],
            ]
        )
        if result.stdout.strip() != manifest["image_id"]:
            raise ShipSafeError(
                f"release image identity mismatch: {manifest['release_id']}"
            )

    def _caddy_text(self, slot: str) -> str:
        if slot not in VALID_SLOTS:
            raise ShipSafeError("invalid Caddy slot")
        return (
            "{\n"
            "    admin localhost:2019\n"
            "    auto_https off\n"
            "}\n\n"
            ":8080 {\n"
            "    encode gzip\n"
            f"    reverse_proxy api-{slot}:8000\n"
            "}\n"
        )

    def _write_initial_caddy(self, slot: str) -> None:
        atomic_write_text(self.runtime_dir / "Caddyfile", self._caddy_text(slot))

    def _switch_caddy(self, slot: str) -> None:
        candidate = self.runtime_dir / "Caddyfile.candidate"
        atomic_write_text(candidate, self._caddy_text(slot))
        self._compose(
            "exec",
            "-T",
            "caddy",
            "caddy",
            "validate",
            "--config",
            "/etc/caddy/runtime/Caddyfile.candidate",
            "--adapter",
            "caddyfile",
        )
        os.replace(candidate, self.runtime_dir / "Caddyfile")
        self._compose(
            "exec",
            "-T",
            "caddy",
            "caddy",
            "reload",
            "--config",
            "/etc/caddy/runtime/Caddyfile",
            "--adapter",
            "caddyfile",
        )

    def _service_running(self, service: str) -> bool:
        result = self._compose(
            "ps",
            "--status",
            "running",
            "-q",
            service,
            check=False,
        )
        return result.returncode == 0 and bool(result.stdout.strip())

    def _wait_service_running(self, service: str, timeout: int) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self._service_running(service):
                return
            time.sleep(1)
        raise ShipSafeError(f"service did not remain running: {service}")

    def _wait_service_healthy(self, service: str, timeout: int) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            container = self._compose(
                "ps", "-q", service, check=False
            ).stdout.strip()
            if container:
                health = self.runner.run(
                    [
                        "docker",
                        "inspect",
                        "--format",
                        "{{.State.Health.Status}}",
                        container,
                    ],
                    check=False,
                )
                if health.returncode == 0 and health.stdout.strip() == "healthy":
                    return
            time.sleep(1)
        raise ShipSafeError(f"service did not become healthy: {service}")

    def _probe_candidate(self, slot: str, path: str) -> None:
        script = (
            "import sys,urllib.request; "
            "response=urllib.request.urlopen(" 
            "'http://127.0.0.1:8000'+sys.argv[1],timeout=3); "
            "raise SystemExit(0 if response.status==200 else 1)"
        )
        self._compose(
            "exec",
            "-T",
            f"api-{slot}",
            "python",
            "-c",
            script,
            path,
            timeout=8,
        )

    def _wait_candidate(self, slot: str, timeout: int) -> None:
        deadline = time.monotonic() + timeout
        last_error: Exception | None = None
        while time.monotonic() < deadline:
            if not self._service_running(f"api-{slot}"):
                time.sleep(1)
                continue
            try:
                self._probe_candidate(slot, "/health")
                self._probe_candidate(slot, "/ready")
                return
            except (ShipSafeError, CommandFailed) as error:
                last_error = error
                time.sleep(1)
        raise ShipSafeError(
            f"candidate did not become ready: {sanitize_reason(last_error)}"
        )

    def _candidate_auth_smoke(self, slot: str, *, invalid: bool = False) -> None:
        key = self.runtime_values["EVENTLEDGER_API_KEY"]
        if invalid:
            key = f"invalid-{uuid4().hex}"
        expected_source = self.runtime_values["EVENTLEDGER_SOURCE_ID"]
        payload = json.dumps({"key": key, "source": expected_source})
        script = (
            "import json,sys,urllib.request; "
            "data=json.loads(sys.stdin.read()); "
            "request=urllib.request.Request(" 
            "'http://127.0.0.1:8000/v1/auth/check'," 
            "headers={'X-API-Key':data['key']}); "
            "response=urllib.request.urlopen(request,timeout=3); "
            "body=json.loads(response.read()); "
            "raise SystemExit(0 if response.status==200 and "
            "body.get('source_id')==data['source'] else 1)"
        )
        self._compose(
            "exec",
            "-T",
            f"api-{slot}",
            "python",
            "-c",
            script,
            input_text=payload,
            timeout=8,
        )

    def _proxy_request(
        self,
        path: str,
        *,
        method: str = "GET",
        body: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> tuple[int, dict[str, Any]]:
        encoded = None
        request_headers = dict(headers or {})
        if body is not None:
            encoded = json.dumps(body).encode("utf-8")
            request_headers["Content-Type"] = "application/json"
        request = urllib.request.Request(
            f"{self.proxy_url}{path}",
            data=encoded,
            headers=request_headers,
            method=method,
        )
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                response_body = json.loads(response.read() or b"{}")
                return response.status, response_body
        except urllib.error.HTTPError as error:
            try:
                response_body = json.loads(error.read() or b"{}")
            except json.JSONDecodeError:
                response_body = {}
            return error.code, response_body

    def _proxy_smoke(self) -> None:
        status, _ = self._proxy_request("/ready")
        if status != 200:
            raise ShipSafeError("proxy readiness check failed")
        status, body = self._proxy_request(
            "/v1/auth/check",
            headers={
                "X-API-Key": self.runtime_values["EVENTLEDGER_API_KEY"]
            },
        )
        if (
            status != 200
            or body.get("source_id")
            != self.runtime_values["EVENTLEDGER_SOURCE_ID"]
        ):
            raise ShipSafeError("proxy authenticated smoke check failed")

    def _observe_proxy(self, seconds: int) -> None:
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            self._proxy_smoke()
            time.sleep(min(2, max(0.1, deadline - time.monotonic())))

    def _wait_proxy(self, timeout: int) -> None:
        deadline = time.monotonic() + timeout
        last_error: Exception | None = None
        while time.monotonic() < deadline:
            try:
                self._proxy_smoke()
                return
            except (OSError, ShipSafeError) as error:
                last_error = error
                time.sleep(1)
        raise ShipSafeError(
            f"proxy did not become ready: {sanitize_reason(last_error)}"
        )

    def _fault_override(self, slot: str, failure_mode: str) -> tuple[Path, ...]:
        if failure_mode == "none":
            return ()
        if os.getenv("SHIPSAFE_ENABLE_FAILURE_INJECTION") != "1":
            raise ShipSafeError(
                "failure injection requires SHIPSAFE_ENABLE_FAILURE_INJECTION=1"
            )
        if failure_mode in {
            "candidate-smoke",
            "post-promotion",
            "interrupt-after-switch",
        }:
            return ()
        override = self.runtime_dir / "failure-override.yaml"
        if failure_mode == "candidate-startup":
            content = (
                "services:\n"
                f"  api-{slot}:\n"
                "    command: [\"python\", \"-c\", "
                "\"raise SystemExit(42)\"]\n"
            )
        elif failure_mode == "candidate-readiness":
            content = (
                "services:\n"
                f"  api-{slot}:\n"
                "    environment:\n"
                "      POSTGRES_HOST: shipsafe-invalid-database\n"
            )
        elif failure_mode == "worker-startup":
            content = (
                "services:\n"
                "  worker:\n"
                "    command: [\"python\", \"-c\", "
                "\"raise SystemExit(43)\"]\n"
            )
        else:
            raise ShipSafeError(f"unsupported failure mode: {failure_mode}")
        atomic_write_text(override, content)
        return (override,)

    def _verify_worker_with_synthetic_event(self, timeout: int) -> str:
        probe_id = uuid4().hex
        order_id = f"SHIPSAFE-PROBE-{probe_id}"
        status, body = self._proxy_request(
            "/v1/events",
            method="POST",
            body={
                "event_type": "order.created",
                "payload": {
                    "order_id": order_id,
                    "shipsafe_probe": True,
                },
            },
            headers={
                "X-API-Key": self.runtime_values["EVENTLEDGER_API_KEY"],
                "Idempotency-Key": f"shipsafe-worker-{probe_id}",
            },
        )
        if status not in {200, 201} or "id" not in body:
            raise ShipSafeError("unable to create synthetic worker probe")
        event_id = str(body["id"])
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            status, detail = self._proxy_request(
                f"/v1/events/{event_id}",
                headers={
                    "X-API-Key": self.runtime_values["EVENTLEDGER_API_KEY"]
                },
            )
            if status == 200 and detail.get("status") == "completed":
                return event_id
            if status == 200 and detail.get("status") == "failed":
                raise ShipSafeError("synthetic worker probe failed")
            time.sleep(1)
        raise ShipSafeError("synthetic worker probe timed out")

    def _record_history(
        self,
        state: dict[str, Any],
        *,
        operation: str,
        release_id: str,
        outcome: str,
        reason: str | None = None,
    ) -> None:
        state["history"].append(
            {
                "at": utc_now(),
                "operation": operation,
                "release_id": release_id,
                "outcome": outcome,
                "reason": reason,
            }
        )
        state["history"] = state["history"][-100:]

    def bootstrap(
        self,
        *,
        image: str,
        git_sha: str,
        ready_timeout: int,
        worker_timeout: int,
    ) -> None:
        with HostLock(self.runtime_dir / "deploy.lock"):
            state = self.store.load(required=False)
            if state.get("active_release"):
                raise ShipSafeError("ShipSafe is already bootstrapped")
            manifest = self._register_release(image, git_sha)
            release_id = manifest["release_id"]
            state["slot_releases"] = {"blue": release_id, "green": release_id}
            state["active_release"] = release_id
            state["active_slot"] = "blue"
            state["worker_release"] = release_id
            state["deployment"] = {
                "operation": "bootstrap",
                "release_id": release_id,
                "phase": "starting_database",
                "started_at": utc_now(),
            }
            self.store.save(state)
            self._write_compose_env(state)
            self._write_initial_caddy("blue")
            try:
                self._compose("config", "--quiet")
                self._compose("up", "-d", "db")
                self._wait_service_healthy("db", ready_timeout)
                self._compose("run", "--rm", "--no-deps", "migrate")
                self._compose(
                    "up", "-d", "--no-deps", "--force-recreate", "api-blue"
                )
                self._wait_candidate("blue", ready_timeout)
                self._candidate_auth_smoke("blue")
                self._compose("up", "-d", "--no-deps", "caddy")
                self._wait_proxy(ready_timeout)
                self._compose(
                    "up", "-d", "--no-deps", "--force-recreate", "worker"
                )
                self._wait_service_running("worker", ready_timeout)
                probe_event = self._verify_worker_with_synthetic_event(
                    worker_timeout
                )
            except Exception as error:
                reason = sanitize_reason(error)
                manifest["status"] = "bootstrap_failed"
                manifest["failure_reason"] = reason
                self.store.save_release(manifest)
                state["deployment"]["phase"] = "failed"
                state["deployment"]["failure_reason"] = reason
                self.store.save(state)
                raise

            manifest["candidate_verified"] = True
            manifest["worker_verified"] = True
            manifest["status"] = "active"
            manifest["verified_at"] = utc_now()
            self.store.save_release(manifest)
            state["deployment"] = None
            self._record_history(
                state,
                operation="bootstrap",
                release_id=release_id,
                outcome="succeeded",
            )
            state["last_worker_probe_event"] = probe_event
            self.store.save(state)
            print(f"bootstrapped release={release_id} slot=blue")

    def _mark_failed(
        self,
        state: dict[str, Any],
        manifest: dict[str, Any],
        operation: str,
        reason: str,
    ) -> None:
        manifest["status"] = "failed"
        manifest["failure_reason"] = reason
        manifest["failed_at"] = utc_now()
        self.store.save_release(manifest)
        self._record_history(
            state,
            operation=operation,
            release_id=manifest["release_id"],
            outcome="failed",
            reason=reason,
        )
        state["deployment"] = None
        self.store.save(state)

    def _deploy_manifest(
        self,
        *,
        state: dict[str, Any],
        manifest: dict[str, Any],
        operation: str,
        failure_mode: str,
        ready_timeout: int,
        observation_seconds: int,
        worker_timeout: int,
    ) -> None:
        release_id = manifest["release_id"]
        if state["active_release"] == release_id:
            print(f"release already active: {release_id}")
            return
        if state.get("deployment"):
            raise ShipSafeError(
                "an interrupted deployment exists; run recover before deploying"
            )
        self._assert_release_image(manifest)
        manifest["status"] = "deploying"
        manifest["failure_reason"] = None
        self.store.save_release(manifest)
        old_slot = state["active_slot"]
        old_release = state["active_release"]
        old_worker = state["worker_release"]
        candidate_slot = "green" if old_slot == "blue" else "blue"
        state["slot_releases"][candidate_slot] = release_id
        state["deployment"] = {
            "operation": operation,
            "release_id": release_id,
            "previous_release": old_release,
            "previous_worker_release": old_worker,
            "previous_slot": old_slot,
            "candidate_slot": candidate_slot,
            "phase": "candidate_starting",
            "started_at": utc_now(),
        }
        self.store.save(state)
        self._write_compose_env(state)

        candidate_overrides = self._fault_override(candidate_slot, failure_mode)
        try:
            self._compose(
                "up",
                "-d",
                "--no-deps",
                "--force-recreate",
                f"api-{candidate_slot}",
                extra_files=candidate_overrides,
            )
            self._wait_candidate(candidate_slot, ready_timeout)
            self._candidate_auth_smoke(
                candidate_slot,
                invalid=failure_mode == "candidate-smoke",
            )
        except Exception as error:
            self._compose(
                "stop", "-t", "10", f"api-{candidate_slot}", check=False
            )
            state["slot_releases"][candidate_slot] = None
            self._write_compose_env(state)
            self._mark_failed(
                state,
                manifest,
                operation,
                sanitize_reason(error),
            )
            raise

        manifest["candidate_verified"] = True
        manifest["candidate_verified_at"] = utc_now()
        self.store.save_release(manifest)
        state["deployment"]["phase"] = "candidate_verified"
        self.store.save(state)

        try:
            self._switch_caddy(candidate_slot)
            state["deployment"]["phase"] = "proxy_switched"
            self.store.save(state)
            if failure_mode == "interrupt-after-switch":
                os._exit(75)
            if failure_mode == "post-promotion":
                self._compose(
                    "stop", "-t", "10", f"api-{candidate_slot}"
                )
            self._observe_proxy(observation_seconds)
        except Exception as error:
            self._switch_caddy(old_slot)
            self._proxy_smoke()
            self._compose(
                "stop", "-t", "10", f"api-{candidate_slot}", check=False
            )
            state["slot_releases"][candidate_slot] = None
            self._write_compose_env(state)
            self._mark_failed(
                state,
                manifest,
                operation,
                sanitize_reason(error),
            )
            raise

        state["deployment"]["phase"] = "worker_stopping"
        self.store.save(state)
        try:
            self._compose("stop", "-t", "35", "worker")
            state["worker_release"] = release_id
            self._write_compose_env(state)
            state["deployment"]["phase"] = "worker_starting"
            self.store.save(state)
            worker_overrides = self._fault_override(
                candidate_slot, failure_mode
            )
            self._compose(
                "up",
                "-d",
                "--no-deps",
                "--force-recreate",
                "worker",
                extra_files=worker_overrides,
            )
            self._wait_service_running("worker", ready_timeout)
            time.sleep(2)
            if not self._service_running("worker"):
                raise ShipSafeError("candidate worker exited during startup")
            probe_event = self._verify_worker_with_synthetic_event(
                worker_timeout
            )
        except Exception as error:
            self._compose("stop", "-t", "10", "worker", check=False)
            state["worker_release"] = old_worker
            self._write_compose_env(state)
            self._compose(
                "up", "-d", "--no-deps", "--force-recreate", "worker"
            )
            self._wait_service_running("worker", ready_timeout)
            self._switch_caddy(old_slot)
            self._proxy_smoke()
            self._compose(
                "stop", "-t", "10", f"api-{candidate_slot}", check=False
            )
            state["slot_releases"][candidate_slot] = None
            self._write_compose_env(state)
            self._mark_failed(
                state,
                manifest,
                operation,
                sanitize_reason(error),
            )
            raise

        previous_manifest = self.store.load_release(old_release)
        previous_manifest["status"] = "verified_previous"
        self.store.save_release(previous_manifest)
        manifest["worker_verified"] = True
        manifest["worker_verified_at"] = utc_now()
        manifest["status"] = "active"
        manifest["failure_reason"] = None
        manifest["activated_at"] = utc_now()
        self.store.save_release(manifest)
        state["previous_release"] = old_release
        state["active_release"] = release_id
        state["active_slot"] = candidate_slot
        state["worker_release"] = release_id
        state["deployment"] = None
        state["last_worker_probe_event"] = probe_event
        self._record_history(
            state,
            operation=operation,
            release_id=release_id,
            outcome="succeeded",
        )
        self.store.save(state)
        self._write_compose_env(state)
        print(f"{operation} succeeded release={release_id} slot={candidate_slot}")

    def deploy(
        self,
        *,
        image: str,
        git_sha: str,
        failure_mode: str,
        ready_timeout: int,
        observation_seconds: int,
        worker_timeout: int,
    ) -> None:
        with HostLock(self.runtime_dir / "deploy.lock"):
            state = self.store.load()
            manifest = self._register_release(image, git_sha)
            self._deploy_manifest(
                state=state,
                manifest=manifest,
                operation="deploy",
                failure_mode=failure_mode,
                ready_timeout=ready_timeout,
                observation_seconds=observation_seconds,
                worker_timeout=worker_timeout,
            )

    def rollback(
        self,
        *,
        release_id: str | None,
        ready_timeout: int,
        observation_seconds: int,
        worker_timeout: int,
    ) -> None:
        with HostLock(self.runtime_dir / "deploy.lock"):
            state = self.store.load()
            target = release_id or state.get("previous_release")
            if not target:
                raise ShipSafeError("no previous release is recorded")
            manifest = self.store.load_release(target)
            if not (
                manifest.get("candidate_verified")
                and manifest.get("worker_verified")
            ):
                raise ShipSafeError("rollback target is not a verified release")
            if manifest.get("legacy_revision") != LEGACY_REVISION:
                raise ShipSafeError("rollback target schema contract is incompatible")
            self._deploy_manifest(
                state=state,
                manifest=manifest,
                operation="rollback",
                failure_mode="none",
                ready_timeout=ready_timeout,
                observation_seconds=observation_seconds,
                worker_timeout=worker_timeout,
            )

    def recover(self, *, ready_timeout: int) -> None:
        with HostLock(self.runtime_dir / "deploy.lock"):
            state = self.store.load()
            deployment = state.get("deployment")
            if not deployment:
                print("no interrupted deployment to recover")
                return
            if deployment.get("operation") == "bootstrap":
                for service in ("worker", "api-green", "api-blue", "caddy"):
                    self._compose(
                        "stop", "-t", "10", service, check=False
                    )
                reset = self.store.load(required=False)
                reset.update(
                    {
                        "active_slot": None,
                        "active_release": None,
                        "previous_release": None,
                        "worker_release": None,
                        "slot_releases": {"blue": None, "green": None},
                        "deployment": None,
                    }
                )
                self._record_history(
                    reset,
                    operation="recover",
                    release_id=deployment["release_id"],
                    outcome="recovered",
                    reason="reset interrupted bootstrap; database preserved",
                )
                interrupted_manifest = self.store.load_release(
                    deployment["release_id"]
                )
                interrupted_manifest["status"] = "interrupted_recovered"
                interrupted_manifest["failure_reason"] = (
                    "interrupted bootstrap reset; database preserved"
                )
                self.store.save_release(interrupted_manifest)
                self.store.save(reset)
                print("recovered interrupted bootstrap; database volume preserved")
                return

            previous_slot = deployment.get("previous_slot")
            previous_release = deployment.get("previous_release")
            previous_worker = deployment.get("previous_worker_release")
            candidate_slot = deployment.get("candidate_slot")
            if previous_slot not in VALID_SLOTS or not previous_release:
                raise ShipSafeError(
                    "bootstrap interruption requires operator inspection"
                )
            if deployment.get("phase") in {
                "proxy_switched",
                "worker_stopping",
                "worker_starting",
            }:
                self._switch_caddy(previous_slot)
                self._proxy_smoke()
            if previous_worker:
                self._compose("stop", "-t", "10", "worker", check=False)
                state["worker_release"] = previous_worker
                self._write_compose_env(state)
                self._compose(
                    "up", "-d", "--no-deps", "--force-recreate", "worker"
                )
                self._wait_service_running("worker", ready_timeout)
            if candidate_slot in VALID_SLOTS:
                self._compose(
                    "stop", "-t", "10", f"api-{candidate_slot}", check=False
                )
                state["slot_releases"][candidate_slot] = None
            state["active_slot"] = previous_slot
            state["active_release"] = previous_release
            state["deployment"] = None
            interrupted_manifest = self.store.load_release(
                deployment["release_id"]
            )
            interrupted_manifest["status"] = "interrupted_recovered"
            interrupted_manifest["failure_reason"] = (
                f"recovered interrupted phase {deployment['phase']}"
            )
            self.store.save_release(interrupted_manifest)
            self._record_history(
                state,
                operation="recover",
                release_id=deployment["release_id"],
                outcome="recovered",
                reason=f"recovered interrupted phase {deployment['phase']}",
            )
            self.store.save(state)
            self._write_compose_env(state)
            print(f"recovered active release={previous_release}")

    def status(self) -> None:
        state = self.store.load(required=False)
        print(json.dumps(state, indent=2, sort_keys=True))


def positive_int(value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be greater than zero")
    return parsed


def add_timing_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--ready-timeout",
        type=positive_int,
        default=DEFAULT_READY_TIMEOUT,
    )
    parser.add_argument(
        "--observation-seconds",
        type=positive_int,
        default=DEFAULT_OBSERVATION_SECONDS,
    )
    parser.add_argument("--worker-timeout", type=positive_int, default=60)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--root",
        type=Path,
        default=Path(__file__).resolve().parents[1],
    )
    parser.add_argument(
        "--runtime-dir",
        type=Path,
        default=Path(__file__).resolve().parent / "runtime",
    )
    parser.add_argument("--runtime-env", type=Path, required=True)
    parser.add_argument("--project-name", default=DEFAULT_PROJECT)
    subparsers = parser.add_subparsers(dest="command", required=True)

    bootstrap = subparsers.add_parser("bootstrap")
    bootstrap.add_argument("--image", required=True)
    bootstrap.add_argument("--git-sha", required=True)
    bootstrap.add_argument(
        "--ready-timeout", type=positive_int, default=DEFAULT_READY_TIMEOUT
    )
    bootstrap.add_argument("--worker-timeout", type=positive_int, default=60)

    deploy = subparsers.add_parser("deploy")
    deploy.add_argument("--image", required=True)
    deploy.add_argument("--git-sha", required=True)
    deploy.add_argument(
        "--failure-mode",
        choices=(
            "none",
            "candidate-startup",
            "candidate-readiness",
            "candidate-smoke",
            "post-promotion",
            "worker-startup",
            "interrupt-after-switch",
        ),
        default="none",
    )
    add_timing_options(deploy)

    rollback = subparsers.add_parser("rollback")
    rollback.add_argument("--release-id")
    add_timing_options(rollback)

    recover = subparsers.add_parser("recover")
    recover.add_argument(
        "--ready-timeout", type=positive_int, default=DEFAULT_READY_TIMEOUT
    )

    subparsers.add_parser("status")
    return parser


def main() -> int:
    arguments = build_parser().parse_args()
    try:
        controller = Controller(
            root=arguments.root,
            runtime_dir=arguments.runtime_dir,
            runtime_env=arguments.runtime_env,
            project_name=arguments.project_name,
        )
        if arguments.command == "bootstrap":
            controller.bootstrap(
                image=arguments.image,
                git_sha=arguments.git_sha,
                ready_timeout=arguments.ready_timeout,
                worker_timeout=arguments.worker_timeout,
            )
        elif arguments.command == "deploy":
            controller.deploy(
                image=arguments.image,
                git_sha=arguments.git_sha,
                failure_mode=arguments.failure_mode,
                ready_timeout=arguments.ready_timeout,
                observation_seconds=arguments.observation_seconds,
                worker_timeout=arguments.worker_timeout,
            )
        elif arguments.command == "rollback":
            controller.rollback(
                release_id=arguments.release_id,
                ready_timeout=arguments.ready_timeout,
                observation_seconds=arguments.observation_seconds,
                worker_timeout=arguments.worker_timeout,
            )
        elif arguments.command == "recover":
            controller.recover(ready_timeout=arguments.ready_timeout)
        else:
            controller.status()
    except (ShipSafeError, OSError, ValueError, json.JSONDecodeError) as error:
        print(f"ShipSafe error: {sanitize_reason(error)}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
