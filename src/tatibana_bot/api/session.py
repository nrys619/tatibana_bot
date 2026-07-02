"""立花証券 e支店 API のセッション管理 (ログイン/リクエスト送信/ログアウト).

対応する認証方式:
- pubkey   (v4r9〜) : 認証ID (sAuthId) + RSA秘密鍵。レスポンスの仮想URLは
                      口座登録済みの公開鍵で暗号化されて返るため秘密鍵で復号する
- password (v4r8以前): sUserId / sPassword (旧方式。電話認証が必要な場合あり)

e支店APIの共通仕様:
- ログインURLに認証情報をURLエンコードしたJSONで送ると、
  以降のリクエスト用の「仮想URL」(request/master/price/event) が返る
- 各リクエストは p_no (連番) と p_sd_date (送信時刻) を含むJSONを
  URLエンコードしてGETする

注意: sCLMID等の電文名・APIバージョンは立花証券の公式API仕様書
(最新版) と突き合わせて config の api.version と合わせること。
デモ環境と本番環境では認証ID・鍵ペアが別物なので混用しないこと。
"""

from __future__ import annotations

import json
import logging
import threading
import urllib.parse
from datetime import datetime
from pathlib import Path
from typing import Any

import requests

logger = logging.getLogger(__name__)

ENCRYPTED_URL_KEYS = [
    "sUrlRequest",
    "sUrlMaster",
    "sUrlPrice",
    "sUrlEvent",
    "sUrlEventWebSocket",
]


class TachibanaApiError(RuntimeError):
    pass


def _now_str() -> str:
    return datetime.now().strftime("%Y.%m.%d-%H:%M:%S.%f")[:-3]


class TachibanaSession:
    def __init__(
        self,
        base_url: str,
        version: str,
        *,
        # --- pubkey認証 (v4r9〜, 推奨) ---
        auth_id: str | None = None,
        private_key_path: str | Path | None = None,
        # --- password認証 (旧方式) ---
        user_id: str | None = None,
        password: str | None = None,
        timeout_sec: float = 10.0,
    ):
        if auth_id and private_key_path:
            self._auth_method = "pubkey"
        elif user_id and password:
            self._auth_method = "password"
        else:
            raise ValueError(
                "auth credentials required: (auth_id + private_key_path) or (user_id + password)"
            )
        self._base_url = base_url.rstrip("/")
        self._version = version
        self._auth_id = auth_id
        self._private_key = None
        if private_key_path:
            from tatibana_bot.api.crypto import load_private_key

            self._private_key = load_private_key(private_key_path)
        self._user_id = user_id
        self._password = password
        self._timeout = timeout_sec
        self._http = requests.Session()
        self._p_no = 0
        self._lock = threading.Lock()
        # ログイン成功時に返る仮想URL
        self.url_request: str | None = None
        self.url_master: str | None = None
        self.url_price: str | None = None
        self.url_event: str | None = None
        self.url_event_websocket: str | None = None

    @property
    def auth_method(self) -> str:
        return self._auth_method

    # ---- low level -------------------------------------------------

    def _next_p_no(self) -> str:
        with self._lock:
            self._p_no += 1
            return str(self._p_no)

    def _send(self, url: str, payload: dict[str, Any]) -> dict[str, Any]:
        body = {
            "p_no": self._next_p_no(),
            "p_sd_date": _now_str(),
            "sJsonOfmt": "4",
            **payload,
        }
        encoded = urllib.parse.quote(json.dumps(body, ensure_ascii=False))
        resp = self._http.get(f"{url}?{encoded}", timeout=self._timeout)
        resp.raise_for_status()
        data = json.loads(resp.text)
        result_code = data.get("sResultCode", data.get("p_errno", "0"))
        if str(result_code) not in ("0", ""):
            raise TachibanaApiError(
                f"API error CLMID={payload.get('sCLMID')} "
                f"code={result_code} text={data.get('sResultText', data.get('p_err', ''))}"
            )
        return data

    # ---- session lifecycle -----------------------------------------

    def login(self) -> None:
        auth_url = f"{self._base_url}/{self._version}/auth/"
        if self._auth_method == "pubkey":
            payload = {"sCLMID": "CLMAuthLoginRequest", "sAuthId": self._auth_id}
        else:
            payload = {
                "sCLMID": "CLMAuthLoginRequest",
                "sUserId": self._user_id,
                "sPassword": self._password,
            }
        data = self._send(auth_url, payload)

        urls = self._extract_urls(data)
        self.url_request = urls.get("sUrlRequest")
        self.url_master = urls.get("sUrlMaster")
        self.url_price = urls.get("sUrlPrice")
        self.url_event = urls.get("sUrlEvent")
        self.url_event_websocket = urls.get("sUrlEventWebSocket")
        if not self.url_request or not self.url_price:
            raise TachibanaApiError(f"login response missing virtual URLs: {list(data)}")
        logger.info("Tachibana API login OK (env=%s, auth=%s)",
                    self._base_url, self._auth_method)

    def _extract_urls(self, data: dict[str, Any]) -> dict[str, str]:
        """仮想URLを取り出す。pubkey認証では秘密鍵で復号する."""
        urls: dict[str, str] = {}
        for key in ENCRYPTED_URL_KEYS:
            raw = data.get(key)
            if not raw:
                continue
            if self._auth_method == "pubkey":
                from tatibana_bot.api.crypto import decrypt_field

                urls[key] = decrypt_field(raw, self._private_key)
            else:
                urls[key] = raw
        return urls

    def logout(self) -> None:
        if self.url_request:
            try:
                self._send(self.url_request, {"sCLMID": "CLMAuthLogoutRequest"})
            except Exception:  # noqa: BLE001 - ログアウト失敗は握りつぶしてよい
                logger.warning("logout failed", exc_info=True)
        self.url_request = None
        logger.info("Tachibana API logged out")

    # ---- typed request helpers -------------------------------------

    def request(self, clmid: str, **params: Any) -> dict[str, Any]:
        """注文・照会系 (仮想URL request) への電文送信."""
        if not self.url_request:
            raise TachibanaApiError("not logged in")
        return self._send(self.url_request, {"sCLMID": clmid, **params})

    def price(self, clmid: str, **params: Any) -> dict[str, Any]:
        """時価情報系 (仮想URL price) への電文送信."""
        if not self.url_price:
            raise TachibanaApiError("not logged in")
        return self._send(self.url_price, {"sCLMID": clmid, **params})

    def master(self, clmid: str, **params: Any) -> dict[str, Any]:
        """マスタ情報系 (仮想URL master) への電文送信."""
        if not self.url_master:
            raise TachibanaApiError("not logged in")
        return self._send(self.url_master, {"sCLMID": clmid, **params})

    def __enter__(self) -> "TachibanaSession":
        self.login()
        return self

    def __exit__(self, *exc: Any) -> None:
        self.logout()
