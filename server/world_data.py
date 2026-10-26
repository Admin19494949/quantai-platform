# -*- coding: utf-8 -*-
"""全球金融数据层：10 大资产类别统一接入 + 本地缓存

覆盖范围（免费源，沙箱内已逐个验证可用）：
  cnidx  A股指数       上证/沪深300/创业板/科创50（2010-）
  us     美股个股+ETF  任意美股代码（个股 1984-，ETF 2001-）
  hk     港股          仅实时行情（历史K线源已下线）
  idx    全球指数      仅实时行情
  gf     全球股指期货  标普/纳指/日经/恒指（2016-，作为指数历史代理）
  fx     外汇直盘      美元兑主要货币（1986-）
  fxh    外汇交叉盘    ECB/Frankfurter，30 种货币任意交叉（1999-）
  cmd    国际商品      WTI/布伦特/黄金/白银/铜/天然气（1996-）
  cnf    国内商品期货  沪金沪银沪铜螺纹铁矿豆粕等（2008-）
  crypto 加密货币      BTC/USD（2017-）

统一接口：
  kline(cat, sym, limit)   -> [{day,open,high,low,close,volume}]
  quotes(pairs)            -> [{cat,symbol,name,price,pct,...}]
  coverage()               -> 各类别标的数/历史起点/数据源/局限
  panel(tags, days)        -> DataFrame 收盘价矩阵（列 = "cat:sym"）

诚实声明：港股/全球指数现货仅有实时行情，历史用对应股指期货（gf）或
海外 ETF（us）代理；ETH 等非 BTC 加密无免费历史源；债券收益率无免费历史，
用 TLT/IEF 等长久期 ETF 价格代理（价格涨=利率降）。
"""
import datetime as dt
import json
import math
import threading
import time

import numpy as np
import pandas as pd

from . import datasource as ds, global_mkt

_TRADING_DAYS = 244

# ================================================================ 资产目录

_CAT_NAME = {
    "cnidx": "A股指数", "us": "美股/ETF", "hk": "港股", "idx": "全球指数",
    "gf": "全球股指期货", "fx": "外汇直盘", "fxh": "外汇交叉盘",
    "cmd": "国际商品", "cnf": "国内期货", "crypto": "加密货币",
}

_CAT_META = {
    "cnidx": dict(source="新浪 CN_MarketData", hist="2010-", mode="全历史"),
    "us": dict(source="新浪美股 MinKService", hist="个股1984-/ETF2001-", mode="全历史"),
    "hk": dict(source="新浪港股实时", hist="—", mode="仅实时"),
    "idx": dict(source="新浪全球指数实时", hist="—", mode="仅实时"),
    "gf": dict(source="新浪 GlobalFuturesService", hist="2016-", mode="全历史(代理)"),
    "fx": dict(source="新浪 NewForexService", hist="1986-", mode="全历史"),
    "fxh": dict(source="ECB / Frankfurter", hist="1999-", mode="全历史"),
    "cmd": dict(source="新浪 GlobalFuturesService", hist="1996-", mode="全历史"),
    "cnf": dict(source="新浪 InnerFutures", hist="2008-", mode="全历史"),
    "crypto": dict(source="新浪 GlobalFuturesService", hist="2017-", mode="BTC全历史"),
}

_CAT_LIMIT = {
    "cnidx": "仅 4 条宽基指数",
    "us": "无退市股、无盘前盘后、复权口径由新浪给定",
    "hk": "仅有实时行情，无法回测",
    "idx": "仅有实时行情，历史请用 gf 期货或 us ETF 代理",
    "gf": "期货连续合约，存在展期跳空",
    "fx": "新浪直盘为中间价，非可成交报价",
    "fxh": "ECB 每日定盘价，一天一个价，无盘中高低",
    "cmd": "期货连续合约，存在展期跳空",
    "cnf": "连续合约，主力切换有跳空",
    "crypto": "仅 BTC；7×24 交易，按自然日聚合",
}

