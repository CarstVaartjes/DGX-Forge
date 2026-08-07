from __future__ import annotations

import os

import pytest
from vonk_agent.packages.backends import BackendInvocation
from vonk_agent.packages.sandbox import SandboxError, SandboxPolicy

from .test_backends import invocation_document


def _descriptors(tmp_path):
    root = tmp_path / "generation"
    root.mkdir()
    executable = root / "adapter"
    executable.write_bytes(b"binary")
    return (
        os.open(root, os.O_RDONLY | os.O_DIRECTORY),
        os.open(executable, os.O_RDONLY),
    )


def test_sandbox_plan_uses_only_pinned_descriptors_and_unprivileged_identity(
    tmp_path,
) -> None:
    root_fd, executable_fd = _descriptors(tmp_path)
    try:
        invocation = BackendInvocation.parse(invocation_document())
        policy = SandboxPolicy(
            workload_uid=64001,
            workload_gid=64001,
            allowed_devices=("nvidia0",),
        )

        plan = policy.plan(invocation, root_fd, executable_fd)

        assert plan.executable == f"/proc/self/fd/{executable_fd}"
        assert plan.cwd == f"/proc/self/fd/{root_fd}"
        assert plan.argv == (plan.executable, "serve", "--port", "8080")
        assert plan.uid == plan.gid == 64001
        assert plan.no_new_privileges is True
        assert plan.ambient_capabilities == ()
        assert plan.inherited_fds == (root_fd, executable_fd)
        assert plan.environment == {
            "HOME": "/nonexistent",
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
            "PATH": "/usr/bin:/bin",
            "PYTHONNOUSERSITE": "1",
        }
    finally:
        os.close(executable_fd)
        os.close(root_fd)


def test_sandbox_rejects_root_identity_and_undeclared_devices(tmp_path) -> None:
    with pytest.raises(SandboxError, match="unprivileged"):
        SandboxPolicy(workload_uid=0, workload_gid=64001)

    root_fd, executable_fd = _descriptors(tmp_path)
    try:
        invocation = BackendInvocation.parse(invocation_document())
        policy = SandboxPolicy(workload_uid=64001, workload_gid=64001)

        with pytest.raises(SandboxError, match="device"):
            policy.plan(invocation, root_fd, executable_fd)
    finally:
        os.close(executable_fd)
        os.close(root_fd)


def test_sandbox_rejects_resource_requests_above_local_ceiling(tmp_path) -> None:
    root_fd, executable_fd = _descriptors(tmp_path)
    try:
        invocation = BackendInvocation.parse(invocation_document())
        policy = SandboxPolicy(
            workload_uid=64001,
            workload_gid=64001,
            allowed_devices=("nvidia0",),
            max_memory_bytes=1024,
        )

        with pytest.raises(SandboxError, match="memory"):
            policy.plan(invocation, root_fd, executable_fd)
    finally:
        os.close(executable_fd)
        os.close(root_fd)


def test_sandbox_rejects_closed_or_nonregular_execution_descriptors(tmp_path) -> None:
    root_fd, executable_fd = _descriptors(tmp_path)
    os.close(executable_fd)
    policy = SandboxPolicy(
        workload_uid=64001,
        workload_gid=64001,
        allowed_devices=("nvidia0",),
    )
    try:
        with pytest.raises(SandboxError, match="descriptor"):
            policy.plan(
                BackendInvocation.parse(invocation_document()), root_fd, executable_fd
            )
    finally:
        os.close(root_fd)
