from stablehand.executors.kubernetes import KubernetesExecutor
from stablehand.executors.local import LocalExecutor
from stablehand.models import ExecutorKind


def for_stack(kind: str):
    if kind == ExecutorKind.kubernetes.value:
        return KubernetesExecutor()
    return LocalExecutor()
