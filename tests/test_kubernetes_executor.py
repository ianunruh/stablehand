import uuid
from unittest.mock import Mock

import pytest
from kubernetes.client import ApiException

from stablehand.executors import kubernetes
from stablehand.executors.kubernetes import KubernetesExecutor, _resource_names
from stablehand.models import Execution, ExecutorKind, Run, RunState, Stack


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
    stack = Stack(
        id=stack_id,
        name="demo",
        deploy_file="deploy.py",
        inventory="inventory.py",
        executor=ExecutorKind.kubernetes.value,
        git_url="https://example.test/repo.git",
        git_ref="main",
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
    stack = Stack(id=uuid.uuid4())
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
    stack = Stack(id=uuid.uuid4())
    _, secret_name = _resource_names(run_id, execution_id, "check")

    assert KubernetesExecutor().recover(execution, run, stack) is None
    core.delete_namespaced_secret.assert_called_once_with(
        name=secret_name,
        namespace="stablehand-runners",
    )
