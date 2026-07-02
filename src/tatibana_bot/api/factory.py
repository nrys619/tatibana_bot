"""設定と環境変数から TachibanaSession を組み立てる."""

from __future__ import annotations

import os

from tatibana_bot.api.crypto import read_auth_id
from tatibana_bot.api.session import TachibanaSession


def create_session(cfg) -> TachibanaSession:
    """config (api セクション) と環境変数から認証方式を判定してセッションを作る.

    pubkey (v4r9, 推奨):
      TACHIBANA_AUTH_ID_FILE     認証IDファイル (e_api_authid.txt) のパス
      (または TACHIBANA_AUTH_ID  認証ID文字列を直接)
      TACHIBANA_PRIVATE_KEY_FILE 秘密鍵PEM (e_api_private_key.pem) のパス

    password (旧方式):
      TACHIBANA_USER_ID / TACHIBANA_PASSWORD
    """
    base_url = cfg.api.base_urls.get(cfg.api.env)
    method = cfg.api.get("auth_method", "pubkey")

    if method == "pubkey":
        auth_id = os.environ.get("TACHIBANA_AUTH_ID")
        auth_id_file = os.environ.get("TACHIBANA_AUTH_ID_FILE")
        key_file = os.environ.get("TACHIBANA_PRIVATE_KEY_FILE")
        if not auth_id and auth_id_file:
            auth_id = read_auth_id(auth_id_file)
        if not auth_id or not key_file:
            raise RuntimeError(
                "pubkey auth requires TACHIBANA_AUTH_ID(_FILE) and "
                "TACHIBANA_PRIVATE_KEY_FILE (see .env.example)"
            )
        return TachibanaSession(
            base_url=base_url,
            version=cfg.api.version,
            auth_id=auth_id,
            private_key_path=key_file,
            timeout_sec=cfg.api.timeout_sec,
        )

    if method == "password":
        user_id = os.environ.get("TACHIBANA_USER_ID")
        password = os.environ.get("TACHIBANA_PASSWORD")
        if not user_id or not password:
            raise RuntimeError(
                "password auth requires TACHIBANA_USER_ID and TACHIBANA_PASSWORD"
            )
        return TachibanaSession(
            base_url=base_url,
            version=cfg.api.version,
            user_id=user_id,
            password=password,
            timeout_sec=cfg.api.timeout_sec,
        )

    raise ValueError(f"unknown api.auth_method: {method}")
