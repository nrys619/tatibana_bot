"""e支店API v4r9 公開鍵認証用の暗号処理.

ログインレスポンスの仮想URL (sUrlRequest 等) は、口座に登録した公開鍵で
RSA暗号化された上でBase64エンコードされて返る。ここでは手元の秘密鍵
(PEM) で復号する。方式は RSA PKCS#1 OAEP / SHA-256 (公式サンプル準拠)。
"""

from __future__ import annotations

import base64
from pathlib import Path

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPrivateKey


def load_private_key(pem_path: str | Path, password: bytes | None = None) -> RSAPrivateKey:
    """PEM形式の秘密鍵 (e_api_private_key.pem) を読み込む."""
    data = Path(pem_path).read_bytes()
    key = serialization.load_pem_private_key(data, password=password)
    if not isinstance(key, RSAPrivateKey):
        raise ValueError(f"not an RSA private key: {pem_path}")
    return key


def decrypt_field(encrypted_b64: str, private_key: RSAPrivateKey) -> str:
    """Base64+RSA(OAEP/SHA-256) で暗号化されたフィールドを復号して文字列で返す."""
    cleaned = encrypted_b64.strip().replace('"', "")
    ciphertext = base64.b64decode(cleaned)
    plaintext = private_key.decrypt(
        ciphertext,
        padding.OAEP(
            mgf=padding.MGF1(algorithm=hashes.SHA256()),
            algorithm=hashes.SHA256(),
            label=None,
        ),
    )
    # 公式サンプルは utf-8-sig でデコードしている (BOM対策)
    return plaintext.decode("utf-8-sig").strip()


def read_auth_id(path: str | Path) -> str:
    """認証IDファイル (e_api_authid.txt) を読み込む."""
    return Path(path).read_text(encoding="utf-8-sig").strip()
