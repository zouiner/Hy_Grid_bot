import base64
import hashlib
import hmac
import time
import requests
import datetime as dt
from typing import Dict, Any, Optional, List

class OkxClient:
    def __init__(self, api_key: str, api_secret: str, passphrase: str, simulated: bool = True):
        self.base_url = "https://www.okx.com"
        self.api_key = api_key
        self.api_secret = api_secret.encode()
        self.passphrase = passphrase
        self.simulated = simulated

    # ---- helpers ----
    def _ts(self) -> str:
        return dt.datetime.utcnow().strftime('%Y-%m-%dT%H:%M:%S.%f')[:-3] + 'Z'

    def _sign(self, ts: str, method: str, path: str, body: str = "") -> str:
        msg = f"{ts}{method.upper()}{path}{body}".encode()
        sig = hmac.new(self.api_secret, msg, hashlib.sha256).digest()
        return base64.b64encode(sig).decode()

    def _headers(self, sign: str, ts: str) -> Dict[str, str]:
        h = {
            'OK-ACCESS-KEY': self.api_key,
            'OK-ACCESS-SIGN': sign,
            'OK-ACCESS-TIMESTAMP': ts,
            'OK-ACCESS-PASSPHRASE': self.passphrase,
            'Content-Type': 'application/json'
        }
        if self.simulated:
            h['x-simulated-trading'] = '1'
        return h

    def _request(self, method: str, path: str, params: Optional[Dict[str, Any]] = None, body: Optional[Dict[str, Any]] = None, auth: bool = False) -> Dict[str, Any]:
        url = self.base_url + path
        body_str = ""
        if body:
            import json
            body_str = json.dumps(body, separators=(',', ':'))
        ts = self._ts()
        sign = self._sign(ts, method, path + ("?" + requests.compat.urlencode(params) if (params and method.upper()=="GET") else ""), body_str if body else "") if auth else ""
        headers = self._headers(sign, ts) if auth else {'Content-Type': 'application/json'}
        if auth and self.simulated:
            headers['x-simulated-trading'] = '1'

        r = requests.request(method, url, params=params if method.upper()=="GET" else None, data=body_str if body else None, headers=headers, timeout=15)
        r.raise_for_status()
        return r.json()

    # ---- market data ----
    def candles(self, instId: str, bar: str = '1H', limit: int = 200):
        path = '/api/v5/market/candles'
        data = self._request('GET', path, params={'instId': instId, 'bar': bar, 'limit': str(limit)})
        arr = data.get('data', [])
        # OKX returns newest first; we convert to ascending DataFrame
        import pandas as pd
        cols = ['ts','open','high','low','close','vol','volCcy','volCcyQuote','confirm','instId']
        df = pd.DataFrame(arr, columns=cols[:len(arr[0])]) if arr else pd.DataFrame(columns=cols)
        for c in ['open','high','low','close','vol']:
            if c in df.columns:
                df[c] = df[c].astype(float)
        if 'ts' in df.columns:
            df['ts'] = df['ts'].astype('int64')
            df['time'] = pd.to_datetime(df['ts'], unit='ms', utc=True)
        return df.sort_values('time').reset_index(drop=True)

    def ticker(self, instId: str):
        path = '/api/v5/market/ticker'
        data = self._request('GET', path, params={'instId': instId})
        return data.get('data', [{}])[0]

    # ---- trading (spot cash) ----
    def place_order(self, instId: str, side: str, ordType: str, sz: str, px: Optional[str] = None, tdMode: str = 'cash', reduceOnly: bool = False, clOrdId: Optional[str] = None):
        path = '/api/v5/trade/order'
        body = {
            'instId': instId,
            'tdMode': tdMode,
            'side': side,               # 'buy' | 'sell'
            'ordType': ordType,         # 'market' | 'limit'
            'sz': sz,
        }
        if px: body['px'] = px
        if reduceOnly: body['reduceOnly'] = 'true'
        if clOrdId: body['clOrdId'] = clOrdId
        return self._request('POST', path, body=body, auth=True)

    def cancel_order(self, instId: str, ordId: Optional[str] = None, clOrdId: Optional[str] = None):
        path = '/api/v5/trade/cancel-order'
        body = {'instId': instId}
        if ordId: body['ordId'] = ordId
        if clOrdId: body['clOrdId'] = clOrdId
        return self._request('POST', path, body=body, auth=True)

    def balance(self, ccy: str = 'USDT'):
        path = '/api/v5/account/balance'
        res = self._request('GET', path, auth=True)
        details = res.get('data', [{}])[0].get('details', [])
        for d in details:
            if d.get('ccy') == ccy:
                return float(d.get('availBal', '0'))
        return 0.0