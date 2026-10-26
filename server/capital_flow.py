# -*- coding: utf-8 -*-
"""资金流向模块：个股资金流、北向持股、龙虎榜

数据源：
- 新浪个股资金流（主力/超大/大/中/小单净流入）
- 东方财富北向持股（持股数、持股市值、占A股比例）
- 东方财富龙虎榜（上榜原因、净买入额）
"""
import datetime as dt
import json
import threading
import time

import requests

from . import datasource as ds

SINA_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Referer": "https://finance.sina.com.cn/",
}

EM_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Referer": "https://data.eastmoney.com/",
}

_cache = {}
_cache_lock = threading.Lock()
TTL = 300


def _sina_get(url, timeout=15):
    return requests.get(url, headers=SINA_HEADERS, timeout=timeout)


def _em_get(url, timeout=15):
    return requests.get(url, headers=EM_HEADERS, timeout=timeout)


def get_stock_money_flow(code, days=10):
    """个股资金流向：主力/超大/大/中/小单净流入"""
    code = ds.norm_code(code)
    sym = ds.market_symbol(code)
    url = (
        "https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/"
        f"MoneyFlow.ssl_qsfx_lscjfb?page=1&num={days}&sort=opendate&asc=0&daima={sym}"
    )
    try:
        r = _sina_get(url)
        data = json.loads(r.text) if r.text.strip() else []
    except Exception:
        return []
    out = []
    for d in data:
        out.append(dict(
            date=d.get("opendate", ""),
            close=_f(d.get("trade")),
            pct=_f(d.get("changeratio"), 4),
            turnover=_f(d.get("turnover"), 2),
            net_amount=_f(d.get("netamount")),  # 主力净流入
            net_ratio=_f(d.get("ratioamount"), 4),
            super_large_net=_f(d.get("r0_net")),  # 超大单净流入
            large_net=_f(d.get("r1_net")),  # 大单净流入
            medium_net=_f(d.get("r2_net")),  # 中单净流入
            small_net=_f(d.get("r3_net")),  # 小单净流入
        ))
    return out


def get_northbound_holding(code):
    """北向资金持股情况"""
    code = ds.norm_code(code)
    url = (
        "https://datacenter-web.eastmoney.com/api/data/v1/get?"
        f"reportName=RPT_MUTUAL_HOLDSTOCKNORTH_STA&columns=ALL"
        f"&filter=(SECURITY_CODE=%22{code}%22)"
        "&pageNumber=1&pageSize=5&sortColumns=TRADE_DATE&sortTypes=-1"
    )
    try:
        r = _em_get(url, timeout=15)
        d = json.loads(r.text)
        res = d.get("result")
        if not res or not res.get("data"):
            return None
        rows = res["data"]
        latest = rows[0]
        return dict(
            date=str(latest.get("TRADE_DATE", ""))[:10],
            name=latest.get("SECURITY_NAME", ""),
            hold_shares=latest.get("HOLD_SHARES"),
            hold_market_cap=latest.get("HOLD_MARKET_CAP"),
            a_shares_ratio=latest.get("A_SHARES_RATIO"),  # 占A股流通比例%
            hold_shares_ratio=latest.get("HOLD_SHARES_RATIO"),
            close_price=latest.get("CLOSE_PRICE"),
            change_rate=latest.get("CHANGE_RATE"),
            total_count=len(rows),
        )
    except Exception:
        return None


def get_dragon_tiger(code):
    """龙虎榜数据（该股近30天上榜记录）"""
    code = ds.norm_code(code)
    url = (
        "https://datacenter-web.eastmoney.com/api/data/v1/get?"
        "reportName=RPT_DAILYBILLBOARD_DETAILSNEW&columns=ALL"
        f"&filter=(SECURITY_CODE=%22{code}%22)"
        "&pageNumber=1&pageSize=10&sortColumns=TRADE_DATE&sortTypes=-1"
    )
    try:
        r = _em_get(url, timeout=15)
        d = json.loads(r.text)
        res = d.get("result")
        if not res or not res.get("data"):
            return []
        rows = res["data"]
        out = []
        for row in rows:
            out.append(dict(
                date=str(row.get("TRADE_DATE", ""))[:10],
                name=row.get("SECURITY_NAME_ABBR", ""),
                close_price=row.get("CLOSE_PRICE"),
                change_rate=row.get("CHANGE_RATE"),
                net_amount=row.get("BILLBOARD_NET_AMT"),  # 龙虎榜净买入
                buy_amount=row.get("BILLBOARD_BUY_AMT"),
                sell_amount=row.get("BILLBOARD_SELL_AMT"),
                deal_amount=row.get("BILLBOARD_DEAL_AMT"),
                reason=row.get("EXPLAIN", ""),
            ))
        return out
    except Exception:
        return []


def _f(v, decimals=2):
    if v is None:
        return None
    try:
        return round(float(v), decimals)
    except (TypeError, ValueError):
        return None


def get_capital_flow(code):
    """综合资金流向数据"""
    code = ds.norm_code(code)
    key = f"flow_{code}"
    now = time.time()
    with _cache_lock:
        if key in _cache and now - _cache[key]["ts"] < TTL:
            return _cache[key]["data"]

    money_flow = get_stock_money_flow(code)
    northbound = get_northbound_holding(code)
    dragon_tiger = get_dragon_tiger(code)

    # 综合判断
    latest_flow = money_flow[0] if money_flow else {}
    main_net = latest_flow.get("net_amount")
    if main_net is not None:
        if main_net > 0:
            flow_signal = "主力净流入"
            flow_verdict = "多"
        elif main_net < 0:
            flow_signal = "主力净流出"
            flow_verdict = "空"
        else:
            flow_signal = "资金平衡"
            flow_verdict = "中性"
    else:
        flow_signal = "数据不可用"
        flow_verdict = "中性"

    result = dict(
        code=code,
        name=ds.get_stock_name(code),
        money_flow=money_flow,
        northbound=northbound,
        dragon_tiger=dragon_tiger,
        flow_signal=flow_signal,
        flow_verdict=flow_verdict,
        latest_net_amount=main_net,
        latest_net_amount_yi=round(main_net / 1e8, 2) if main_net else None,
        update_time=dt.datetime.now().strftime("%Y-%m-%d %H:%M"),
    )
    with _cache_lock:
        _cache[key] = {"ts": now, "data": result}
    return result
