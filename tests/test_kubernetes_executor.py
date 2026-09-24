import uuid
from unittest.mock import Mock

import pytest
from kubernetes.client import ApiException

from stablehand.config import get_settings
from stablehand.executors import kubernetes
from stablehand.executors.kubernetes import KubernetesExecutor, _resource_names
from stablehand.models import Execution, ExecutorKind, Run, RunState
from tests.conftest import make_stack


def test_resource_names_are_unique_per_execution():
    run_id = uuid.uuid4()
    first_job, first_secret = _resource_names(run_id, uuid.uuid4(), "apply")
    second_job, second_secret = _resource_names(run_id, uuid.uuid4(), "apply")

    assert first_job != second_job
    assert first_secret != second_secret
    assert len(first_job) <= 63
    assert len(first_secret) <= 63


def test_start_cleans_up_resources_when_job_creation_fails(monkeypatch):
    core = Mock()
    batch = Mock()
    batch.create_namespaced_job.side_effect = RuntimeError("job failed")
    monkeypatch.setattr(kubernetes, "_load_config", lambda: None)
    monkeypatch.setattr(kubernetes.client, "CoreV1Api", lambda: core)
    monkeypatch.setattr(kubernetes.client, "BatchV1Api", lambda: batch)
    stack_id = uuid.uuid4()
    run = Run(
        id=uuid.uuid4(),
        stack_id=stack_id,
        commit_sha="abc123",
        trigger="manual",
        state=RunState.apply_running.value,
    )
    stack = make_stack(
        id=stack_id,
        git_url="https://example.test/repo.git",
        executor=ExecutorKind.kubernetes.value,
    )

    with pytest.raises(RuntimeError, match="job failed"):
        KubernetesExecutor().start(
            run,
            stack,
            "apply",
            "shr_token",
            execution_id=uuid.uuid4(),
        )

    batch.delete_namespaced_job.assert_called_once()
    core.delete_namespaced_secret.assert_called_once()


def test_runner_job_uses_the_published_image(monkeypatch):
    core = Mock()
    batch = Mock()
    monkeypatch.setattr(kubernetes, "_load_config", lambda: None)
    monkeypatch.setattr(kubernetes.client, "CoreV1Api", lambda: core)
    monkeypatch.setattr(kubernetes.client, "BatchV1Api", lambda: batch)
    stack_id = uuid.uuid4()
    run = Run(
        id=uuid.uuid4(),
        stack_id=stack_id,
        commit_sha="abc123",
        trigger="manual",
        state=RunState.apply_running.value,
    )
    stack = make_stack(
        id=stack_id,
        git_url="https://example.test/repo.git",
        executor=ExecutorKind.kubernetes.value,
    )

    KubernetesExecutor().start(run, stack, "check", "shr_token", execution_id=uuid.uuid4())

    job = batch.create_namespaced_job.call_args.args[1]
    container = job.spec.template.spec.containers[0]
    assert container.image == get_settings().runner_image
    assert container.image_pull_policy == "Always"


def test_cleanup_removes_job_and_secret(monkeypatch):
    core = Mock()
    batch = Mock()
    monkeypatch.setattr(kubernetes, "_load_config", lambda: None)
    monkeypatch.setattr(kubernetes.client, "CoreV1Api", lambda: core)
    monkeypatch.setattr(kubernetes.client, "BatchV1Api", lambda: batch)

    KubernetesExecutor().cleanup("job-name", "secret-name")

    batch.delete_namespaced_job.assert_called_once_with(
        name="job-name",
        namespace="stablehand-runners",
        propagation_policy="Background",
    )
    core.delete_namespaced_secret.assert_called_once_with(
        name="secret-name",
        namespace="stablehand-runners",
    )


def test_recover_finds_job_by_execution_id(monkeypatch):
    batch = Mock()
    monkeypatch.setattr(kubernetes, "_load_config", lambda: None)
    monkeypatch.setattr(kubernetes.client, "BatchV1Api", lambda: batch)
    run_id = uuid.uuid4()
    execution_id = uuid.uuid4()
    run = Run(id=run_id)
    execution = Execution(id=execution_id, phase="apply")
    stack = make_stack(id=uuid.uuid4())
    job_name, secret_name = _resource_names(run_id, execution_id, "apply")

    recovered = KubernetesExecutor().recover(execution, run, stack)

    assert recovered == (job_name, secret_name)
    batch.read_namespaced_job.assert_called_once_with(job_name, "stablehand-runners")


def test_recover_missing_job_removes_orphaned_secret(monkeypatch):
    core = Mock()
    batch = Mock()
    batch.read_namespaced_job.side_effect = ApiException(status=404)
    monkeypatch.setattr(kubernetes, "_load_config", lambda: None)
    monkeypatch.setattr(kubernetes.client, "CoreV1Api", lambda: core)
    monkeypatch.setattr(kubernetes.client, "BatchV1Api", lambda: batch)
    run_id = uuid.uuid4()
    execution_id = uuid.uuid4()
    run = Run(id=run_id)
    execution = Execution(id=execution_id, phase="check")
    stack = make_stack(id=uuid.uuid4())
    _, secret_name = _resource_names(run_id, execution_id, "check")

    assert KubernetesExecutor().recover(execution, run, stack) is None
    core.delete_namespaced_secret.assert_called_once_with(
        name=secret_name,
        namespace="stablehand-runners",
    )
