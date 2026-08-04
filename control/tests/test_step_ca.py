from __future__ import annotations

# Keep this import first so the TDD RED proves the provider is absent before
# any new runtime dependency is imported.
from dgx_control.step_ca import StepCertificateAuthority, StepCAError

from datetime import UTC, datetime, timedelta
import base64
import json
from pathlib import Path
from types import SimpleNamespace

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, ed25519
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID
import httpx
import jwt
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from dgx_control.agent_api import AgentApiServices
from dgx_control.api import build_agent_services
from dgx_control.models import Base


NODE_ID = "spk_0123456789abcdef0123456789abcdef"
NOW = datetime(2026, 8, 4, 12, tzinfo=UTC)
CA_URL = "https://step-ca:9000"


def _b64(value: int) -> str:
    return base64.urlsafe_b64encode(value.to_bytes(32, "big")).rstrip(b"=").decode()


def _write_material(tmp_path: Path) -> dict[str, object]:
    tmp_path.mkdir(parents=True, exist_ok=True)
    root_key = ed25519.Ed25519PrivateKey.generate()
    root_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "DGX Forge Offline Root")])
    root = (
        x509.CertificateBuilder().subject_name(root_name).issuer_name(root_name)
        .public_key(root_key.public_key()).serial_number(x509.random_serial_number())
        .not_valid_before(NOW - timedelta(days=1)).not_valid_after(NOW + timedelta(days=3650))
        .add_extension(x509.BasicConstraints(ca=True, path_length=1), critical=True)
        .add_extension(x509.KeyUsage(False, False, False, False, False, True, True, False, False), critical=True)
        .sign(root_key, algorithm=None)
    )
    intermediate_key = ed25519.Ed25519PrivateKey.generate()
    intermediate_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "DGX Forge Agent Intermediate")])
    intermediate = (
        x509.CertificateBuilder().subject_name(intermediate_name).issuer_name(root.subject)
        .public_key(intermediate_key.public_key()).serial_number(x509.random_serial_number())
        .not_valid_before(NOW - timedelta(days=1)).not_valid_after(NOW + timedelta(days=365))
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .add_extension(x509.KeyUsage(False, False, False, False, False, True, True, False, False), critical=True)
        .sign(root_key, algorithm=None)
    )
    provisioner_key = ec.generate_private_key(ec.SECP256R1())
    root_path = tmp_path / "root.pem"
    intermediate_path = tmp_path / "intermediate.pem"
    credential_path = tmp_path / "provisioner.pem"
    public_jwk_path = tmp_path / "provisioner-public.jwk"
    root_path.write_bytes(root.public_bytes(serialization.Encoding.PEM))
    intermediate_path.write_bytes(intermediate.public_bytes(serialization.Encoding.PEM))
    numbers = provisioner_key.public_key().public_numbers()
    public_jwk = {"kty": "EC", "crv": "P-256", "use": "sig", "alg": "ES256", "x": _b64(numbers.x), "y": _b64(numbers.y)}
    thumbprint_input = json.dumps({name: public_jwk[name] for name in ("crv", "kty", "x", "y")}, sort_keys=True, separators=(",", ":")).encode()
    import hashlib
    kid = base64.urlsafe_b64encode(hashlib.sha256(thumbprint_input).digest()).rstrip(b"=").decode()
    public_jwk["kid"] = kid
    private_jwk = public_jwk | {"d": _b64(provisioner_key.private_numbers().private_value)}
    credential_path.write_text(json.dumps(private_jwk))
    public_jwk_path.write_text(json.dumps(public_jwk))
    credential_path.chmod(0o600)
    return {
        "root": root, "root_path": root_path,
        "intermediate": intermediate, "intermediate_key": intermediate_key,
        "intermediate_path": intermediate_path, "credential_path": credential_path,
        "public_jwk_path": public_jwk_path,
        "public_jwk": public_jwk, "kid": kid,
    }