# ---- 美股个股（按板块分组，用于行业映射与全球因子） ----
_US_STOCKS = {
    "科技": ["AAPL", "MSFT", "NVDA", "GOOGL", "AMZN", "META", "AVGO", "TSLA", "AMD",
             "INTC", "MU", "QCOM", "TXN", "ORCL", "CRM", "ADBE", "NFLX", "CSCO", "IBM",
             "NOW", "PANW", "ANET", "LRCX", "AMAT", "TSM", "ARM", "SMCI", "DELL"],
    "金融": ["JPM", "BAC", "WFC", "C", "GS", "MS", "BLK", "SCHW", "V", "AXP", "PYPL", "COIN"],
    "医疗": ["UNH", "JNJ", "LLY", "PFE", "MRK", "ABBV", "TMO", "ABT", "DHR", "ISRG", "VRTX"],
    "消费": ["WMT", "COST", "HD", "MCD", "NKE", "SBUX", "KO", "PEP", "PG", "PM", "MO",
             "TGT", "LOW"],
    "工业能源": ["XOM", "CVX", "COP", "SLB", "CAT", "DE", "BA", "GE", "HON", "UPS",
                 "FDX", "LMT", "RTX"],
    "传媒电信": ["DIS", "CMCSA", "T", "VZ", "TMUS"],
}

# ---- 美股 ETF（宽基/行业/国家/债券/商品/汇率/加密） ----
_US_ETFS = {
    "宽基": ["SPY", "QQQ", "DIA", "IWM", "VTI", "VOO"],
    "行业": ["XLK", "XLE", "XLF", "XLV", "XLI", "XLY", "XLP", "XLU", "XLB", "XLRE",
             "SMH", "IGV", "KWEB"],
    "国家": ["FXI", "MCHI", "EWJ", "EWG", "EWU", "EWC", "EWA", "EWY", "INDA", "EWZ",
             "EWW", "EWT", "EWH", "EWQ", "EEM", "EFA", "VGK", "VWO"],
    "债券": ["TLT", "IEF", "SHY", "LQD", "HYG"],
    "商品": ["GLD", "SLV", "USO", "UNG", "CPER", "DBA"],
    "汇率": ["UUP", "FXE", "FXY"],
    "加密": ["IBIT", "BITO"],
}

_HK = [("00700", "腾讯控股"), ("09988", "阿里巴巴-W"), ("03690", "美团-W"),
       ("09618", "京东集团-SW"), ("09888", "百度集团-SW"), ("01810", "小米集团-W"),
       ("00388", "香港交易所"), ("01299", "友邦保险"), ("00941", "中国移动"),
       ("01211", "比亚迪股份"), ("02015", "理想汽车-W"), ("09868", "小鹏汽车-W"),
       ("01024", "快手-W"), ("09999", "网易-S"), ("02318", "中国平安")]

_IDX = [("int_dji", "道琼斯"), ("int_nasdaq", "纳斯达克"), ("int_sp500", "标普500"),
        ("b_NKY", "日经225"), ("b_HSI", "恒生指数"), ("b_DAX", "德国DAX"),
        ("b_UKX", "英国富时100"), ("b_FTSE", "法国CAC40"), ("b_KOSPI", "韩国KOSPI"),
        ("b_SENSEX", "印度SENSEX"), ("b_SSEC", "上证指数"), ("b_TAIEX", "台湾加权")]

_GF = [("ES", "标普500期货"), ("NQ", "纳指100期货"), ("NK", "日经225期货"),
       ("HSI", "恒生指数期货")]

_FX = [("fx_susdcny", "美元兑人民币"), ("fx_seurusd", "欧元兑美元"),
       ("fx_sgbpusd", "英镑兑美元"), ("fx_susdjpy", "美元兑日元"),
       ("fx_saudusd", "澳元兑美元"), ("fx_susdchf", "美元兑瑞郎"),
       ("fx_susdcad", "美元兑加元"), ("fx_susdhkd", "美元兑港元"),
       ("fx_susdmxn", "美元兑比索"), ("fx_susdtry", "美元兑里拉"),
       ("fx_susdzar", "美元兑兰特"), ("fx_susdhuf", "美元兑福林")]

# ECB 交叉盘（任意 base/quote，Frankfurter）
_FXH = ["USDJPY", "USDCNY", "USDHKD", "USDKRW", "USDINR", "USDSGD", "USDCHF",
        "USDCAD", "USDMXN", "USDTRY", "USDBRL", "USDZAR", "USDSEK", "USDNOK",
        "USDPLN", "EURJPY", "GBPJPY", "AUDJPY", "EURCNY", "HKDCNY", "EURGBP"]

