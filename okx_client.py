# okx_client.py
import base64
import hashlib
import hmac
import json
import time
from typing import Any, Dict, Optional, List

import requests


class OkxClient:
    """
    Minimal OKX REST client (Spot).
    - Auth: HMAC-SHA256 (OKX v5)
    - Simulated trading: set header 'x-simulated-trading': '1' when enabled
    - Rate limit: very light retry logic
    """

    def __init__(
        self,
        api_key: str,
        api_secret: str,
        passphrase: str,
        simulated: bool = True,
        base_url: str = "https://www.okx.com",
        timeout: int = 20,
        max_retries: int = 2,
        retry_backoff: float = 0.5,
    ):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.api_secret = api_secret.encode()
        self.passphrase = passphrase
        self.simulated = simulated
        self.timeout = timeout
        self.max_retries = max_retries
        self.retry_backoff = retry_backoff
        self._session = requests.Session()

    # ---------- low-level helpers ----------
    @staticmethod
    def _timestamp() -> str:
        # RFC3339/ISO8601 in milliseconds, e.g. 2020-12-08T09:08:57.715Z
        return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()) + f".{int((time.time()%1)*1000):03d}Z"

    def _sign(self, ts: str, method: str, path: str, body: str = "") -> str:
        msg = f"{ts}{method.upper()}{path}{body}".encode()
        sig = hmac.new(self.api_secret, msg, hashlib.sha256).digest()
        return base64.b64encode(sig).decode()

    def _headers(self, sign: Optional[str], ts: str) -> Dict[str, str]:
        h = {
            "Content-Type": "application/json",
        }
        if sign is not None:
            h.update(
                {
                    "OK-ACCESS-KEY": self.api_key,
                    "OK-ACCESS-SIGN": sign,
                    "OK-ACCESS-TIMESTAMP": ts,
                    "OK-ACCESS-PASSPHRASE": self.passphrase,
                }
            )
            if self.simulated:
                h["x-simulated-trading"] = "1"
        return h

    def _request(
        self,
        method: str,
        path: str,
        params: Optional[Dict[str, Any]] = None,
        body: Optional[Dict[str, Any]] = None,
        auth: bool = False,
        timeout: Optional[int] = None,
    ) -> Dict[str, Any]:
        url = self.base_url + path
        body_str = json.dumps(body, separators=(",", ":")) if body is not None else ""
        ts = self._timestamp()

        # OKX signs with full request path (including query)
        query = ""
        if params and method.upper() == "GET":
            # requests handles encoding; we sign the same canonicalized querystring
            # Build it ourselves to ensure stable order:
            query_parts = []
            for k in sorted(params.keys()):
                query_parts.append(f"{k}={requests.utils.quote(str(params[k]))}")
            query = "?" + "&".join(query_parts) if query_parts else ""
        sign = self._sign(ts, method, path + query, body_str) if auth else None
        headers = self._headers(sign, ts)

        # retries
        last_exc = None
        for attempt in range(self.max_retries + 1):
            try:
                resp = self._session.request(
                    method=method.upper(),
                    url=url,
                    params=params if method.upper() == "GET" else None,
                    data=body_str if method.upper() != "GET" else None,
                    headers=headers,
                    timeout=timeout or self.timeout,
                )
                # raise on HTTP errors
                resp.raise_for_status()
                data = resp.json()
                # OKX returns {"code":"0","msg":"","data":[...]} on success
                code = data.get("code", "0")
                if code != "0":
                    # sometimes transient; allow retry
                    if attempt < self.max_retries and code in {"50011", "50013", "50026", "51008"}:
                        time.sleep(self.retry_backoff * (attempt + 1))
                        continue
                    raise requests.HTTPError(f"OKX error code={code} msg={data.get('msg')}")
                return data
            except Exception as e:
                last_exc = e
                if attempt < self.max_retries:
                    time.sleep(self.retry_backoff * (attempt + 1))
                    continue
                raise last_exc

    # ---------- public endpoints ----------
    def public_instruments(self, instType: str = "SPOT") -> Dict[str, Any]:
        """
        GET /api/v5/public/instruments
        """
        return self._request("GET", "/api/v5/public/instruments", params={"instType": instType}, auth=False)

    def candles(self, instId: str, bar: str = "1H", limit: int = 200):
        """
        GET /api/v5/market/candles
        Returns a pandas-compatible structure if pandas is installed; else list.
        """
        data = self._request("GET", "/api/v5/market/candles", params={"instId": instId, "bar": bar, "limit": str(limit)}, auth=False)
        arr = data.get("data", []) or []
        try:
            import pandas as pd  # optional
            cols = ["ts", "open", "high", "low", "close", "vol", "volCcy", "volCcyQuote", "confirm"]
            df = pd.DataFrame(arr, columns=cols[: len(arr[0])]) if arr else pd.DataFrame(columns=cols)
            for c in ("open", "high", "low", "close", "vol"):
                if c in df.columns:
                    df[c] = df[c].astype(float)
            if "ts" in df.columns:
                df["ts"] = df["ts"].astype("int64")
                df["time"] = pd.to_datetime(df["ts"], unit="ms", utc=True)
            return df.sort_values("time").reset_index(drop=True)
        except Exception:
            return arr

    def ticker(self, instId: str) -> Dict[str, Any]:
        """
        GET /api/v5/market/ticker
        """
        data = self._request("GET", "/api/v5/market/ticker", params={"instId": instId}, auth=False)
        return (data.get("data") or [{}])[0]

    # ---------- private: account ----------
    def balance(self, ccy: Optional[str] = None) -> float:
        """
        GET /api/v5/account/balance
        If ccy provided, returns available balance for that currency; else returns 0.0
        """
        res = self._request("GET", "/api/v5/account/balance", auth=True)
        details = (res.get("data") or [{}])[0].get("details", []) or []
        if not ccy:
            return 0.0
        for d in details:
            if d.get("ccy") == ccy:
                # OKX returns strings
                return float(d.get("availBal", "0"))
        return 0.0

    # ---------- private: wallet ----------
    def wallet(self):
        """
        GET /api/v5/account/balance
        Returns the list of all currency balances for the current account
        (sim or live, depending on keys/headers).
        """
        res = self._request("GET", "/api/v5/account/balance", auth=True)
        return (res.get("data") or [{}])[0].get("details", []) or []

    # ---------- private: orders ----------
    def place_order(
        self,
        instId: str,
        side: str,
        ordType: str,
        sz: str,
        px: Optional[str] = None,
        tdMode: str = "cash",
        reduceOnly: bool = False,
        clOrdId: Optional[str] = None,
        attach_tp: Optional[float] = None,
        attach_sl: Optional[float] = None,
    ) -> Dict[str, Any]:
        """
        POST /api/v5/trade/order
        - For grid buys, call WITHOUT attach TP/SL (we attach OCO AFTER fill).
        - Supports attachAlgoOrds if you want inline TP/SL for non-grid use.
        """
        body: Dict[str, Any] = {
            "instId": instId,
            "tdMode": tdMode,        # 'cash' for spot
            "side": side,            # 'buy'|'sell'
            "ordType": ordType,      # 'limit'|'market'
            "sz": sz,
        }
        if px is not None:
            body["px"] = px
        if reduceOnly:
            body["reduceOnly"] = "true"
        if clOrdId:
            body["clOrdId"] = clOrdId
        if attach_tp is not None or attach_sl is not None:
            attach: Dict[str, Any] = {}
            if attach_tp is not None:
                attach["tpTriggerPx"] = str(attach_tp)
                attach["tpOrdPx"] = str(attach_tp)
            if attach_sl is not None:
                attach["slTriggerPx"] = str(attach_sl)
                attach["slOrdPx"] = str(attach_sl)
            body["attachAlgoOrds"] = [attach]

        return self._request("POST", "/api/v5/trade/order", body=body, auth=True)

    def place_algo_oco(
        self,
        instId: str,
        side: str,
        sz: str,
        tpTriggerPx: str,
        slTriggerPx: str,
        tpOrdPx: Optional[str] = None,
        slOrdPx: Optional[str] = None,
        tdMode: str = "cash",
    ) -> Dict[str, Any]:
        """
        POST /api/v5/trade/order-algo
        OCO to exit a filled position (bot uses this AFTER buy fills).
        """
        body: Dict[str, Any] = {
            "instId": instId,
            "tdMode": tdMode,
            "side": side,         # usually 'sell' for long exits
            "ordType": "oco",
            "sz": sz,
            "tpTriggerPx": str(tpTriggerPx),
            "slTriggerPx": str(slTriggerPx),
        }
        if tpOrdPx is not None:
            body["tpOrdPx"] = str(tpOrdPx)
        if slOrdPx is not None:
            body["slOrdPx"] = str(slOrdPx)

        # OKX expects a list for algo batch in some cases; single dict also accepted.
        # We send a single dict for simplicity.
        return self._request("POST", "/api/v5/trade/order-algo", body=body, auth=True)

    def cancel_order(self, instId: str, ordId: Optional[str] = None, clOrdId: Optional[str] = None) -> Dict[str, Any]:
        """
        POST /api/v5/trade/cancel-order
        """
        body: Dict[str, Any] = {"instId": instId}
        if ordId:
            body["ordId"] = ordId
        if clOrdId:
            body["clOrdId"] = clOrdId
        return self._request("POST", "/api/v5/trade/cancel-order", body=body, auth=True)

    def cancel_algo(self, algoId: str) -> Dict[str, Any]:
        """
        POST /api/v5/trade/cancel-algo-order
        """
        return self._request("POST", "/api/v5/trade/cancel-algo-order", body=[{"algoId": algoId}], auth=True)

    def order(self, instId: str, ordId: Optional[str] = None, clOrdId: Optional[str] = None) -> Dict[str, Any]:
        """
        GET /api/v5/trade/order
        """
        params: Dict[str, Any] = {"instId": instId}
        if ordId:
            params["ordId"] = ordId
        if clOrdId:
            params["clOrdId"] = clOrdId
        return self._request("GET", "/api/v5/trade/order", params=params, auth=True)

    def orders_pending(self, instId: Optional[str] = None) -> Dict[str, Any]:
        """
        GET /api/v5/trade/orders-pending
        """
        params: Dict[str, Any] = {}
        if instId:
            params["instId"] = instId
        return self._request("GET", "/api/v5/trade/orders-pending", params=params, auth=True)

    def orders_history(self, instType: str = "SPOT", instId: Optional[str] = None, limit: int = 100) -> Dict[str, Any]:
        """
        GET /api/v5/trade/orders-history
        """
        params: Dict[str, Any] = {"instType": instType, "limit": str(limit)}
        if instId:
            params["instId"] = instId
        return self._request("GET", "/api/v5/trade/orders-history", params=params, auth=True)
