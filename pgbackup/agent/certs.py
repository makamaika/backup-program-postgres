"""バックアップエージェント用の自己署名 TLS 証明書生成。"""

from __future__ import annotations

import ipaddress
import os
import socket
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

from ..errors import AgentError


def _general_name(value: str) -> x509.GeneralName:
    try:
        return x509.IPAddress(ipaddress.ip_address(value))
    except ValueError:
        return x509.DNSName(value)


def generate_self_signed_certificate(
    cert_path: Path,
    key_path: Path,
    *,
    common_name: str,
    public_url: str,
    validity_days: int = 3650,
) -> str:
    """自己署名証明書と秘密鍵を生成し、SHA-256 fingerprint を返す。

    クライアントは pairing bundle 内の証明書そのものを CA として信頼するため、
    OS の信頼ストアを変更する必要はない。
    """
    parsed = urlparse(public_url)
    if parsed.scheme != "https" or not parsed.hostname:
        raise AgentError("public_url は https://ホスト:ポート の形式で指定してください")

    cert_path.parent.mkdir(parents=True, exist_ok=True)
    key = ec.generate_private_key(ec.SECP256R1())
    now = datetime.now(timezone.utc)
    subject = x509.Name(
        [
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, "pgbackup"),
            x509.NameAttribute(NameOID.COMMON_NAME, common_name),
        ]
    )
    san_values = {parsed.hostname, "localhost", socket.gethostname()}
    san = [_general_name(value) for value in sorted(v for v in san_values if v)]
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=5))
        .not_valid_after(now + timedelta(days=validity_days))
        .add_extension(x509.SubjectAlternativeName(san), critical=False)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(
            x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False
        )
        .sign(key, hashes.SHA256())
    )

    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    try:
        os.chmod(key_path, 0o600)
        os.chmod(cert_path, 0o644)
    except OSError:
        pass
    return cert.fingerprint(hashes.SHA256()).hex()


def certificate_fingerprint(cert_path: Path) -> str:
    cert = x509.load_pem_x509_certificate(cert_path.read_bytes())
    return cert.fingerprint(hashes.SHA256()).hex()
