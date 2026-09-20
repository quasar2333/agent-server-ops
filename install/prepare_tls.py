"""Prepare private TLS files and an encrypted enrollment receipt, offline.

Run from an administrator session after install.py. Requires cryptography.
No ports, service definitions, config or existing credentials are changed.
The recipient public key must arrive through a trusted administrator channel.
"""
import argparse
from datetime import datetime, timedelta, timezone
import hashlib
import ipaddress
import json
from pathlib import Path
import re
import secrets

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa, x25519
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

from agent_server_ops.config import private_write


def derive(shared):
    return HKDF(algorithm=hashes.SHA256(), length=32, salt=None,
                info=b"agent-server-ops-enrollment-v1").derive(shared)


def prepare(root, hostname, recipient_hex):
    root = Path(root).resolve()
    if not re.fullmatch(r"[a-z0-9](?:[a-z0-9.-]{0,251}[a-z0-9])?", hostname):
        raise ValueError("Expected a lowercase DNS hostname")
    recipient = x25519.X25519PublicKey.from_public_bytes(bytes.fromhex(recipient_hex))
    ephemeral = x25519.X25519PrivateKey.generate()
    shared = ephemeral.exchange(recipient)  # Reject invalid/low-order keys before writes.
    cfg = json.loads((root / "gateway.json").read_text(encoding="utf-8"))
    token = (root / "gateway.token").read_text(encoding="utf-8").strip().encode()
    if len(token) < 32 or hashlib.sha256(token).hexdigest() != cfg["token_sha256"]:
        raise ValueError("Gateway token does not match config")
    dest = root / "tls"
    dest.mkdir(mode=0o700)  # Never replace an existing TLS identity, even after partial failure.
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, hostname)])
    now = datetime.now(timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
            .public_key(key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(now - timedelta(minutes=5)).not_valid_after(now + timedelta(days=365))
            .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
            .add_extension(x509.KeyUsage(True, False, True, False, False, True, True, None, None), critical=True)
            .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
            .add_extension(x509.SubjectAlternativeName([x509.DNSName(hostname), x509.DNSName("localhost"),
                            x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]), critical=False)
            .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
            .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(key.public_key()), critical=False)
            .sign(key, hashes.SHA256()))
    private_write(dest / "key.pem", key.private_bytes(serialization.Encoding.PEM,
                  serialization.PrivateFormat.PKCS8, serialization.NoEncryption()).decode())
    private_write(dest / "cert.pem", cert.public_bytes(serialization.Encoding.PEM).decode())
    fingerprint = cert.fingerprint(hashes.SHA256())
    nonce = secrets.token_bytes(12)
    encrypted = AESGCM(derive(shared)).encrypt(nonce, token, fingerprint)
    receipt = {"format": "agent-server-ops-enrollment-v1", "hostname": hostname,
               "certSha256": fingerprint.hex(),
               "ephemeral": ephemeral.public_key().public_bytes_raw().hex(),
               "nonce": nonce.hex(), "ciphertext": encrypted.hex()}
    private_write(dest / "enrollment.json", json.dumps(receipt, indent=2) + "\n")
    return receipt


def decrypt(receipt, recipient_private_bytes):
    if receipt["format"] != "agent-server-ops-enrollment-v1":
        raise ValueError("Unknown enrollment format")
    key = x25519.X25519PrivateKey.from_private_bytes(recipient_private_bytes)
    peer = x25519.X25519PublicKey.from_public_bytes(bytes.fromhex(receipt["ephemeral"]))
    return AESGCM(derive(key.exchange(peer))).decrypt(bytes.fromhex(receipt["nonce"]),
            bytes.fromhex(receipt["ciphertext"]), bytes.fromhex(receipt["certSha256"]))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--hostname", required=True)
    parser.add_argument("--recipient", required=True, help="X25519 recipient public key, 64 hex digits")
    args = parser.parse_args()
    print(json.dumps(prepare(args.root, args.hostname, args.recipient), indent=2))