def _csr(node_id: str = NODE_ID) -> bytes:
    key = ed25519.Ed25519PrivateKey.generate()
    return (
        x509.CertificateSigningRequestBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, node_id)]))
        .add_extension(x509.SubjectAlternativeName([
            x509.UniformResourceIdentifier(f"spiffe://dgx-forge.local/node/{node_id}")
        ]), critical=False)
        .sign(key, algorithm=None)
        .public_bytes(serialization.Encoding.PEM)
    )


def _leaf(csr_pem: bytes, material: dict[str, object], *, now: datetime = NOW, serial: int = 1234) -> x509.Certificate:
    request = x509.load_pem_x509_csr(csr_pem)
    return (
        x509.CertificateBuilder().subject_name(request.subject)
        .issuer_name(material["intermediate"].subject).public_key(request.public_key())
        .serial_number(serial).not_valid_before(now).not_valid_after(now + timedelta(hours=24))
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(x509.KeyUsage(True, False, False, False, False, False, False, False, False), critical=True)
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.CLIENT_AUTH]), critical=True)
        .add_extension(request.extensions.get_extension_for_class(x509.SubjectAlternativeName).value, critical=False)
        .sign(material["intermediate_key"], algorithm=None)
    )


def _provider(tmp_path: Path, handler, *, max_response_bytes: int = 64 * 1024) -> tuple[StepCertificateAuthority, dict[str, object]]:
    material = _write_material(tmp_path)
    provider = StepCertificateAuthority(
        ca_url=CA_URL,
        root_certificate_path=material["root_path"],
        intermediate_certificate_path=material["intermediate_path"],
        provisioner_name="dgx-forge-agent",
        provisioner_kid=material["kid"],
        credential_path=material["credential_path"],
        provisioner_public_jwk_path=material["public_jwk_path"],
        timeout_seconds=2.0,
        max_response_bytes=max_response_bytes,
        transport=httpx.MockTransport(handler),
    )
    return provider, material


def _success_response(request: httpx.Request, material: dict[str, object], seen: list[dict[str, object]], *, serial: int = 1234) -> httpx.Response:
    body = json.loads(request.content)
    seen.append({"request": request, "body": body})
    leaf = _leaf(body["csr"].encode(), material, serial=serial)
    leaf_pem = leaf.public_bytes(serialization.Encoding.PEM).decode()
    intermediate_pem = material["intermediate"].public_bytes(serialization.Encoding.PEM).decode()
    return httpx.Response(201, json={"crt": leaf_pem, "ca": intermediate_pem, "certChain": [leaf_pem, intermediate_pem]})


def test_sign_uses_fixed_policy_short_lived_one_use_authorization_and_node_signed_csr(tmp_path: Path) -> None:
    seen: list[dict[str, object]] = []
    holder: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        return _success_response(request, holder["material"], seen)

    provider, material = _provider(tmp_path, handler)
    holder["material"] = material
    request_pem = _csr()
    issued = provider.issue_node(NODE_ID, request_pem, NOW)

    assert issued.node_id == NODE_ID
    assert len(seen) == 1
    request = seen[0]["request"]
    assert request.url == f"{CA_URL}/1.0/sign"
    assert request.headers["content-type"] == "application/json"
    assert set(seen[0]["body"]) == {"csr", "ott", "notBefore", "notAfter"}
    assert seen[0]["body"]["csr"] == request_pem.decode()
    assert seen[0]["body"]["notBefore"] == "2026-08-04T12:00:00Z"
    assert seen[0]["body"]["notAfter"] == "2026-08-05T12:00:00Z"
    token = seen[0]["body"]["ott"]
    header = jwt.get_unverified_header(token)
    claims = jwt.decode(token, options={"verify_signature": False})
    assert header == {"alg": "ES256", "kid": material["kid"], "typ": "JWT"}
    assert claims["iss"] == "dgx-forge-agent"
    assert claims["sub"] == NODE_ID
    assert claims["aud"] == f"{CA_URL}/1.0/sign"
    assert claims["sans"] == [f"spiffe://dgx-forge.local/node/{NODE_ID}"]
    assert claims["exp"] - claims["iat"] == 60
    assert claims["nbf"] == claims["iat"] - 30
    assert len(claims["jti"]) >= 43
    certificate = x509.load_pem_x509_certificate(issued.certificate_pem)
    assert issued.serial == str(certificate.serial_number)
    assert issued.fingerprint == certificate.fingerprint(hashes.SHA256()).hex()


