# -*- coding: utf-8 -*-
"""全球市场数据层：美股 / 港股 / 全球指数 / 外汇 / 国际商品 / 国内期货

数据源：新浪财经（实时 hq.sinajs.cn + 历史 stock.finance.sina.com.cn）
沙箱内已验证：美股日K(1984-)、外汇日K(1994-)、国际商品日K(1996-)、
国内期货日K(2008-)；港股与全球指数仅有实时行情。
"""
import datetime as dt
import json
import re
import threading

from . import datasource as ds

_KLINE_URLS = {
    "us": "https://stock.finance.sina.com.cn/usstock/api/json_v2.php/US_MinKService.getDailyK?symbol={sym}",
    "fx": "https://vip.stock.finance.sina.com.cn/forex/api/jsonp.php/var%20t=/NewForexService.getDayKLine?symbol={sym}",
    "cmd": "https://stock2.finance.sina.com.cn/futures/api/jsonp.php/var%20t=/GlobalFuturesService.getGlobalFuturesDailyKLine?symbol={sym}",
    "cnf": "https://stock2.finance.sina.com.cn/futures/api/jsonp.php/var%20t=/InnerFuturesNewService.getDailyKLine?symbol={sym}",
    # fxh：Frankfurter（双占位符 base/quote，见 get_kline 内 format）
    "fxh": "https://api.frankfurter.app/1999-01-04..?from={b}&to={q}",
}
_CAT_NAME = {"us": "美股", "hk": "港股", "idx": "全球指数", "fx": "外汇",
             "fxh": "外汇交叉盘", "cmd": "国际商品", "cnf": "国内期货"}

