from __future__ import annotations

# This import is deliberately first: the initial TDD run must prove the provider
# does not exist yet, rather than fail because its new dependency is unavailable.
from dgx_control.pki import BuiltinCertificateAuthority, CertificateAuthority, IssuedCertificate

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ed25519, rsa
from cryptography.x509.oid import ExtendedKeyUsageOID, ExtensionOID, NameOID


NODE_ID = "spk_0123456789abcdef0123456789abcdef"
NOW = datetime(2026, 8, 3, 12, tzinfo=UTC)


def _pem_private_key(key: ed25519.Ed25519PrivateKey) -> bytes:
    return key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )


def _pem_public_key(key: ed25519.Ed25519PrivateKey) -> bytes:
    return key.public_key().public_bytes(
        serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )


def _intermediate_certificate(
    key: ed25519.Ed25519PrivateKey,
    *,
    ca: bool = True,
    path_length: int | None = 0,
    expires_at: datetime = NOW + timedelta(days=8),
) -> x509.Certificate:
    return (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "DGX Forge Agent Intermediate")]))
        .issuer_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "DGX Forge Offline Root")]))
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(NOW - timedelta(days=1))
        .not_valid_after(expires_at)
        .add_extension(x509.BasicConstraints(ca=ca, path_length=path_length), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=True,
                crl_sign=True,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .sign(key, algorithm=None)
    )


def _write_intermediate(
    directory: Path,
    key: ed25519.Ed25519PrivateKey,
    certificate: x509.Certificate,
) -> tuple[Path, Path]:
    key_path = directory / "intermediate.key"
    certificate_path = directory / "intermediate.pem"
    key_path.write_bytes(_pem_private_key(key))
    certificate_path.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    return key_path, certificate_path


@pytest.fixture
def now() -> datetime:
    return NOW


@pytest.fixture
def public_key() -> bytes:
    return _pem_public_key(ed25519.Ed25519PrivateKey.generate())


@pytest.fixture
def authority(tmp_path: Path) -> BuiltinCertificateAuthority:
    key = ed25519.Ed25519PrivateKey.generate()
    key_path, certificate_path = _write_intermediate(tmp_path, key, _intermediate_certificate(key))
    return BuiltinCertificateAuthority(key_path, certificate_path)


def test_issued_certificate_is_short_lived_and_node_bound(
    authority: CertificateAuthority, public_key: bytes, now: datetime
) -> None:
    issued = authority.issue_node(NODE_ID, public_key, now)

    certificate = x509.load_pem_x509_certificate(issued.certificate_pem)
    assert certificate.subject.get_attributes_for_oid(NameOID.COMMON_NAME)[0].value == NODE_ID
    assert certificate.extensions.get_extension_for_class(x509.ExtendedKeyUsage).value == x509.ExtendedKeyUsage(
        [ExtendedKeyUsageOID.CLIENT_AUTH]
    )
    assert certificate.not_valid_after_utc - now == timedelta(hours=24)
    assert certificate.not_valid_before_utc == now
    assert certificate.extensions.get_extension_for_class(x509.SubjectAlternativeName).value == x509.SubjectAlternativeName(
        [x509.UniformResourceIdentifier(f"spiffe://dgx-forge.local/node/{NODE_ID}")]
    )
    assert certificate.extensions.get_extension_for_class(x509.BasicConstraints).value == x509.BasicConstraints(
        ca=False, path_length=None
    )
    assert certificate.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw
    ) == serialization.load_pem_public_key(public_key).public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw
    )
    assert issued.certificate_pem not in issued.chain_pem
    assert issued.serial == format(certificate.serial_number, "x")
    assert issued.fingerprint == certificate.fingerprint(hashes.SHA256()).hex()
    assert issued.node_id == NODE_ID
    assert issued.not_before == now
    assert issued.not_after == now + timedelta(hours=24)


def test_issued_certificate_metadata_matches_second_precision_x509_validity(
    authority: CertificateAuthority, public_key: bytes, now: datetime
) -> None:
    requested_now = now.replace(microsecond=789123)
    issued = authority.issue_node(NODE_ID, public_key, requested_now)
    certificate = x509.load_pem_x509_certificate(issued.certificate_pem)

    assert issued.not_before == certificate.not_valid_before_utc
    assert issued.not_after == certificate.not_valid_after_utc
    assert issued.not_after - issued.not_before == timedelta(hours=24)


def test_issued_certificate_has_exact_signed_client_auth_profile(
    authority: CertificateAuthority, public_key: bytes, now: datetime
) -> None:
    issued = authority.issue_node(NODE_ID, public_key, now)
    certificate = x509.load_pem_x509_certificate(issued.certificate_pem)
    intermediate = x509.load_pem_x509_certificate(issued.chain_pem)

    assert certificate.subject == x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, NODE_ID)])
    assert certificate.issuer == intermediate.subject
    assert [(extension.oid, extension.critical) for extension in certificate.extensions] == [
        (ExtensionOID.BASIC_CONSTRAINTS, True),
        (ExtensionOID.KEY_USAGE, True),
        (ExtensionOID.EXTENDED_KEY_USAGE, True),
        (ExtensionOID.SUBJECT_ALTERNATIVE_NAME, False),
    ]
    assert certificate.extensions.get_extension_for_oid(ExtensionOID.BASIC_CONSTRAINTS).value == x509.BasicConstraints(
        ca=False, path_length=None
    )
    assert certificate.extensions.get_extension_for_oid(ExtensionOID.KEY_USAGE).value == x509.KeyUsage(
        digital_signature=True,
        content_commitment=False,
        key_encipherment=False,
        data_encipherment=False,
        key_agreement=False,
        key_cert_sign=False,
        crl_sign=False,
        encipher_only=False,
        decipher_only=False,
    )
    assert certificate.extensions.get_extension_for_oid(ExtensionOID.EXTENDED_KEY_USAGE).value == x509.ExtendedKeyUsage(
        [ExtendedKeyUsageOID.CLIENT_AUTH]
    )
    assert certificate.extensions.get_extension_for_oid(ExtensionOID.SUBJECT_ALTERNATIVE_NAME).value == x509.SubjectAlternativeName(
        [x509.UniformResourceIdentifier(f"spiffe://dgx-forge.local/node/{NODE_ID}")]
    )
    intermediate.public_key().verify(certificate.signature, certificate.tbs_certificate_bytes)


