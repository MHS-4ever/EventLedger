import json
import shutil
from pathlib import Path
from uuid import uuid4

import pytest

from shipsafe.controller import (
    CommandResult,
    Controller,
    DeploymentLocked,
    HostLock,
    Runner,
    ShipSafeError,
    StateStore,
)


IMAGE_ID_A = "sha256:" + "a" * 64
IMAGE_ID_B = "sha256:" + "b" * 64
GIT_SHA_A = "1" * 40
GIT_SHA_B = "2" * 40


@pytest.fixture
def test_dir() -> Path:
    path = Path("shipsafe/runtime/unit-tests") / uuid4().hex
    path.mkdir(parents=True)
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


class ImageRunner(Runner):
    def __init__(self, image_id: str = IMAGE_ID_A) -> None:
        self.image_id = image_id
        self.commands: list[tuple[str, ...]] = []

    def run(self, command, **kwargs):
        del kwargs
        self.commands.append(tuple(command))
        if command[:3] == ["docker", "image", "inspect"]:
            return CommandResult(self.image_id, "", 0)
        return CommandResult("", "", 0)


def runtime_env(tmp_path: Path) -> Path:
    path = tmp_path / "runtime.env"
    path.write_text(
        "POSTGRES_DB=eventledger_shipsafe\n"
        "POSTGRES_USER=eventledger_shipsafe\n"
        "POSTGRES_PASSWORD=test-password-not-a-secret\n"
        "EVENTLEDGER_API_KEY=test-api-key-not-a-secret\n"
        "EVENTLEDGER_SOURCE_ID=shipsafe-test\n",
        encoding="utf-8",
    )
    return path


def make_controller(
    tmp_path: Path,
    *,
    runner: Runner | None = None,
) -> Controller:
    return Controller(
        root=Path.cwd(),
        runtime_dir=tmp_path / "state",
        runtime_env=runtime_env(tmp_path),
        runner=runner,
    )


def release_manifest(
    release_id: str,
    image_id: str,
    git_sha: str,
) -> dict:
    return {
        "manifest_version": 1,
        "release_id": release_id,
        "git_sha": git_sha,
        "image_id": image_id,
        "image_ref": f"eventledger:shipsafe-{release_id}",
        "created_at": "2026-11-05T00:00:00Z",
        "schema_mode": "legacy",
        "legacy_revision": "d4e5f6a7b8c9",
        "candidate_verified": True,
        "worker_verified": True,
        "status": "verified_previous",
        "failure_reason": None,
    }


def initial_state(old_release: str) -> dict:
    return {
        "version": 1,
        "active_slot": "blue",
        "active_release": old_release,
        "previous_release": None,
        "worker_release": old_release,
        "slot_releases": {"blue": old_release, "green": None},
        "deployment": None,
        "history": [],
    }


