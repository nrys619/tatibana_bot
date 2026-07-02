import base64

import pytest

cryptography = pytest.importorskip("cryptography")

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from tatibana_bot.api.crypto import decrypt_field, load_private_key, read_auth_id


@pytest.fixture(scope="module")
def rsa_key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def _encrypt(plaintext: str, key) -> str:
    """立花側の暗号化 (公開鍵 OAEP/SHA-256 + Base64) を再現する."""
    ciphertext = key.public_key().encrypt(
        plaintext.encode(),
        padding.OAEP(
            mgf=padding.MGF1(algorithm=hashes.SHA256()),
            algorithm=hashes.SHA256(),
            label=None,
        ),
    )
    return base64.b64encode(ciphertext).decode()


def test_decrypt_field_roundtrip(rsa_key):
    url = "https://kabuka.e-shiten.jp/e_api_v4r9/request/abcdef123456/"
    assert decrypt_field(_encrypt(url, rsa_key), rsa_key) == url


def test_decrypt_field_strips_quotes_and_whitespace(rsa_key):
    url = "https://example.com/x/"
    # 引用符・空白まじりでも復号できる (公式サンプルと同じ前処理)
    assert decrypt_field(f'"{_encrypt(url, rsa_key)}"', rsa_key) == url
    assert decrypt_field(f"  {_encrypt(url, rsa_key)}\n", rsa_key) == url


def test_load_private_key_and_auth_id(tmp_path, rsa_key):
    pem = rsa_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    key_path = tmp_path / "e_api_private_key.pem"
    key_path.write_bytes(pem)
    loaded = load_private_key(key_path)
    url = "https://example.com/y/"
    assert decrypt_field(_encrypt(url, loaded), loaded) == url

    auth_path = tmp_path / "e_api_authid.txt"
    auth_path.write_text("﻿AUTH1234\n", encoding="utf-8")
    assert read_auth_id(auth_path) == "AUTH1234"


def test_session_requires_credentials():
    from tatibana_bot.api.session import TachibanaSession

    with pytest.raises(ValueError):
        TachibanaSession(base_url="https://x", version="v")


def test_session_extract_urls_pubkey(tmp_path, rsa_key):
    from tatibana_bot.api.session import TachibanaSession

    pem = rsa_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    key_path = tmp_path / "key.pem"
    key_path.write_bytes(pem)

    session = TachibanaSession(
        base_url="https://demo-kabuka.e-shiten.jp",
        version="e_api_v4r9",
        auth_id="AUTH1234",
        private_key_path=key_path,
    )
    assert session.auth_method == "pubkey"

    data = {
        "sUrlRequest": _encrypt("https://x/request/", rsa_key),
        "sUrlPrice": _encrypt("https://x/price/", rsa_key),
        "sUrlEventWebSocket": _encrypt("wss://x/event/", rsa_key),
    }
    urls = session._extract_urls(data)
    assert urls["sUrlRequest"] == "https://x/request/"
    assert urls["sUrlPrice"] == "https://x/price/"
    assert urls["sUrlEventWebSocket"] == "wss://x/event/"
