"""Production handlers for the per-Spark installation gates."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from cluster_profiles.fleet.install_contracts import InstallationRequest

from .identity import (
    IdentityObservation,
    TrustedIdentityAssertion,
    evaluate_identity,
)
from .orchestrator import StepResult, WaitForOperator
from .remote import InstallTransport, RemoteResult


@dataclass(frozen=True)
class ProductionStepOptions:
    repository_root: Path
    admin_public_key: Path | None
    admin_key_fingerprint: str | None
    trusted_serial_sha256: str | None
    trusted_host_key_fingerprints: tuple[str, ...]
    recovery_verified: bool
    expected_artifact_sha256: Mapping[str, str] = field(default_factory=dict)

    @classmethod
    def from_arguments(cls, arguments: object, *, root: Path) -> ProductionStepOptions:
        public_key = getattr(arguments, "admin_public_key", None)
        return cls(
            repository_root=root,
            admin_public_key=Path(public_key) if public_key else None,
            admin_key_fingerprint=getattr(arguments, "admin_key_fingerprint", None),
            trusted_serial_sha256=getattr(arguments, "trusted_serial_sha256", None),
            trusted_host_key_fingerprints=tuple(
                getattr(arguments, "trusted_host_key_fingerprint", ()) or ()
            ),
            recovery_verified=bool(getattr(arguments, "recovery_verified", False)),
        )


def _checked(result: RemoteResult, action: str, accepted: tuple[int, ...] = (0,)) -> RemoteResult:
    if result.returncode not in accepted:
        detail = result.stderr.decode(errors="replace").strip()[:400]
        raise RuntimeError(f"{action} failed with exit {result.returncode}: {detail}")
    return result


def _json_result(result: RemoteResult, action: str) -> dict[str, object]:
    _checked(result, action)
    try:
        value = json.loads(result.stdout)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise RuntimeError(f"{action} returned invalid JSON") from error
    if not isinstance(value, dict):
        raise TypeError(f"{action} returned a non-object")
    return value


def _step_result(result: RemoteResult) -> StepResult:
    return StepResult(result.stdout, result.stderr)


def _artifact(root: Path, relative: str, expected: Mapping[str, str]) -> Path:
    path = root / relative
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"required installation artifact is missing: {relative}")
    name = path.name
    if name in expected:
        actual = hashlib.sha256(path.read_bytes()).hexdigest()
        if actual != expected[name]:
            raise ValueError(f"installation artifact digest mismatch: {name}")
    return path


def build_production_handlers(
    options: ProductionStepOptions,
    transport: InstallTransport,
) -> dict[str, object]:
    """Build stateful handlers scoped exclusively to each request endpoint."""

    root = options.repository_root
    expected = options.expected_artifact_sha256
    identity_script = _artifact(root, "nodes/bin/inspect-node-identity", expected)
    inventory_script = _artifact(root, "nodes/bin/collect-inventory", expected)
    hardening_script = _artifact(root, "nodes/bin/install-ssh-hardening", expected)
    policy_script = _artifact(root, "nodes/bin/apply-node-policy", expected)
    earlyoom_script = _artifact(root, "nodes/bin/disable-earlyoom", expected)
    ssh_drop_in = _artifact(
        root, "nodes/etc/ssh/sshd_config.d/90-dgx-admin.conf", expected
    )
    policy_document = _artifact(root, "nodes/policy/default.json", expected)
    observations: dict[str, object] = {}

    def identity(request: InstallationRequest) -> StepResult:
        result = transport.run(
            request.endpoint, ("bash", "-s"), identity_script.read_bytes(), 30
        )
        payload = _json_result(result, "node identity inspection")
        observation = IdentityObservation(
            product_serial_sha256=str(payload.get("product_serial_sha256", "")),
            machine_id_sha256=str(payload.get("machine_id_sha256", "")),
            host_key_fingerprints=tuple(payload.get("host_key_fingerprints", ())),
            requires_console_repair=payload.get("requires_console_repair", True),
        )
        assertion = None
        if options.trusted_serial_sha256 and options.trusted_host_key_fingerprints:
            assertion = TrustedIdentityAssertion(
                options.trusted_serial_sha256,
                options.trusted_host_key_fingerprints,
            )
        decision = evaluate_identity(observation, assertion)
        if decision.action == "wait-for-console":
            raise WaitForOperator(decision.reason)
        if decision.action == "quarantine":
            raise RuntimeError(decision.reason)
        observations["identity"] = observation
        return _step_result(result)

    def inventory(request: InstallationRequest, name: str) -> StepResult:
        result = transport.run(
            request.endpoint, ("bash", "-s"), inventory_script.read_bytes(), 60
        )
        observations[name] = _json_result(result, f"{name} inventory")
        return _step_result(result)

    def public_key(request: InstallationRequest) -> StepResult:
        key_path = options.admin_public_key
        fingerprint = options.admin_key_fingerprint
        if key_path is None or fingerprint is None:
            raise WaitForOperator("administrator public key and fingerprint are required")
        if key_path.is_symlink() or not key_path.is_file():
            raise ValueError("administrator public key must be a regular non-symlink file")
        key = key_path.read_bytes()
        if b"PRIVATE" in key or len(key.splitlines()) != 1 or not key.startswith(b"ssh-"):
            raise ValueError("administrator public key file is invalid")
        script = b"""set -euo pipefail
