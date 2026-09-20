import importlib.util
import json
from pathlib import Path
import ssl
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
import urllib.request

import pytest

pytest.importorskip("cryptography")
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey

spec = importlib.util.spec_from_file_location("prepare_tls", Path(__file__).resolve().parents[1] / "install/prepare_tls.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def test_enrollment_encrypts_token_and_rejects_wrong_key_or_tampering(setup):
    _, _, root = setup
    key = X25519PrivateKey.generate()
    original = (root / "gateway.json").read_bytes()
    receipt = module.prepare(root, "ops.example.com", key.public_key().public_bytes_raw().hex())
    token = (root / "gateway.token").read_text().strip()
    assert token not in json.dumps(receipt)
    assert module.decrypt(receipt, key.private_bytes_raw()).decode() == token
    with pytest.raises(InvalidTag): module.decrypt(receipt, X25519PrivateKey.generate().private_bytes_raw())
    with pytest.raises(InvalidTag): module.decrypt({**receipt, "certSha256": "00" * 32}, key.private_bytes_raw())
    with pytest.raises(FileExistsError): module.prepare(root, "ops.example.com", key.public_key().public_bytes_raw().hex())
    assert (root / "gateway.json").read_bytes() == original


def test_generated_certificate_passes_verified_https_and_wrong_ca_fails(setup, tmp_path):
    _, _, root = setup
    key = X25519PrivateKey.generate()
    module.prepare(root, "ops.example.com", key.public_key().public_bytes_raw().hex())
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200); self.end_headers(); self.wfile.write(b"verified")
        def log_message(self, *args): pass
    server = HTTPServer(("127.0.0.1", 0), Handler)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(root / "tls/cert.pem", root / "tls/key.pem")
    server.socket = context.wrap_socket(server.socket, server_side=True)
    worker = threading.Thread(target=server.serve_forever)
    worker.start()
    try:
        url = f"https://127.0.0.1:{server.server_port}"
        trusted = ssl.create_default_context(cafile=str(root / "tls/cert.pem"))
        with urllib.request.urlopen(url, context=trusted, timeout=3) as response:
            assert response.read() == b"verified"
        with pytest.raises(urllib.error.URLError): urllib.request.urlopen(url, context=ssl.create_default_context(), timeout=3)
    finally:
        server.shutdown(); worker.join(); server.server_close()
