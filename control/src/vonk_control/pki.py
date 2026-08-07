"""Certificate issuance boundary for outbound GPU node agents.

The built-in issuer deliberately owns only the online intermediate key.  The
offline root never appears in this API or in the control service's runtime
configuration, so a remote CA implementation can replace this class without
changing enrollment callers.
"""

from __future__ import annotations

import os
import re
import stat
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ed25519
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

_NODE_ID = re.compile(r"spk_[0-9a-f]{32}")
_PROVIDER_REQUEST_ID = re.compile(r"[A-Za-z0-9_-]{43}\Z")
_CERTIFICATE_LIFETIME = timedelta(hours=24)
_MINIMUM_INTERMEDIATE_LIFETIME = timedelta(days=7)


@dataclass(frozen=True)
class IssuedCertificate:
    """Public certificate material returned to a node after issuance."""

    node_id: str
    certificate_pem: bytes
    chain_pem: bytes
    serial: str
    fingerprint: str
    not_before: datetime
    not_after: datetime
    generation: int = 1


class CertificateAuthority(ABC):
    """Stable CA provider boundary; Smallstep can implement this interface."""

    @abstractmethod
    def issue_node(self, node_id: str, csr_pem: bytes, now: datetime) -> IssuedCertificate:
        """Issue a client certificate that represents exactly one node."""

    @abstractmethod
    def renew_node(
        self,
        node_id: str,
        csr_pem: bytes,
        now: datetime,
        *,
        request_id: str,
    ) -> IssuedCertificate:
        """Rotate a node certificate after its authenticated renewal request."""

    @abstractmethod
    def revocation_bundle(self, now: datetime) -> bytes:
        """Return the intermediate's current signed CRL."""

    @abstractmethod
    def revoke_node(self, serial: str, now: datetime) -> None:
        """Revoke one decimal certificate serial; repeated calls are safe in effect."""


class BuiltinCertificateAuthority(CertificateAuthority):
    """Issue short-lived agent certificates from an Ed25519 intermediate."""

    def __init__(self, intermediate_key_path: Path | str, intermediate_certificate_path: Path | str) -> None:
        key_bytes = _read_regular_secret_file(intermediate_key_path)
        certificate_bytes = _read_regular_secret_file(intermediate_certificate_path)
        try:
            key = serialization.load_pem_private_key(key_bytes, password=None)
            certificate = x509.load_pem_x509_certificate(certificate_bytes)
        except ValueError as error:
            raise ValueError("intermediate key and certificate must be valid PEM") from error
        if not isinstance(key, ed25519.Ed25519PrivateKey):
            raise ValueError("intermediate signing key must be Ed25519")  # noqa: TRY004
        if not isinstance(certificate.public_key(), ed25519.Ed25519PublicKey):
            raise ValueError("intermediate certificate public key must be Ed25519")  # noqa: TRY004
        if _raw_public_key(key.public_key()) != _raw_public_key(certificate.public_key()):
            raise ValueError("intermediate signing key does not match certificate")
        self._key = key
        self._certificate = certificate
        self._validate_intermediate(datetime.now(UTC))

    def issue_node(self, node_id: str, csr_pem: bytes, now: datetime) -> IssuedCertificate:
        timestamp = _utc_timestamp(now)
        self._validate_intermediate(timestamp)
        if _NODE_ID.fullmatch(node_id) is None:
            raise ValueError("node ID must be a canonical spk_<32 lowercase hex characters> value")
        public_key = _load_node_csr(node_id, csr_pem).public_key()
        not_after = timestamp + _CERTIFICATE_LIFETIME
        certificate = (
            x509.CertificateBuilder()
            .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, node_id)]))
            .issuer_name(self._certificate.subject)
            .public_key(public_key)
            .serial_number(x509.random_serial_number())
            .not_valid_before(timestamp)
            .not_valid_after(not_after)
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .add_extension(
                x509.KeyUsage(
                    digital_signature=True,
                    content_commitment=False,
                    key_encipherment=False,
                    data_encipherment=False,
                    key_agreement=False,
                    key_cert_sign=False,
                    crl_sign=False,
                    encipher_only=False,
                    decipher_only=False,
                ),
                critical=True,
            )
            .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.CLIENT_AUTH]), critical=True)
            .add_extension(
                x509.SubjectAlternativeName(
                    [x509.UniformResourceIdentifier(f"spiffe://vonk-forge.local/node/{node_id}")]
                ),
                critical=False,
            )
            .sign(self._key, algorithm=None)
        )
        return IssuedCertificate(
            node_id=node_id,
            certificate_pem=certificate.public_bytes(serialization.Encoding.PEM),
            chain_pem=self._certificate.public_bytes(serialization.Encoding.PEM),
            serial=str(certificate.serial_number),
            fingerprint=certificate.fingerprint(hashes.SHA256()).hex(),
            not_before=timestamp,
            not_after=not_after,
        )

    def renew_node(
        self,
        node_id: str,
        csr_pem: bytes,
        now: datetime,
        *,
        request_id: str,
    ) -> IssuedCertificate:
        _validate_provider_request_id(request_id)
        return self.issue_node(node_id, csr_pem, now)

    def revoke_node(self, serial: str, now: datetime) -> None:
        # The built-in provider has no durable CA database. Vonk Forge's local
        # certificate row is the immediate revocation authority for ingress.
        if not serial.isdecimal() or int(serial) <= 0:
            raise ValueError("certificate serial must be a positive decimal integer")
        _utc_timestamp(now)

    def revocation_bundle(self, now: datetime) -> bytes:
        timestamp = _utc_timestamp(now)
        self._validate_intermediate(timestamp)
        crl = (
            x509.CertificateRevocationListBuilder()
            .issuer_name(self._certificate.subject)
            .last_update(timestamp)
            .next_update(timestamp + _CERTIFICATE_LIFETIME)
            .sign(self._key, algorithm=None)
        )
        return crl.public_bytes(serialization.Encoding.PEM)

    def _validate_intermediate(self, now: datetime) -> None:
        try:
            constraints = self._certificate.extensions.get_extension_for_class(x509.BasicConstraints).value
        except x509.ExtensionNotFound as error:
            raise ValueError("intermediate certificate must declare CA constraints") from error
        if not constraints.ca:
            raise ValueError("intermediate certificate must be a CA")
        if constraints.path_length != 0:
            raise ValueError("intermediate certificate path length must be zero")
        try:
            usage = self._certificate.extensions.get_extension_for_class(x509.KeyUsage).value
        except x509.ExtensionNotFound as error:
            raise ValueError("intermediate certificate must permit certificate and CRL signing") from error
        if not usage.key_cert_sign or not usage.crl_sign:
            raise ValueError("intermediate certificate must permit certificate and CRL signing")
        if self._certificate.not_valid_before_utc > now:
            raise ValueError("intermediate certificate is not yet valid")
        if self._certificate.not_valid_after_utc - now < _MINIMUM_INTERMEDIATE_LIFETIME:
            raise ValueError("intermediate certificate must have at least seven days remaining")