_CMD = [("CL", "WTI原油"), ("OIL", "布伦特原油"), ("GC", "COMEX黄金"),
        ("SI", "COMEX白银"), ("HG", "COMEX铜"), ("NG", "NYMEX天然气")]

_CNF = [("AU0", "沪金连续"), ("AG0", "沪银连续"), ("CU0", "沪铜连续"),
        ("AL0", "沪铝连续"), ("ZN0", "沪锌连续"), ("RB0", "螺纹钢连续"),
        ("HC0", "热卷连续"), ("I0", "铁矿石连续"), ("J0", "焦炭连续"),
        ("JM0", "焦煤连续"), ("M0", "豆粕连续"), ("Y0", "豆油连续"),
        ("P0", "棕榈油连续"), ("OI0", "菜油连续"), ("SR0", "白糖连续"),
        ("CF0", "棉花连续"), ("TA0", "PTA连续"), ("MA0", "甲醇连续"),
        ("PP0", "聚丙烯连续"), ("EG0", "乙二醇连续"), ("SA0", "纯碱连续"),
        ("FG0", "玻璃连续"), ("UR0", "尿素连续")]

_CNIDX = [("sh000001", "上证指数"), ("sh000300", "沪深300"),
          ("sz399006", "创业板指"), ("sh000688", "科创50")]

_CRYPTO = [("BTC", "比特币")]


def _build_catalog():
    """生成 (cat, sym, name, aliases, sector) 扁平目录"""
    out = []
    for sec, syms in _US_STOCKS.items():
        for s in syms:
            out.append(("us", s.lower(), s, (s,), sec))
    for sec, syms in _US_ETFS.items():
        for s in syms:
            out.append(("us", s.lower(), s, (s,), f"ETF-{sec}"))
    for s, n in _HK:
        out.append(("hk", s, n, (), "港股"))
    for s, n in _IDX:
        out.append(("idx", s, n, (), "指数"))
    for s, n in _GF:
        out.append(("gf", s, n, (), "股指期货"))
    for s, n in _FX:
        out.append(("fx", s, n, (s.replace("fx_s", "").upper(),), "外汇"))
    for s in _FXH:
        out.append(("fxh", s, s[:3] + "/" + s[3:], (s,), "外汇"))
    for s, n in _CMD:
        out.append(("cmd", s, n, (s,), "商品"))
    for s, n in _CNF:
        out.append(("cnf", s, n, (s,), "期货"))
    for s, n in _CNIDX:
        out.append(("cnidx", s, n, (s,), "A股指数"))
    for s, n in _CRYPTO:
        out.append(("crypto", s, n, ("BTC", "比特币"), "加密"))
    return out

CATALOG = _build_catalog()
_INDEX = {(c, s): dict(cat=c, sym=s, name=n, aliases=a, sector=sec)
          for c, s, n, a, sec in CATALOG}

# 搜索词表（名称/别名/代码）
_TERMS = {}
for c, s, n, a, sec in CATALOG:
    for w in (n,) + tuple(x for x in a if x):
        _TERMS.setdefault(str(w).lower(), (c, s))
WORLD_TERMS = sorted(_TERMS.keys(), key=lambda x: -len(x))

# ================================================================ 缓存

_CACHE = {}
_TTL = 4 * 3600
_LOCK = threading.Lock()


def _db_init():
    db = ds._db()
    with ds._db_lock:
        db.execute(
            "CREATE TABLE IF NOT EXISTS world_kline("
            "symbol TEXT, day TEXT, open REAL, high REAL, low REAL, close REAL, "
            "volume REAL, PRIMARY KEY(symbol, day))")
        db.execute("CREATE INDEX IF NOT EXISTS idx_world_symbol ON world_kline(symbol, day)")
        db.commit()


def _key(cat, sym):
    return f"{cat}:{sym}"


def _cache_read(key):
    try:
        _db_init()
        rows = ds._db().execute(
            "SELECT day,open,high,low,close,volume FROM world_kline WHERE symbol=? ORDER BY day",
            (key,)).fetchall()
        return [dict(day=r[0], open=r[1], high=r[2], low=r[3], close=r[4], volume=r[5])
                for r in rows]
    except Exception:  # noqa: BLE001
        return []