# ---------------------------------------------------------------- 资产目录
# (cat, symbol, 名称, 英文/别名...)——用于搜索与自然语言识别
CATALOG = [
    # 美股
    ("us", "aapl", "苹果", "apple", "AAPL"), ("us", "msft", "微软", "microsoft", "MSFT"),
    ("us", "nvda", "英伟达", "nvidia", "NVDA"), ("us", "tsla", "特斯拉", "tesla", "TSLA"),
    ("us", "googl", "谷歌", "google", "GOOGL"), ("us", "amzn", "亚马逊", "amazon", "AMZN"),
    ("us", "meta", "Meta", "facebook", "脸书", "META"), ("us", "nflx", "奈飞", "netflix", "NFLX"),
    ("us", "amd", "超威半导体", "AMD"), ("us", "intc", "英特尔", "intel", "INTC"),
    ("us", "orcl", "甲骨文", "oracle", "ORCL"), ("us", "avgo", "博通", "broadcom", "AVGO"),
    ("us", "tsm", "台积电", "TSM"), ("us", "jpm", "摩根大通", "jpmorgan", "JPM"),
    ("us", "wmt", "沃尔玛", "walmart", "WMT"), ("us", "ko", "可口可乐", "coca-cola", "KO"),
    ("us", "mcd", "麦当劳", "mcdonald", "MCD"), ("us", "dis", "迪士尼", "disney", "DIS"),
    ("us", "ba", "波音", "boeing", "BA"), ("us", "xom", "埃克森美孚", "exxon", "XOM"),
    # 港股（仅实时）
    ("hk", "00700", "腾讯控股", "腾讯", "TENCENT"), ("hk", "09988", "阿里巴巴-W", "港股阿里"),
    ("hk", "03690", "美团-W", "港股美团"), ("hk", "09618", "京东集团-SW", "港股京东"),
    ("hk", "09888", "百度集团-SW", "港股百度"), ("hk", "01810", "小米集团-W", "港股小米"),
    ("hk", "00388", "香港交易所", "港交所"), ("hk", "01299", "友邦保险", "友邦"),
    ("hk", "00941", "中国移动", "港股中移动"), ("hk", "01211", "比亚迪股份", "港股比亚迪"),
    ("hk", "02015", "理想汽车-W", "港股理想"), ("hk", "09868", "小鹏汽车-W", "港股小鹏"),
    # 全球指数（仅实时）
    ("idx", "int_dji", "道琼斯指数", "道琼斯", "道指", "DJIA"),
    ("idx", "int_nasdaq", "纳斯达克指数", "纳斯达克", "纳指"),
    ("idx", "int_sp500", "标普500", "标普指数", "SP500"),
    ("idx", "b_NKY", "日经225", "日经指数", "日经"),
    ("idx", "b_HSI", "恒生指数", "恒指"),
    ("idx", "b_DAX", "德国DAX", "DAX", "德指"),
    ("idx", "b_UKX", "英国富时100", "富时100", "英富时"),
    ("idx", "b_FTSE", "法国CAC40", "CAC40"),
    ("idx", "b_KOSPI", "韩国KOSPI", "韩综"),
    ("idx", "b_SENSEX", "印度SENSEX", "印度指数"),
    # 外汇（美元指数历史K线暂无数据源，仅实时行情，自动降级）
    ("fx", "fx_susdindex", "美元指数", "美指", "Dollar Index", "DINIW"),
    ("fx", "fx_susdcny", "美元兑人民币", "USDCNY", "人民币汇率"),
    ("fx", "fx_seurusd", "欧元兑美元", "EURUSD", "欧元汇率"),
    ("fx", "fx_susdjpy", "美元兑日元", "USDJPY", "日元汇率"),
    ("fx", "fx_sgbpusd", "英镑兑美元", "GBPUSD", "英镑汇率"),
    ("fx", "fx_saudusd", "澳元兑美元", "AUDUSD", "澳元汇率"),
    ("fx", "fx_susdhkd", "美元兑港元", "USDHKD"),
    # 外汇交叉盘（Frankfurter/欧洲央行每日定盘价，最早1999年至今全历史）
    ("fxh", "EURJPY", "欧元兑日元", "EURJPY", "欧元日元"),
    ("fxh", "GBPJPY", "英镑兑日元", "GBPJPY", "英镑日元"),
    ("fxh", "AUDJPY", "澳元兑日元", "AUDJPY", "澳元日元"),
    ("fxh", "EURCNY", "欧元兑人民币", "EURCNY", "欧元汇率人民币"),
    ("fxh", "HKDCNY", "港元兑人民币", "HKDCNY", "港币汇率"),
    ("fxh", "USDSGD", "美元兑新加坡元", "USDSGD", "新加坡元汇率"),
    ("fxh", "USDCHF", "美元兑瑞郎", "USDCHF", "瑞郎汇率"),
    ("fxh", "USDCAD", "美元兑加元", "USDCAD", "加元汇率"),
    ("fxh", "NZDUSD", "纽元兑美元", "NZDUSD", "新西兰元汇率"),
    ("fxh", "USDKRW", "美元兑韩元", "USDKRW", "韩元汇率"),
    ("fxh", "USDINR", "美元兑印度卢比", "USDINR", "卢比汇率"),
    ("fxh", "EURGBP", "欧元兑英镑", "EURGBP"),
    # 国际商品
    ("cmd", "CL", "纽约原油", "WTI原油", "美原油", "原油"),
    ("cmd", "OIL", "布伦特原油", "布油", "Brent"),
    ("cmd", "GC", "COMEX黄金", "纽约黄金", "国际金价", "黄金", "金价"),
    ("cmd", "SI", "COMEX白银", "国际白银", "白银", "银价"),
    ("cmd", "HG", "COMEX铜", "国际铜价", "铜价"),
    ("cmd", "NG", "天然气", "美国天然气"),
    # 国内期货
    ("cnf", "AU0", "沪金连续", "沪金", "上海黄金期货"),
    ("cnf", "AG0", "沪银连续", "沪银", "上海白银期货"),
    ("cnf", "CU0", "沪铜连续", "沪铜"),
    ("cnf", "RB0", "螺纹钢连续", "螺纹钢", "螺纹"),
    ("cnf", "HC0", "热卷连续", "热轧卷板"),
    ("cnf", "I0", "铁矿石连续", "铁矿石"),
    ("cnf", "M0", "豆粕连续", "豆粕"),
    ("cnf", "TA0", "PTA连续", "PTA"),
]

# 搜索/识别索引：词条 -> (cat, symbol)；按长度降序匹配
_TERM_MAP = {}
for cat, sym, name, *alias in CATALOG:
    for w in (name,) + tuple(a for a in alias if a):
        _TERM_MAP[w] = (cat, sym)
GLOBAL_TERMS = sorted(_TERM_MAP.keys(), key=lambda x: -len(x))