def test_renewal_uses_new_signed_csr_and_fresh_serial(tmp_path: Path) -> None:
    seen: list[dict[str, object]] = []
    holder: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        return _success_response(request, holder["material"], seen, serial=5678)

    provider, material = _provider(tmp_path, handler)
    holder["material"] = material
    request_pem = _csr()
    issued = provider.renew_node(NODE_ID, request_pem, NOW)

    assert seen[0]["body"]["csr"] == request_pem.decode()
    assert issued.serial == "5678"


def test_revocation_is_authenticated_passive_and_idempotent_in_effect(tmp_path: Path) -> None:
    seen: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        return httpx.Response(200, json={"status": "ok"})

    provider, _ = _provider(tmp_path, handler)
    provider.revoke_node("5678", NOW)
    provider.revoke_node("5678", NOW)

    assert len(seen) == 2
    assert all(set(body) == {"serial", "ott", "reasonCode", "reason", "passive"} for body in seen)
    assert all(body | {"ott": "redacted"} == {"serial": "5678", "ott": "redacted", "reasonCode": 4, "reason": "superseded by DGX-Forge", "passive": True} for body in seen)
    for body in seen:
        claims = jwt.decode(body["ott"], options={"verify_signature": False})
        assert claims["aud"] == f"{CA_URL}/1.0/revoke"
        assert claims["sub"] == "5678"
    assert seen[0]["ott"] != seen[1]["ott"]


@pytest.mark.parametrize("mutation", ("key", "subject", "san", "eku", "usage", "issuer", "lifetime", "chain", "extra-chain"))
def test_rejects_malformed_or_policy_mismatched_sign_responses(tmp_path: Path, mutation: str) -> None:
    holder: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        material = holder["material"]
        body = json.loads(request.content)
        request_pem = body["csr"].encode()
        leaf = _leaf(request_pem, material)
        other_intermediate = _write_material(tmp_path / "other") if mutation in {"issuer", "chain"} else None
        if mutation == "key":
            request_pem = _csr()
        if mutation in {"subject", "san", "eku", "usage", "lifetime", "key", "issuer"}:
            request_obj = x509.load_pem_x509_csr(request_pem)
            node = "spk_fedcba9876543210fedcba9876543210" if mutation in {"subject", "san"} else NODE_ID
            signer = other_intermediate["intermediate_key"] if mutation == "issuer" else material["intermediate_key"]
            issuer = other_intermediate["intermediate"].subject if mutation == "issuer" else material["intermediate"].subject
            builder = (
                x509.CertificateBuilder().subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, node)]))
                .issuer_name(issuer).public_key(request_obj.public_key()).serial_number(9876)
                .not_valid_before(NOW).not_valid_after(NOW + (timedelta(hours=25) if mutation == "lifetime" else timedelta(hours=24)))
                .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
                .add_extension(x509.KeyUsage(mutation != "usage", False, False, False, False, False, False, False, False), critical=True)
                .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH if mutation == "eku" else ExtendedKeyUsageOID.CLIENT_AUTH]), critical=True)
                .add_extension(x509.SubjectAlternativeName([x509.UniformResourceIdentifier(f"spiffe://dgx-forge.local/node/{node}")]), critical=False)
            )
            leaf = builder.sign(signer, algorithm=None)
        chain_ca = other_intermediate["intermediate"] if mutation == "chain" else material["intermediate"]
        leaf_pem = leaf.public_bytes(serialization.Encoding.PEM).decode()
        ca_pem = chain_ca.public_bytes(serialization.Encoding.PEM).decode()
        chain = [leaf_pem, ca_pem]
        if mutation == "extra-chain":
            chain.append(material["root"].public_bytes(serialization.Encoding.PEM).decode())
        return httpx.Response(201, json={"crt": leaf_pem, "ca": ca_pem, "certChain": chain})

    (tmp_path / "other").mkdir(exist_ok=True)
    provider, material = _provider(tmp_path, handler)
    holder["material"] = material
    with pytest.raises(StepCAError):
        provider.issue_node(NODE_ID, _csr(), NOW)