def _cache_write(key, rows):
    if not rows:
        return
    try:
        _db_init()
        with ds._db_lock:
            ds._db().executemany(
                "INSERT INTO world_kline(symbol,day,open,high,low,close,volume) "
                "VALUES(?,?,?,?,?,?,?) ON CONFLICT(symbol,day) DO UPDATE SET "
                "open=excluded.open,high=excluded.high,low=excluded.low,"
                "close=excluded.close,volume=excluded.volume",
                [(key, r["day"], r["open"], r["high"], r["low"], r["close"], r["volume"])
                 for r in rows])
            ds._db().commit()
    except Exception:  # noqa: BLE001
        pass


# ================================================================ 历史K线

_URLS = {
    "cnidx": "https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/CN_MarketData.getKLineData",
    "us": global_mkt._KLINE_URLS["us"],
    "fx": global_mkt._KLINE_URLS["fx"],
    "cmd": global_mkt._KLINE_URLS["cmd"],
    "cnf": global_mkt._KLINE_URLS["cnf"],
    "gf": global_mkt._KLINE_URLS["cmd"],
    "crypto": global_mkt._KLINE_URLS["cmd"],
}
_FXH_URL = "https://api.frankfurter.dev/v1/1999-01-04..?base={b}&symbols={q}"


def fetch_remote(cat, sym):
    """从上游抓取全历史日线（不做缓存）"""
    if cat in ("hk", "idx"):
        raise RuntimeError(f"{_CAT_NAME[cat]}仅有实时行情，无免费历史K线源")
    if cat == "cnidx":
        txt = ds._get(_URLS[cat], params=dict(symbol=sym, scale=240, ma="no",
                                              datalen=4000), timeout=30).text
        arr = json.loads(txt)
        return [dict(day=x["day"], open=float(x["open"]), high=float(x["high"]),
                     low=float(x["low"]), close=float(x["close"]),
                     volume=float(x.get("volume") or 0)) for x in arr if x.get("close")]
    if cat == "fxh":
        txt = ds._get(_FXH_URL.format(b=sym[:3], q=sym[3:]), timeout=40).text
        return global_mkt._parse_kline("fxh", txt)
    txt = ds._get(_URLS[cat].format(sym=sym), timeout=40).text
    return global_mkt._parse_kline(cat, txt)


def kline(cat, sym, limit=None, use_cache=True):
    """统一历史日线（本地 SQLite + 内存双层缓存）"""
    key = _key(cat, sym)
    now = time.time()
    with _LOCK:
        hit = _CACHE.get(key)
        if hit and now - hit[0] < _TTL:
            rows = hit[1]
            return rows[-limit:] if limit else rows
    rows = []
    if use_cache:
        rows = _cache_read(key)
        # 当天已同步过则直接用库里的
        if rows and ds._meta_get("world_" + key) == dt.date.today().isoformat():
            with _LOCK:
                _CACHE[key] = (now, rows)
            return rows[-limit:] if limit else rows
    try:
        rows = fetch_remote(cat, sym)
        _cache_write(key, rows)
        ds._meta_set("world_" + key, dt.date.today().isoformat())
    except Exception:  # noqa: BLE001
        if not rows:
            raise
    with _LOCK:
        _CACHE[key] = (now, rows)
        if len(_CACHE) > 400:
            _CACHE.pop(min(_CACHE, key=lambda k: _CACHE[k][0]), None)
    return rows[-limit:] if limit else rows


def closes(pairs, days=1200):
    """多标的口径对齐：返回 DataFrame（index=日期升序，列="cat:sym"）"""
    out = {}
    for cat, sym in pairs:
        try:
            rows = kline(cat, sym)
        except Exception:  # noqa: BLE001
            continue
        if not rows:
            continue
        s = pd.Series([r["close"] for r in rows],
                      index=[r["day"] for r in rows], dtype=float)
        s = s[~s.index.duplicated(keep="last")].sort_index()
        out[_key(cat, sym)] = s
    if not out:
        return pd.DataFrame()
    df = pd.concat(out, axis=1).sort_index()
    return df.tail(days) if days else df


def panel(pairs, days=1200):
    return closes(pairs, days)


# ================================================================ 实时行情