_kline_cache = {}          # (cat,sym) -> (ts, rows)
_kline_lock = threading.Lock()
_KLINE_TTL = 4 * 3600


def search_global(q, limit=8):
    """目录搜索：名称/别名/symbol 匹配，附实时行情"""
    q = (q or "").strip().lower()
    if not q:
        return []
    out = []
    seen = set()
    for w in sorted(_TERM_MAP.keys(), key=lambda x: (-len(x), x)):
        if q in w.lower() or q in w or w.lower() in q:
            cat, sym = _TERM_MAP[w]
            key = (cat, sym)
            if key in seen:
                continue
            seen.add(key)
            meta = _asset_meta(cat, sym)
            try:
                qt = get_quote(cat, sym)
            except Exception:  # noqa: BLE001
                qt = {}
            out.append(dict(cat=cat, cat_name=_CAT_NAME[cat], symbol=sym,
                            name=meta["name"], price=qt.get("price"), pct=qt.get("pct")))
            if len(out) >= limit:
                break
    return out


def resolve_global_in_text(q):
    """从自然语言中识别全球资产（最长名优先）；返回 (cat, symbol, name) 或 None"""
    for w in GLOBAL_TERMS:
        if w in q:
            cat, sym = _TERM_MAP[w]
            return cat, sym, _asset_meta(cat, sym)["name"]
    return None


def _asset_meta(cat, sym):
    for c, s, name, *_ in CATALOG:
        if c == cat and s == sym:
            return dict(cat=c, symbol=s, name=name, cat_name=_CAT_NAME[c])
    return dict(cat=cat, symbol=sym, name=sym, cat_name=_CAT_NAME.get(cat, cat))


# ---------------------------------------------------------------- 实时行情

def get_quote(cat, sym):
    """统一实时行情：price/open/high/low/pct/high52/low52(美股)"""
    if cat == "fxh":
        # Frankfurter/ECB 每日定盘价：无盘中高低点，K线以收盘变动生成蜡烛
        rows = get_kline(cat, sym)
        last, prev = rows[-1], (rows[-2] if len(rows) > 1 else rows[-1])
        pct = round((last["close"] / prev["close"] - 1) * 100, 2) \
            if last["close"] and prev["close"] else None
        return dict(cat=cat, cat_name=_CAT_NAME[cat], symbol=sym,
                    name=_asset_meta(cat, sym)["name"], price=last["close"], pct=pct,
                    open=last["open"], high=last["high"], low=last["low"],
                    prev_close=prev["close"], time=last["day"])
    codes = {"us": f"gb_{sym}", "hk": f"rt_hk{sym}", "idx": sym,
             "fx": "DINIW" if sym == "fx_susdindex" else (sym if sym.startswith("fx_") else f"fx_{sym}"),
             "cmd": f"hf_{sym}", "cnf": f"nf_{sym}"}[cat]
    txt = ds._get(f"https://hq.sinajs.cn/list={codes}").text
    m = re.search(r'="([^"]*)"', txt)
    if not m or not m.group(1):
        raise RuntimeError(f"{_asset_meta(cat, sym)['name']} 行情获取失败")
    f = m.group(1).split(",")
    meta = _asset_meta(cat, sym)
    if cat == "us":
        # 苹果,349.75,0.44%,时间,涨跌额,开盘,最高,最低,...
        return dict(cat=cat, cat_name=meta["cat_name"], symbol=sym, name=f[0],
                    price=_ff(f[1]), pct=_ff(f[2]), open=_ff(f[5]), high=_ff(f[6]),
                    low=_ff(f[7]), prev_close=_ff(f[26]) if len(f) > 26 else None,
                    time=f[3])
    if cat == "hk":
        # 英文名,中文名,开盘,昨收,最高,最低,现价,涨跌,涨跌%,买一,卖一,成交额,成交量,...
        return dict(cat=cat, cat_name=meta["cat_name"], symbol=sym, name=f[1],
                    price=_ff(f[6]), pct=_ff(f[8]), open=_ff(f[2]), high=_ff(f[4]),
                    low=_ff(f[5]), prev_close=_ff(f[3]), time=dt.datetime.now().strftime("%Y-%m-%d %H:%M"))
    if cat == "idx":
        # 名称,点位,涨跌,涨跌%
        return dict(cat=cat, cat_name=meta["cat_name"], symbol=sym, name=f[0],
                    price=_ff(f[1]), pct=_ff(f[3]), change=_ff(f[2]),
                    time=dt.datetime.now().strftime("%Y-%m-%d %H:%M"))
    if cat == "fx":
        # 时间,买价,卖价,昨收,...,开,高,低,名称,涨跌%（DINIW 无涨跌幅字段，末位为日期）
        price = _ff(f[1])
        pct = _ff(f[10]) if len(f) > 10 else None
        prev = _ff(f[3])
        if pct is None and price and prev:
            pct = round((price / prev - 1) * 100, 2)
        return dict(cat=cat, cat_name=meta["cat_name"], symbol=sym,
                    name=f[9] if len(f) > 9 and f[9] else meta["name"],
                    price=price, pct=pct,
                    open=_ff(f[6]) if len(f) > 6 else None, high=_ff(f[7]) if len(f) > 7 else None,
                    low=_ff(f[8]) if len(f) > 8 else None, prev_close=prev, time=f[0])
    if cat == "cmd":
        # 现价,,买价,卖价,最高,最低,时间,昨结,开盘,持仓,买量,卖量,日期,名称
        pct = None
        if _ff(f[0]) and _ff(f[7]):
            pct = round((_ff(f[0]) / _ff(f[7]) - 1) * 100, 2)
        return dict(cat=cat, cat_name=meta["cat_name"], symbol=sym,
                    name=f[13] if len(f) > 13 and f[13] else meta["name"],
                    price=_ff(f[0]), pct=pct, open=_ff(f[8]), high=_ff(f[4]),
                    low=_ff(f[5]), prev_close=_ff(f[7]), time=f"{f[12]} {f[6]}")
    # cnf: 名称,时间,开,最高,最低,?,买价,卖价,现价,?,昨结,买量,卖量,持仓,...
    price = _ff(f[8]) if len(f) > 8 else None
    prev = _ff(f[10]) if len(f) > 10 else None
    pct = round((price / prev - 1) * 100, 2) if price and prev else None
    return dict(cat=cat, cat_name=meta["cat_name"], symbol=sym, name=f[0] or meta["name"],
                price=price, pct=pct, open=_ff(f[2]), high=_ff(f[3]), low=_ff(f[4]),
                prev_close=prev, time=dt.datetime.now().strftime("%Y-%m-%d %H:%M"))


