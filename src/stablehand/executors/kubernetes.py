from __future__ import annotations

import logging

from kubernetes import client, config
from kubernetes.client import ApiException

from stablehand.config import get_settings
from stablehand.executors.base import Executor, runner_env
from stablehand.models import Run, Stack

logger = logging.getLogger(__name__)


class KubernetesExecutor(Executor):
    def start(self, run: Run, stack: Stack, phase: str, token: str) -> tuple[str, str | None]:
        _load_config()
        settings = get_settings()
        namespace = settings.k8s_namespace
        job_name = f"sh-{str(run.id)[:8]}-{phase}"
        secret_name = f"{job_name}-token"
        core = client.CoreV1Api()
        batch = client.BatchV1Api()
        core.create_namespaced_secret(
            namespace,
            client.V1Secret(
                metadata=client.V1ObjectMeta(name=secret_name),
                string_data={"token": token},
            ),
        )
        env = [
            client.V1EnvVar(name=key, value=value)
            for key, value in runner_env(run, stack, phase, "").items()
            if key != "STABLEHAND_RUN_TOKEN"
        ]
        env.append(
            client.V1EnvVar(
                name="STABLEHAND_RUN_TOKEN",
                value_from=client.V1EnvVarSource(
                    secret_key_ref=client.V1SecretKeySelector(name=secret_name, key="token")
                ),
            )
        )
        volumes = []
        mounts = []
        if stack.secret_ref:
            volumes.append(
                client.V1Volume(
                    name="secrets",
                    secret=client.V1SecretVolumeSource(secret_name=stack.secret_ref),
                )
            )
            mounts.append(
                client.V1VolumeMount(name="secrets", mount_path="/secrets", read_only=True)
            )
            for item in env:
                if item.name == "STABLEHAND_SECRETS":
                    item.value = "/secrets"
        job = client.V1Job(
            metadata=client.V1ObjectMeta(name=job_name),
            spec=client.V1JobSpec(
                backoff_limit=0,
                ttl_seconds_after_finished=600,
                template=client.V1PodTemplateSpec(
                    spec=client.V1PodSpec(
                        restart_policy="Never",
                        automount_service_account_token=False,
                        containers=[
                            client.V1Container(
                                name="runner",
                                image=settings.runner_image,
                                command=["stablehand-runner"],
                                env=env,
                                volume_mounts=mounts or None,
                            )
                        ],
                        volumes=volumes or None,
                    )
                ),
            ),
        )
        batch.create_namespaced_job(namespace, job)
        return job_name, secret_name

    def poll(self, ref: str, secret_name: str | None) -> str:
        del secret_name
        _load_config()
        job = client.BatchV1Api().read_namespaced_job(ref, get_settings().k8s_namespace)
        status = job.status
        if status is not None and status.failed:
            return "failed"
        if status is not None and status.succeeded:
            return "finished"
        return "running"

    def cleanup(self, ref: str, secret_name: str | None) -> None:
        if not secret_name:
            return
        _load_config()
        try:
            client.CoreV1Api().delete_namespaced_secret(secret_name, get_settings().k8s_namespace)
        except ApiException as exc:
            if exc.status != 404:
                logger.warning("could not delete secret %s: %s", secret_name, exc.reason)


def _load_config() -> None:
    try:
        config.load_incluster_config()
    except config.ConfigException:
        config.load_kube_config()