def test_issued_certificate_contains_no_private_key(
    authority: CertificateAuthority, public_key: bytes, now: datetime
) -> None:
    issued = authority.issue_node(NODE_ID, public_key, now)

    assert b"PRIVATE KEY" not in issued.certificate_pem
    assert b"PRIVATE KEY" not in issued.chain_pem
    assert not hasattr(issued, "private_key")


def test_renewal_issues_a_fresh_short_lived_certificate(
    authority: CertificateAuthority, public_key: bytes, now: datetime
) -> None:
    original = authority.issue_node(NODE_ID, public_key, now)
    renewed = authority.renew_node(NODE_ID, public_key, now + timedelta(hours=12))

    assert renewed.serial != original.serial
    assert original.node_id == NODE_ID
    assert renewed.node_id == NODE_ID
    assert renewed.not_before == now + timedelta(hours=12)
    assert renewed.not_after - renewed.not_before == timedelta(hours=24)


def test_revocation_bundle_is_a_signed_empty_crl(
    authority: CertificateAuthority, public_key: bytes, now: datetime
) -> None:
    bundle = authority.revocation_bundle(now)
    issued = authority.issue_node(NODE_ID, public_key, now)

    crl = x509.load_pem_x509_crl(bundle)
    intermediate = x509.load_pem_x509_certificate(issued.chain_pem)
    assert crl.issuer.get_attributes_for_oid(NameOID.COMMON_NAME)[0].value == "DGX Forge Agent Intermediate"
    assert crl.issuer == intermediate.subject
    assert crl.last_update_utc == now
    assert crl.next_update_utc == now + timedelta(hours=24)
    assert list(crl) == []
    intermediate.public_key().verify(crl.signature, crl.tbs_certlist_bytes)


@pytest.mark.parametrize("kind", ("key", "certificate"))
def test_authority_rejects_symlinked_intermediate_material(tmp_path: Path, kind: str) -> None:
    key = ed25519.Ed25519PrivateKey.generate()
    key_path, certificate_path = _write_intermediate(tmp_path, key, _intermediate_certificate(key))
    target = key_path if kind == "key" else certificate_path
    link = tmp_path / f"{kind}-link"
    link.symlink_to(target)

    with pytest.raises(ValueError, match="regular non-symlink"):
        BuiltinCertificateAuthority(link if kind == "key" else key_path, link if kind == "certificate" else certificate_path)


def test_authority_rejects_non_regular_intermediate_material(tmp_path: Path) -> None:
    key = ed25519.Ed25519PrivateKey.generate()
    _, certificate_path = _write_intermediate(tmp_path, key, _intermediate_certificate(key))
    key_directory = tmp_path / "key-directory"
    key_directory.mkdir()

    with pytest.raises(ValueError, match="regular non-symlink"):
        BuiltinCertificateAuthority(key_directory, certificate_path)


def test_authority_rejects_mismatched_intermediate_key_and_certificate(tmp_path: Path) -> None:
    key_path, certificate_path = _write_intermediate(
        tmp_path,
        ed25519.Ed25519PrivateKey.generate(),
        _intermediate_certificate(ed25519.Ed25519PrivateKey.generate()),
    )

    with pytest.raises(ValueError, match="does not match"):
        BuiltinCertificateAuthority(key_path, certificate_path)


@pytest.mark.parametrize(
    ("ca", "path_length", "expires_at", "message"),
    (
        (False, None, NOW + timedelta(days=8), "CA"),
        (True, 1, NOW + timedelta(days=8), "path length"),
        (True, 0, NOW + timedelta(days=7) - timedelta(seconds=1), "seven days"),
    ),
)
def test_authority_rejects_unsafe_intermediate_constraints(
    tmp_path: Path, ca: bool, path_length: int | None, expires_at: datetime, message: str
) -> None:
    key = ed25519.Ed25519PrivateKey.generate()
    key_path, certificate_path = _write_intermediate(
        tmp_path, key, _intermediate_certificate(key, ca=ca, path_length=path_length, expires_at=expires_at)
    )

    with pytest.raises(ValueError, match=message):
        BuiltinCertificateAuthority(key_path, certificate_path)


def test_authority_rejects_non_ed25519_node_keys(authority: CertificateAuthority, now: datetime) -> None:
    rsa_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public_key = rsa_key.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
    )

    with pytest.raises(ValueError, match="Ed25519"):
        authority.issue_node(NODE_ID, public_key, now)


@pytest.mark.parametrize("node_id", ("spark1", "", "spk_1", "spk_" + "g" * 32, NODE_ID.upper()))
def test_authority_rejects_noncanonical_node_ids(
    authority: CertificateAuthority, public_key: bytes, now: datetime, node_id: str
) -> None:
    with pytest.raises(ValueError, match="canonical"):
        authority.issue_node(node_id, public_key, now)