def _ff(x):
    try:
        v = float(x)
        return v if v == v else None
    except (TypeError, ValueError, IndexError):
        return None


# ---------------------------------------------------------------- 历史日K

def get_kline(cat, sym, limit=None):
    """历史日K rows=[{day,open,high,low,close,volume}]；无历史源的类别抛 RuntimeError"""
    if cat not in _KLINE_URLS:
        raise RuntimeError(f"{_CAT_NAME.get(cat, cat)}暂无历史K线数据源（仅有实时行情）")
    key = (cat, sym)
    now = dt.datetime.now().timestamp()
    with _kline_lock:
        hit = _kline_cache.get(key)
        if hit and now - hit[0] < _KLINE_TTL:
            rows = hit[1]
        else:
            if cat == "fxh":
                url = _KLINE_URLS[cat].format(b=sym[:3], q=sym[3:])
            else:
                url = _KLINE_URLS[cat].format(sym=sym)
            txt = ds._get(url).text
            rows = _parse_kline(cat, txt)
            if not rows:
                raise RuntimeError(f"{_asset_meta(cat, sym)['name']} 无K线数据（symbol 可能不存在）")
            _kline_cache[key] = (now, rows)
    return rows[-limit:] if limit else rows


def _parse_kline(cat, txt):
    m0 = re.search(r"\((\[.*\])\)", txt, re.S)
    if cat == "fxh":
        # Frankfurter 纯 JSON：{"rates": {"1999-01-04": {"JPY": 133.73}, ...}}
        # ECB 每日一个定盘价：开=前收，高/低=开收极值，生成有意义的蜡烛
        try:
            rates = json.loads(txt).get("rates") or {}
        except ValueError:
            return []
        rows = []
        prev_c = None
        for d_ in sorted(rates.keys()):
            vals = rates[d_]
            c = _ff(list(vals.values())[0]) if vals else None
            if not c or c <= 0:
                continue
            o = prev_c if prev_c else c
            rows.append(dict(day=d_, open=o, high=max(o, c), low=min(o, c),
                             close=c, volume=0.0))
            prev_c = c
        return rows
    if cat in ("us", "cnf"):
        body = m0.group(1) if m0 else txt  # us 为纯JSON，cnf 为 jsonp 包裹
        try:
            arr = json.loads(body)
            return [dict(day=x["d"], open=float(x["o"]), high=float(x["h"]),
                         low=float(x["l"]), close=float(x["c"]), volume=float(x.get("v") or 0))
                    for x in arr if x.get("c")]
        except (ValueError, KeyError):
            return []
    m = re.search(r"\((\[.*\]|\".*\")\)", txt, re.S)
    body = m.group(1) if m else txt
    rows = []
    if cat == "fx":
        for seg in body.strip('"').split("|"):
            p = seg.split(",")
            if len(p) >= 5 and (p[0][:2] in ("19", "20")):
                try:
                    rows.append(dict(day=p[0], open=float(p[1]), high=float(p[3]),
                                     low=float(p[2]), close=float(p[4]), volume=0.0))
                except (ValueError, IndexError):
                    continue
    else:  # cmd: JSON 数组 [{"date","open","high","low","close","volume"}]
        try:
            arr = json.loads(body)
            for x in arr:
                rows.append(dict(day=x.get("date") or x.get("d"),
                                 open=float(x["open"]), high=float(x["high"]),
                                 low=float(x["low"]), close=float(x["close"]),
                                 volume=float(x.get("volume") or 0)))
        except (ValueError, KeyError, TypeError):
            return []
    rows = [r for r in rows if r.get("day") and r.get("close") and r["close"] > 0]
    rows.sort(key=lambda r: r["day"])
    return rows