class HarnessController(Controller):
    def __init__(self, *args, failure_at: str | None = None, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.failure_at = failure_at
        self.actions: list[str] = []

    def _assert_release_image(self, manifest):
        self.actions.append(f"assert:{manifest['release_id']}")

    def _write_compose_env(self, state):
        self.actions.append(f"env:{state.get('worker_release')}")

    def _fault_override(self, slot, failure_mode):
        self.actions.append(f"fault:{failure_mode}:{slot}")
        return ()

    def _compose(self, *arguments, **kwargs):
        del kwargs
        action = "compose:" + ":".join(arguments)
        self.actions.append(action)
        return CommandResult("", "", 0)

    def _wait_candidate(self, slot, timeout):
        del timeout
        self.actions.append(f"candidate-ready:{slot}")
        if self.failure_at == "candidate":
            raise ShipSafeError("candidate rejected")

    def _candidate_auth_smoke(self, slot, *, invalid=False):
        self.actions.append(f"candidate-smoke:{slot}:{invalid}")

    def _switch_caddy(self, slot):
        self.actions.append(f"switch:{slot}")

    def _proxy_smoke(self):
        self.actions.append("proxy-smoke")

    def _observe_proxy(self, seconds):
        del seconds
        self.actions.append("observe")
        if self.failure_at == "observation":
            raise ShipSafeError("observation failed")

    def _wait_service_running(self, service, timeout):
        del timeout
        self.actions.append(f"running:{service}")

    def _service_running(self, service):
        self.actions.append(f"still-running:{service}")
        return True

    def _verify_worker_with_synthetic_event(self, timeout):
        del timeout
        self.actions.append("worker-probe")
        if self.failure_at == "worker":
            raise ShipSafeError("worker rejected")
        return "probe-event-id"


def harness(tmp_path: Path, failure_at: str | None = None):
    controller = HarnessController(
        root=Path.cwd(),
        runtime_dir=tmp_path / "state",
        runtime_env=runtime_env(tmp_path),
        runner=ImageRunner(),
        failure_at=failure_at,
    )
    old_id = "old-release"
    new_id = "new-release"
    old = release_manifest(old_id, IMAGE_ID_A, GIT_SHA_A)
    new = release_manifest(new_id, IMAGE_ID_B, GIT_SHA_B)
    controller.store.save_release(old)
    controller.store.save_release(new)
    state = initial_state(old_id)
    controller.store.save(state)
    return controller, state, new


def test_state_store_uses_versioned_json(test_dir: Path) -> None:
    store = StateStore(test_dir)
    state = store.load(required=False)
    state["history"].append({"outcome": "test"})
    store.save(state)

    assert store.load()["version"] == 1
    assert store.load()["history"] == [{"outcome": "test"}]
    assert not list(test_dir.glob(".state.json.*"))


def test_host_lock_rejects_concurrent_command(test_dir: Path) -> None:
    lock_path = test_dir / "deploy.lock"

    with HostLock(lock_path):
        with pytest.raises(DeploymentLocked):
            with HostLock(lock_path):
                pass


def test_release_manifest_records_git_sha_and_verified_image_id(
    test_dir: Path,
) -> None:
    runner = ImageRunner()
    controller = make_controller(test_dir, runner=runner)

    manifest = controller._register_release("eventledger:candidate", GIT_SHA_A)

    assert manifest["git_sha"] == GIT_SHA_A
    assert manifest["image_id"] == IMAGE_ID_A
    assert manifest["release_id"].startswith(GIT_SHA_A[:12])
    assert controller.store.load_release(manifest["release_id"]) == manifest
    assert any(command[:3] == ("docker", "image", "tag") for command in runner.commands)


def test_compose_state_file_contains_no_runtime_secrets(test_dir: Path) -> None:
    controller = make_controller(test_dir, runner=ImageRunner())
    manifest = controller._register_release("eventledger:candidate", GIT_SHA_A)
    state = initial_state(manifest["release_id"])
    state["slot_releases"]["green"] = manifest["release_id"]
    controller._write_compose_env(state)

    content = controller._compose_env_path().read_text(encoding="utf-8")
    assert "test-password-not-a-secret" not in content
    assert "test-api-key-not-a-secret" not in content
    assert manifest["image_ref"] in content


def test_caddy_routes_only_to_named_internal_slot(test_dir: Path) -> None:
    controller = make_controller(test_dir, runner=ImageRunner())

    assert "reverse_proxy api-green:8000" in controller._caddy_text("green")
    assert "admin localhost:2019" in controller._caddy_text("green")
    with pytest.raises(ShipSafeError):
        controller._caddy_text("attacker.example")


def test_healthy_deploy_preserves_old_api_until_candidate_verified(
    test_dir: Path,
) -> None:
    controller, state, manifest = harness(test_dir)

    controller._deploy_manifest(
        state=state,
        manifest=manifest,
        operation="deploy",
        failure_mode="none",
        ready_timeout=1,
        observation_seconds=1,
        worker_timeout=1,
    )

    saved = controller.store.load()
    assert saved["active_slot"] == "green"
    assert saved["active_release"] == "new-release"
    assert saved["previous_release"] == "old-release"
    candidate_verified = controller.actions.index("candidate-smoke:green:False")
    old_api_stops = [
        index
        for index, action in enumerate(controller.actions)
        if "api-blue" in action and "stop" in action
    ]
    assert not old_api_stops or min(old_api_stops) > candidate_verified


def test_candidate_failure_does_not_switch_traffic(test_dir: Path) -> None:
    controller, state, manifest = harness(test_dir, failure_at="candidate")

    with pytest.raises(ShipSafeError, match="candidate rejected"):
        controller._deploy_manifest(
            state=state,
            manifest=manifest,
            operation="deploy",
            failure_mode="none",
            ready_timeout=1,
            observation_seconds=1,
            worker_timeout=1,
        )

    saved = controller.store.load()
    assert saved["active_release"] == "old-release"
    assert not any(action.startswith("switch:") for action in controller.actions)


def test_post_promotion_failure_reverses_proxy(test_dir: Path) -> None:
    controller, state, manifest = harness(test_dir, failure_at="observation")

    with pytest.raises(ShipSafeError, match="observation failed"):
        controller._deploy_manifest(
            state=state,
            manifest=manifest,
            operation="deploy",
            failure_mode="none",
            ready_timeout=1,
            observation_seconds=1,
            worker_timeout=1,
        )

    assert [a for a in controller.actions if a.startswith("switch:")] == [
        "switch:green",
        "switch:blue",
    ]
    assert controller.store.load()["active_release"] == "old-release"


def test_worker_failure_restores_worker_and_proxy(test_dir: Path) -> None:
    controller, state, manifest = harness(test_dir, failure_at="worker")

    with pytest.raises(ShipSafeError, match="worker rejected"):
        controller._deploy_manifest(
            state=state,
            manifest=manifest,
            operation="deploy",
            failure_mode="none",
            ready_timeout=1,
            observation_seconds=1,
            worker_timeout=1,
        )

    saved = controller.store.load()
    assert saved["active_release"] == "old-release"
    assert saved["worker_release"] == "old-release"
    assert controller.actions.count("switch:blue") == 1


def test_repeated_active_release_is_idempotent(test_dir: Path) -> None:
    controller, state, _ = harness(test_dir)
    active = controller.store.load_release("old-release")

    controller._deploy_manifest(
        state=state,
        manifest=active,
        operation="deploy",
        failure_mode="none",
        ready_timeout=1,
        observation_seconds=1,
        worker_timeout=1,
    )

    assert controller.actions == []


def test_failure_injection_requires_explicit_opt_in(
    test_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("SHIPSAFE_ENABLE_FAILURE_INJECTION", raising=False)
    controller = make_controller(test_dir, runner=ImageRunner())

    with pytest.raises(ShipSafeError, match="requires"):
        controller._fault_override("green", "candidate-readiness")


def test_manifest_json_is_non_secret(test_dir: Path) -> None:
    controller = make_controller(test_dir, runner=ImageRunner())
    manifest = controller._register_release("eventledger:candidate", GIT_SHA_A)

    serialized = json.dumps(manifest)
    assert "test-password-not-a-secret" not in serialized
    assert "test-api-key-not-a-secret" not in serialized