fp=$1
line=$(cat)
mkdir -p "$HOME/.ssh"
chmod 700 "$HOME/.ssh"
touch "$HOME/.ssh/authorized_keys"
chmod 600 "$HOME/.ssh/authorized_keys"
if ! ssh-keygen -lf "$HOME/.ssh/authorized_keys" 2>/dev/null | grep -Fq -- "$fp"; then
  printf '%s\\n' "$line" >>"$HOME/.ssh/authorized_keys"
fi
"""
        result = transport.run(
            request.endpoint, ("bash", "-c", script.decode(), "--", fingerprint), key, 30
        )
        return _step_result(_checked(result, "administrator public-key installation"))

    def ssh_hardening(request: InstallationRequest) -> StepResult:
        if not options.recovery_verified:
            raise WaitForOperator("verified SSH recovery access is required")
        if not options.admin_key_fingerprint:
            raise WaitForOperator("administrator public key fingerprint is required")
        staged_script = "/tmp/dgx-install-ssh-hardening"
        staged_drop_in = "/tmp/dgx-install-ssh-drop-in.conf"
        _checked(transport.copy(request.endpoint, hardening_script, staged_script, 0o755), "stage SSH hardening")
        _checked(transport.copy(request.endpoint, ssh_drop_in, staged_drop_in, 0o600), "stage SSH drop-in")
        marker = "/run/dgx-ssh-recovery-verified"
        try:
            _checked(transport.run(request.endpoint, ("sudo", "install", "-m", "0600", "/dev/stdin", marker), b"verified\n", 30), "create recovery marker")
            base = ("sudo", "bash", staged_script, "--admin-user", request.endpoint.user, "--admin-key-fingerprint", options.admin_key_fingerprint, "--drop-in", staged_drop_in, "--recovery-marker", marker)
            _checked(transport.run(request.endpoint, (*base, "--check"), b"", 60), "check SSH hardening", (0, 2))
            result = _checked(transport.run(request.endpoint, (*base, "--apply"), b"", 60), "apply SSH hardening")
            _checked(transport.run(request.endpoint, (*base, "--verify"), b"", 60), "verify SSH hardening")
            return _step_result(result)
        finally:
            transport.run(request.endpoint, ("sudo", "rm", "-f", "--", marker, staged_script, staged_drop_in), b"", 30)

    def node_policy(request: InstallationRequest) -> StepResult:
        staged_policy = "/tmp/dgx-apply-node-policy"
        staged_earlyoom = "/tmp/dgx-disable-earlyoom"
        staged_document = "/tmp/dgx-node-policy.json"
        for source, destination, mode in (
            (policy_script, staged_policy, 0o755),
            (earlyoom_script, staged_earlyoom, 0o755),
            (policy_document, staged_document, 0o600),
        ):
            _checked(transport.copy(request.endpoint, source, destination, mode), "stage node policy")
        try:
            base = ("sudo", "env", f"DGX_DISABLE_EARLYOOM_BIN={staged_earlyoom}", "bash", staged_policy, "--policy", staged_document)
            _checked(transport.run(request.endpoint, (*base, "--check"), b"", 60), "check node policy", (0, 2))
            result = _checked(transport.run(request.endpoint, (*base, "--apply"), b"", 60), "apply node policy")
            _checked(transport.run(request.endpoint, (*base, "--verify"), b"", 60), "verify node policy")
            return _step_result(result)
        finally:
            transport.run(request.endpoint, ("sudo", "rm", "-f", "--", staged_policy, staged_earlyoom, staged_document), b"", 30)

    def acceptance(_request: InstallationRequest) -> StepResult:
        if not all(name in observations for name in ("identity", "pre", "post")):
            raise RuntimeError("acceptance requires identity and both inventories")
        post = observations["post"]
        assert isinstance(post, dict)
        if not str(post.get("hostname", "")).strip() or not str(post.get("boot_id", "")).strip():
            raise RuntimeError("post-install inventory lacks host identity")
        if post.get("nvidia") is None or post.get("docker") is None:
            raise RuntimeError("post-install inventory lacks GPU or Docker readiness")
        content = json.dumps({"status": "accepted", "hostname": post["hostname"]}, sort_keys=True).encode() + b"\n"
        return StepResult(content, b"")

    return {
        "identity": identity,
        "pre-inventory": lambda request: inventory(request, "pre"),
        "public-key": public_key,
        "ssh-hardening": ssh_hardening,
        "node-policy": node_policy,
        "post-inventory": lambda request: inventory(request, "post"),
        "acceptance": acceptance,
    }
