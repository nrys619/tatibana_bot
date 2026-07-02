"""立花証券 e支店 API のセッション管理 (ログイン/リクエスト送信/ログアウト).

e支店APIの仕様:
- ログインURLに認証情報をURLエンコードしたJSONで送ると、
  以降のリクエスト用の「仮想URL」(request/master/price/event) が返る
- 各リクエストは p_no (連番) と p_sd_date (送信時刻) を含むJSONを
  URLエンコードしてGETする

注意: sCLMID等の電文名・APIバージョンは立花証券の公式API仕様書
(最新版) と突き合わせて config の api.version と合わせること。
"""

from __future__ import annotations

import json
import logging
import threading
import urllib.parse
from datetime import datetime
from typing import Any

import requests

logger = logging.getLogger(__name__)


class TachibanaApiError(RuntimeError):
    pass


def _now_str() -> str:
    return datetime.now().strftime("%Y.%m.%d-%H:%M:%S.%f")[:-3]


class TachibanaSession:
    def __init__(
        self,
        base_url: str,
        version: str,
        user_id: str,
        password: str,
        timeout_sec: float = 10.0,
    ):
        self._base_url = base_url.rstrip("/")
        self._version = version
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
        data = self._send(
            auth_url,
            {
                "sCLMID": "CLMAuthLoginRequest",
                "sUserId": self._user_id,
                "sPassword": self._password,
            },
        )
        self.url_request = data.get("sUrlRequest")
        self.url_master = data.get("sUrlMaster")
        self.url_price = data.get("sUrlPrice")
        self.url_event = data.get("sUrlEvent")
        if not self.url_request or not self.url_price:
            raise TachibanaApiError(f"login response missing virtual URLs: {data}")
        logger.info("Tachibana API login OK (env=%s)", self._base_url)

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