_HQ_CODE = {
    "us": lambda s: "gb_" + s.lower(),
    "hk": lambda s: "rt_hk" + s,
    "idx": lambda s: s,
    "gf": lambda s: "hf_" + s,
    "fx": lambda s: s if s.startswith("fx_") else "fx_" + s,
    "cmd": lambda s: "hf_" + s,
    "cnf": lambda s: "nf_" + s,
    "crypto": lambda s: "btc_btcbtcusd",
    "cnidx": lambda s: "s_" + s,
}


def _num(x, i):
    try:
        v = float(x[i])
        return v if v == v else None
    except (TypeError, ValueError, IndexError):
        return None


def _parse_hq(cat, sym, raw, name):
    f = raw.split(",")
    if not f or not f[0]:
        return None
    if cat in ("us",):
        price, prev = _num(f, 1), _num(f, 26)
    elif cat == "hk":
        price, prev = _num(f, 6), _num(f, 3)
    elif cat in ("idx", "cnidx"):
        # 指数行：名称,点位,涨跌,涨跌% → 末两位直接给涨跌幅
        price = _num(f, 1)
        pct_src = _num(f, 3)
        prev = round(price / (1 + pct_src / 100), 4) if price and pct_src is not None else None
        return dict(cat=cat, cat_name=_CAT_NAME[cat], symbol=sym,
                    name=_INDEX.get((cat, sym), {}).get("name", name or sym),
                    sector=_INDEX.get((cat, sym), {}).get("sector"),
                    price=price, pct=pct_src, prev_close=prev,
                    time=dt.datetime.now().strftime("%Y-%m-%d %H:%M"))
    elif cat == "fx":
        price, prev = _num(f, 1), _num(f, 3)
    elif cat in ("cmd", "gf"):
        price, prev = _num(f, 0), _num(f, 7)
    elif cat == "cnf":
        price, prev = _num(f, 8), _num(f, 10)
    else:  # crypto: 时间,?,?,昨?,...,现价在 f[8]
        price, prev = _num(f, 8), _num(f, 3)
    pct = None
    if price and prev and price > 0 and prev > 0 and abs(price / prev - 1) < 0.8:
        pct = round((price / prev - 1) * 100, 2)
    return dict(cat=cat, cat_name=_CAT_NAME[cat], symbol=sym,
                name=_INDEX.get((cat, sym), {}).get("name", name or sym),
                sector=_INDEX.get((cat, sym), {}).get("sector"),
                price=price, pct=pct, prev_close=prev,
                time=dt.datetime.now().strftime("%Y-%m-%d %H:%M"))


def quotes(pairs, batch=60):
    """批量实时行情（hq.sinajs.cn 一次多标的；fxh 走 K 线末值）"""
    out = []
    todo = []
    for cat, sym in pairs:
        if cat == "fxh":
            try:
                rows = kline(cat, sym, limit=2)
                last, prev = rows[-1], rows[-2] if len(rows) > 1 else rows[-1]
                pct = round((last["close"] / prev["close"] - 1) * 100, 2) if prev["close"] else None
                out.append(dict(cat=cat, cat_name=_CAT_NAME[cat], symbol=sym,
                                name=_INDEX.get((cat, sym), {}).get("name", sym),
                                price=last["close"], pct=pct, prev_close=prev["close"],
                                time=last["day"]))
            except Exception:  # noqa: BLE001
                continue
        else:
            todo.append((cat, sym))
    for i in range(0, len(todo), batch):
        chunk = todo[i:i + batch]
        codes = ",".join(_HQ_CODE[c](s) for c, s in chunk)
        try:
            txt = ds._get(f"https://hq.sinajs.cn/list={codes}", timeout=20).text
        except Exception:  # noqa: BLE001
            continue
        raw = {}
        for line in txt.splitlines():
            if '="' not in line:
                continue
            code = line.split('hq_str_')[-1].split('="')[0].strip()
            val = line.split('="', 1)[1].rstrip('";')
            raw[code] = val
        for cat, sym in chunk:
            code = _HQ_CODE[cat](sym)
            v = raw.get(code)
            if not v:
                continue
            q = _parse_hq(cat, sym, v, sym)
            if q and q.get("price"):
                out.append(q)
    return out


# ================================================================ 覆盖度