def _read_regular_secret_file(path_value: Path | str) -> bytes:
    path = os.fspath(path_value)
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise ValueError("intermediate material must be a regular non-symlink file") from error
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise ValueError("intermediate material must be a regular non-symlink file")
        chunks: list[bytes] = []
        while block := os.read(descriptor, 65536):
            chunks.append(block)
        return b"".join(chunks)
    finally:
        os.close(descriptor)


def _raw_public_key(key: ed25519.Ed25519PublicKey) -> bytes:
    return key.public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)


def _load_node_csr(node_id: str, csr_pem: bytes) -> x509.CertificateSigningRequest:
    try:
        request = x509.load_pem_x509_csr(csr_pem)
    except (TypeError, ValueError) as error:
        raise ValueError("node CSR must be valid PEM") from error
    if not request.is_signature_valid:
        raise ValueError("node CSR signature is invalid")
    expected_subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, node_id)])
    if request.subject != expected_subject:
        raise ValueError("node CSR subject does not match node identity")
    if len(request.extensions) != 1:
        raise ValueError("node CSR must contain only the node URI SAN extension")
    try:
        sans = request.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    except x509.ExtensionNotFound as error:
        raise ValueError("node CSR must contain the node URI SAN") from error
    expected_sans = x509.SubjectAlternativeName(
        [x509.UniformResourceIdentifier(f"spiffe://vonk-forge.local/node/{node_id}")]
    )
    if sans != expected_sans:
        raise ValueError("node CSR URI SAN does not match node identity")
    public_key = request.public_key()
    if not isinstance(public_key, ed25519.Ed25519PublicKey):
        raise ValueError("node CSR public key must be Ed25519")  # noqa: TRY004
    return request


def _validate_provider_request_id(request_id: str) -> None:
    if _PROVIDER_REQUEST_ID.fullmatch(request_id) is None:
        raise ValueError("provider request ID must be a 43-character base64url value")


def _utc_timestamp(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamp must be timezone-aware")
    return value.astimezone(UTC).replace(microsecond=0)