def test_rejects_redirects_proxy_environment_oversize_and_secret_leakage(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("HTTPS_PROXY", "http://attacker.invalid:3128")
    requests: list[httpx.Request] = []

    def redirect(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(307, headers={"location": "https://attacker.invalid/sign"})

    provider, _ = _provider(tmp_path, redirect)
    with pytest.raises(StepCAError) as caught:
        provider.issue_node(NODE_ID, _csr(), NOW)
    assert len(requests) == 1 and requests[0].url.host == "step-ca"
    assert "eyJ" not in str(caught.value)

    def oversized(_: httpx.Request) -> httpx.Response:
        return httpx.Response(201, content=b"{" + b"x" * 2048 + b"}")

    bounded, _ = _provider(tmp_path / "bounded", oversized, max_response_bytes=1024)
    with pytest.raises(StepCAError, match="too large"):
        bounded.issue_node(NODE_ID, _csr(), NOW)


@pytest.mark.parametrize("url", ("http://step-ca:9000", "https://step-ca:9000/path", "https://user@step-ca:9000", "https://step-ca:9000?x=1"))
def test_rejects_nonfixed_or_non_https_ca_urls(tmp_path: Path, url: str) -> None:
    material = _write_material(tmp_path)
    with pytest.raises(ValueError, match="CA URL"):
        StepCertificateAuthority(
            ca_url=url, root_certificate_path=material["root_path"],
            intermediate_certificate_path=material["intermediate_path"],
            provisioner_name="dgx-forge-agent", provisioner_kid=material["kid"],
            credential_path=material["credential_path"],
            provisioner_public_jwk_path=material["public_jwk_path"],
        )


def test_rejects_symlinked_root_and_credential_files(tmp_path: Path) -> None:
    material = _write_material(tmp_path)
    for argument, target in (("root_certificate_path", material["root_path"]), ("credential_path", material["credential_path"])):
        link = tmp_path / f"{argument}.link"
        link.symlink_to(target)
        values = {
            "ca_url": CA_URL, "root_certificate_path": material["root_path"],
            "intermediate_certificate_path": material["intermediate_path"],
            "provisioner_name": "dgx-forge-agent", "provisioner_kid": material["kid"],
            "credential_path": material["credential_path"],
            "provisioner_public_jwk_path": material["public_jwk_path"],
        }
        values[argument] = link
        with pytest.raises(ValueError, match="regular non-symlink"):
            StepCertificateAuthority(**values)


def test_rejects_public_provisioner_key_with_copied_configured_kid(tmp_path: Path) -> None:
    material = _write_material(tmp_path)
    other = ec.generate_private_key(ec.SECP256R1()).public_key().public_numbers()
    copied = dict(material["public_jwk"])
    copied["x"], copied["y"] = _b64(other.x), _b64(other.y)
    material["public_jwk_path"].write_text(json.dumps(copied))

    with pytest.raises(ValueError, match="does not match private credential"):
        StepCertificateAuthority(
            ca_url=CA_URL, root_certificate_path=material["root_path"],
            intermediate_certificate_path=material["intermediate_path"],
            provisioner_name="dgx-forge-agent", provisioner_kid=material["kid"],
            credential_path=material["credential_path"],
            provisioner_public_jwk_path=material["public_jwk_path"],
        )


def test_health_probe_is_bounded_get_without_body(tmp_path: Path) -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"status": "ok"})

    provider, _ = _provider(tmp_path, handler)
    provider.check_health()

    assert len(seen) == 1
    assert seen[0].method == "GET" and seen[0].url == f"{CA_URL}/health"
    assert seen[0].content == b""