def coverage():
    """按类别统计标的数、数据源、历史深度与局限；附实际缓存区间"""
    rows = []
    for cat in _CAT_NAME:
        items = [(c, s) for c, s, *_ in CATALOG if c == cat]
        n = len(items)
        cached, start, end = 0, None, None
        try:
            _db_init()
            got = ds._db().execute(
                "SELECT COUNT(DISTINCT symbol), MIN(day), MAX(day) FROM world_kline "
                "WHERE symbol LIKE ?", (cat + ":%",)).fetchone()
            cached = int(got[0] or 0)
            start, end = got[1], got[2]
        except Exception:  # noqa: BLE001
            pass
        rows.append(dict(
            cat=cat, cat_name=_CAT_NAME[cat], symbols=n, cached=cached,
            source=_CAT_META[cat]["source"], hist=_CAT_META[cat]["hist"],
            mode=_CAT_META[cat]["mode"], limitation=_CAT_LIMIT[cat],
            data_start=start, data_end=end))
    # A 股全市场单独统计（来自本地 stocks 表）
    try:
        a_total = ds._db().execute("SELECT COUNT(*) FROM stocks").fetchone()[0]
        a_kline = ds._db().execute(
            "SELECT COUNT(DISTINCT symbol) FROM kline").fetchone()[0]
    except Exception:  # noqa: BLE001
        a_total, a_kline = 0, 0
    return dict(
        classes=rows,
        total_symbols=sum(r["symbols"] for r in rows),
        a_share=dict(symbols=int(a_total), with_kline=int(a_kline),
                     source="新浪 CN_MarketData", hist="上市以来全部",
                     mode="全历史", cat_name="A股个股"),
        note=("全球 10 大类共 %d 个标的接入本地缓存；A 股全市场 %d 只（%d 只有历史K线）。"
              "免费源无法覆盖：港股/全球指数现货历史、非 BTC 加密、债券收益率、"
              "期权与期货持仓明细、逐笔与 Level-2。这些在覆盖表中如实标注为「仅实时/无」。"
              % (sum(r["symbols"] for r in rows), int(a_total), int(a_kline))),
    )


def search(q, limit=10):
    """全球目录搜索（名称/别名/代码）"""
    q = (q or "").strip().lower()
    if not q:
        return []
    out, seen = [], set()
    for w in WORLD_TERMS:
        if q not in w and w not in q:
            continue
        cat, sym = _TERMS[w]
        if (cat, sym) in seen:
            continue
        seen.add((cat, sym))
        m = _INDEX[(cat, sym)]
        out.append(dict(cat=cat, cat_name=_CAT_NAME[cat], symbol=sym,
                        name=m["name"], sector=m["sector"]))
        if len(out) >= limit:
            break
    return out


def asset(cat, sym):
    m = _INDEX.get((cat, sym))
    if not m:
        raise KeyError(f"未收录标的 {cat}:{sym}")
    rows = kline(cat, sym)
    s = pd.Series([r["close"] for r in rows],
                  index=pd.to_datetime([r["day"] for r in rows])).sort_index()
    ret = s.pct_change().dropna()
    ann_vol = float(ret.tail(60).std() * math.sqrt(_TRADING_DAYS) * 100)
    return dict(meta=m, days=int(len(s)),
                first=str(s.index[0].date()) if len(s) else None,
                last=str(s.index[-1].date()) if len(s) else None,
                last_close=float(s.iloc[-1]) if len(s) else None,
                ann_vol_60d=round(ann_vol, 2),
                cum_1y=round(float(s.iloc[-1] / s.iloc[-min(244, len(s))] - 1) * 100, 2)
                if len(s) > 30 else None)


def warm(pairs=None, workers=8):
    """并行预热缓存（供后台/测试调用）"""
    from concurrent.futures import ThreadPoolExecutor
    todo = pairs or [(c, s) for c, s, *_ in CATALOG]
    errs = []

    def _one(p):
        try:
            return p, len(kline(*p))
        except Exception as e:  # noqa: BLE001
            errs.append((p, str(e)[:60]))
            return p, 0

    with ThreadPoolExecutor(max_workers=workers) as ex:
        res = list(ex.map(_one, todo))
    ok = sum(1 for _, n in res if n > 0)
    return dict(total=len(todo), ok=ok, failed=len(errs), errors=errs[:20])
