# -*- coding: utf-8 -*-
"""基本面数据模块：财务指标、估值、同业对比

数据源：
- 新浪财经财务指标页（ROE/ROA/毛利率/净利率/营收增速/利润增速/资产负债率等）
- stocks 表已有 PE/PB/市值/换手率
"""
import datetime as dt
import re
import threading
import time

import requests

from . import datasource as ds, market_data

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Referer": "https://finance.sina.com.cn/",
}

# 关键财务指标映射（新浪表格中的名称 → 我们的字段名）
METRIC_MAP = {
    "摊薄每股收益(元)": "eps",
    "加权每股收益(元)": "eps_weighted",
    "每股净资产_调整前(元)": "bps",
    "每股经营现金流量(元)": "ocfps",
    "净资产收益率(%)": "roe",
    "加权净资产收益率(%)": "roe_weighted",
    "总资产净利润率(%)": "roa",
    "销售净利率(%)": "net_margin",
    "销售毛利率(%)": "gross_margin",
    "营业总收入(元)": "revenue",
    "净利润(元)": "net_profit",
    "扣除非经常性损益后的净利润(元)": "net_profit_excl",
    "净利润增长率(%)": "profit_growth",
    "营业总收入增长率(%)": "revenue_growth",
    "资产负债率(%)": "debt_ratio",
    "流动比率": "current_ratio",
    "速动比率": "quick_ratio",
}

_cache = {}
_cache_lock = threading.Lock()
TTL = 3600  # 财务数据1小时缓存


def _get(url, timeout=20):
    return requests.get(url, headers=HEADERS, timeout=timeout)


def _parse_num(s):
    """解析数值，兼容 str/float/int，'--' 返回 None"""
    if s is None:
        return None
    if isinstance(s, (int, float)):
        return float(s)
    s = str(s).strip().replace(",", "")
    if s in ("", "--", "-", "None"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def fetch_financial_indicators(code):
    """从新浪财务指标页解析最新一期的关键财务指标"""
    code = ds.norm_code(code)
    url = (
        f"https://money.finance.sina.com.cn/corp/go.php/vFD_FinancialGuideLine/"
        f"stockid/{code}/ctrl/{dt.date.today().year}/displaytype/4.phtml"
    )
    try:
        r = _get(url)
        r.encoding = "gb2312"
        html = r.text
    except Exception:
        return {}
    tables = re.findall(r"<table[^>]*>(.*?)</table>", html, re.S)
    target = None
    for t in tables:
        if "净资产收益率" in t or "每股收益" in t:
            target = t
            break
    if not target:
        return {}
    rows = re.findall(r"<tr[^>]*>(.*?)</tr>", target, re.S)
    report_dates = []
    data = {}
    for row in rows:
        cells = re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", row, re.S)
        cells = [re.sub(r"<[^>]+>", "", c).strip() for c in cells]
        if not cells:
            continue
        # 报告日期行
        if cells[0] == "报告日期":
            report_dates = cells[1:]
            continue
        metric = cells[0]
        if metric in METRIC_MAP:
            key = METRIC_MAP[metric]
            # 取最新一期（第一列）
            val = _parse_num(cells[1]) if len(cells) > 1 else None
            if val is not None:
                data[key] = val
            # 同期对比（如果有去年同期）
            if len(cells) > 5:
                # 列顺序: 本期, ... , 去年同期可能在靠后位置
                pass
    if report_dates:
        data["report_date"] = report_dates[0]
    return data


def get_valuation(code):
    """从 stocks 表取估值数据"""
    code = ds.norm_code(code)
    try:
        with ds._db() as conn:
            row = conn.execute(
                "SELECT pe,pb,turnover,mktcap,price,name FROM stocks WHERE code=?", (code,)
            ).fetchone()
        if not row:
            return {}
        pe, pb, turnover, mktcap, price, name = row
        return dict(
            name=name or "",
            pe=_parse_num(pe) if pe else None,
            pb=_parse_num(pb) if pb else None,
            turnover=_parse_num(turnover) if turnover else None,
            mktcap_yi=round(float(mktcap) / 1e4, 2) if mktcap else None,  # 万元→亿元
            price=float(price) if price else None,
        )
    except Exception:
        return {}


def get_industry_peers(code):
    """同业对比：取同市场股票的PE/PB分布，计算分位"""
    code = ds.norm_code(code)
    try:
        with ds._db() as conn:
            row = conn.execute("SELECT pe,pb,mktcap FROM stocks WHERE code=?", (code,)).fetchone()
            if not row:
                return {}
            my_pe, my_pb, _ = row
            # 取全市场有PE的股票做分位
            rows = conn.execute(
                "SELECT pe,pb FROM stocks WHERE pe IS NOT NULL AND pe>0 AND pb IS NOT NULL AND pb>0"
            ).fetchall()
        pes = sorted(float(r[0]) for r in rows if r[0])
        pbs = sorted(float(r[1]) for r in rows if r[1])
        if not pes or not pbs:
            return {}

        def percentile(arr, val):
            if val is None:
                return None
            cnt = sum(1 for x in arr if x <= val)
            return round(cnt / len(arr) * 100, 1)

        my_pe_f = _parse_num(my_pe)
        my_pb_f = _parse_num(my_pb)
        return dict(
            pe_percentile=percentile(pes, my_pe_f),
            pb_percentile=percentile(pbs, my_pb_f),
            pe_median=round(pes[len(pes) // 2], 2),
            pb_median=round(pbs[len(pbs) // 2], 2),
            pe_min=round(pes[0], 2),
            pe_max=round(pes[-1], 2),
            sample_size=len(pes),
        )
    except Exception:
        return {}


def get_fundamental(code):
    """综合基本面数据：财务指标 + 估值 + 同业分位"""
    code = ds.norm_code(code)
    key = f"fund_{code}"
    now = time.time()
    with _cache_lock:
        if key in _cache and now - _cache[key]["ts"] < TTL:
            return _cache[key]["data"]
    fin = fetch_financial_indicators(code)
    # 新浪财务页部分指标缺失时（如银行无销售净利率），用全市场财务快照补齐
    snap = market_data.get_fin_snapshot(code) or {}
    for k in ("roe", "roa", "net_margin", "gross_margin", "debt_ratio",
              "revenue_growth", "profit_growth", "eps", "bps"):
        if fin.get(k) is None and snap.get(k) is not None:
            fin[k] = snap[k]
    val = get_valuation(code)
    peers = get_industry_peers(code)
    result = dict(
        code=code,
        name=val.get("name", ""),
        valuation=val,
        financial=fin,
        peer_comparison=peers,
        update_time=dt.datetime.now().strftime("%Y-%m-%d %H:%M"),
    )
    with _cache_lock:
        _cache[key] = {"ts": now, "data": result}
    return result