# ---------------------------------------------------------------- 总览

def overview():
    """全球市场一屏总览"""
    groups = {"indices": [], "commodities": [], "forex": [], "stocks": []}
    pick = [("idx", "int_dji"), ("idx", "int_nasdaq"), ("idx", "int_sp500"), ("idx", "b_NKY"),
            ("idx", "b_HSI"), ("idx", "b_DAX"), ("idx", "b_UKX"),
            ("cmd", "CL"), ("cmd", "GC"), ("cmd", "SI"), ("cmd", "HG"), ("cmd", "NG"),
            ("fx", "fx_susdindex"), ("fx", "fx_susdcny"), ("fx", "fx_seurusd"),
            ("fx", "fx_susdjpy"), ("fx", "fx_sgbpusd"),
            ("fxh", "EURJPY"), ("fxh", "HKDCNY"), ("fxh", "NZDUSD"),
            ("us", "aapl"), ("us", "nvda"), ("us", "tsla"), ("us", "msft"),
            ("hk", "00700"), ("hk", "09988"), ("hk", "01810")]
    for cat, sym in pick:
        try:
            q = get_quote(cat, sym)
        except Exception:  # noqa: BLE001
            continue
        if cat == "idx":
            groups["indices"].append(q)
        elif cat in ("cmd", "cnf"):
            groups["commodities"].append(q)
        elif cat in ("fx", "fxh"):
            groups["forex"].append(q)
        else:
            groups["stocks"].append(q)
    return dict(date=dt.datetime.now().strftime("%Y-%m-%d %H:%M"), **groups)


def asset_analysis(cat, sym):
    """全球资产技术面：复用 A 股量化引擎（指标/信号/评分对 OHLCV 通用）"""
    from . import analysis
    meta = _asset_meta(cat, sym)
    quote = get_quote(cat, sym)
    out = dict(meta=meta, quote=quote)
    try:
        rows = get_kline(cat, sym)
        df = analysis.build_df(rows)
        if len(df) >= 30:
            ctx = analysis.daily_analysis(sym, meta["name"], df)
            out["daily"] = dict(
                date=ctx.get("date"),
                indicators=ctx.get("indicators"), stats=ctx.get("stats"),
                support_resistance=ctx.get("support_resistance"), signals=ctx.get("signals"))
            out["history_days"] = len(df)
            out["first_day"] = rows[0]["day"]
    except RuntimeError as e:
        out["kline_error"] = str(e)
    return out