def test_production_agent_service_builder_selects_step_ca_and_checks_reachability(tmp_path: Path, monkeypatch) -> None:
    calls: list[object] = []

    class FakeStepAuthority:
        def __init__(self, **kwargs) -> None:
            calls.append(kwargs)

        def check_health(self) -> None:
            calls.append("health")

    monkeypatch.setattr("dgx_control.step_ca.StepCertificateAuthority", FakeStepAuthority)
    engine = create_engine(f"sqlite:///{tmp_path / 'runtime.sqlite'}")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)
    settings = SimpleNamespace(
        agent_runtime="enabled", agent_ca_provider="step-ca",
        agent_intermediate_certificate_path=tmp_path / "intermediate.pem",
        agent_intermediate_key_path=None, agent_ca_root_path=tmp_path / "root.pem",
        agent_ca_credential_path=tmp_path / "credential.pem", agent_ca_url=CA_URL,
        agent_ca_provisioner_public_jwk_path=tmp_path / "public.jwk",
        agent_ca_provisioner_name="dgx-forge-agent", agent_ca_provisioner_kid="kid",
        agent_ca_timeout_seconds=2.0, agent_ca_max_response_bytes=4096,
        agent_artifact_root=tmp_path / "artifacts",
    )

    services = build_agent_services(settings, sessions, lambda: NOW)

    assert isinstance(services, AgentApiServices)
    assert calls[-1] == "health"
    assert calls[0]["ca_url"] == CA_URL
    assert settings.agent_artifact_root.is_dir()


def test_production_agent_service_builder_fails_closed_on_unreachable_or_mixed_provider(tmp_path: Path, monkeypatch) -> None:
    class Unreachable:
        def __init__(self, **kwargs) -> None:
            pass

        def check_health(self) -> None:
            raise StepCAError("step-ca request failed")

    monkeypatch.setattr("dgx_control.step_ca.StepCertificateAuthority", Unreachable)
    settings = SimpleNamespace(
        agent_runtime="enabled", agent_ca_provider="step-ca",
        agent_intermediate_certificate_path=tmp_path / "intermediate.pem",
        agent_intermediate_key_path=None, agent_ca_root_path=tmp_path / "root.pem",
        agent_ca_credential_path=tmp_path / "credential.pem", agent_ca_url=CA_URL,
        agent_ca_provisioner_public_jwk_path=tmp_path / "public.jwk",
        agent_ca_provisioner_name="dgx-forge-agent", agent_ca_provisioner_kid="kid",
        agent_ca_timeout_seconds=2.0, agent_ca_max_response_bytes=4096,
        agent_artifact_root=tmp_path / "artifacts",
    )
    with pytest.raises(StepCAError, match="request failed"):
        build_agent_services(settings, object(), lambda: NOW)

    settings.agent_ca_provider = "unknown"
    with pytest.raises(RuntimeError, match="provider is unavailable"):
        build_agent_services(settings, object(), lambda: NOW)


def test_tracked_step_ca_template_is_public_only_and_matches_provider_validation() -> None:
    config_path = Path(__file__).resolve().parents[2] / "deploy/compose/step-ca/ca.json"
    config = json.loads(config_path.read_text())
    provisioner = config["authority"]["provisioners"][0]

    assert provisioner["type"] == "JWK" and provisioner["name"] == "dgx-forge-agent"
    assert "encryptedKey" not in provisioner and "d" not in provisioner["key"]
    assert provisioner["claims"] == {
        "minTLSCertDuration": "24h", "maxTLSCertDuration": "24h",
        "defaultTLSCertDuration": "24h", "disableRenewal": True,
        "disableSmallstepExtensions": True,
    }
    template = provisioner["options"]["x509"]["template"]
    assert "digitalSignature" in template and "clientAuth" in template
    assert "serverAuth" not in template
