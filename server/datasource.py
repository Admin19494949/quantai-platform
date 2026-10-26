# -*- coding: utf-8 -*-
"""数据层：新浪财经行情接口 + SQLite 本地缓存

数据源（经沙箱代理验证可用）：
- 个股日线（上市以来全部）：money.finance.sina.com.cn CN_MarketData.getKLineData
- A股全列表（含实时快照字段）：vip.stock.finance.sina.com.cn Market_Center.getHQNodeData
- 批量实时行情：hq.sinajs.cn
- 指数实时：hq.sinajs.cn（s_sh000001 等）
"""
import datetime as dt
import json
import os
import re
import sqlite3
import threading
import time

import requests

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.path.join(BASE_DIR, "data", "cache.db")

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Referer": "https://finance.sina.com.cn/",
}

_db_lock = threading.Lock()
_conn = None

# ---------------------------------------------------------------- DB


def _init_db():
    global _conn
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    _conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    c = _conn.cursor()
    c.executescript(
        """
        CREATE TABLE IF NOT EXISTS stocks(
            code TEXT PRIMARY KEY, name TEXT, market TEXT,
            price REAL, pct REAL, amount REAL, turnover REAL,
            pe REAL, pb REAL, mktcap REAL, updated TEXT);
        CREATE TABLE IF NOT EXISTS kline(
            symbol TEXT, day TEXT, open REAL, high REAL, low REAL,
            close REAL, volume REAL, PRIMARY KEY(symbol, day));
        CREATE INDEX IF NOT EXISTS idx_kline_symbol ON kline(symbol, day);
        CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);
        CREATE TABLE IF NOT EXISTS heat_history(
            date TEXT PRIMARY KEY, score REAL, up INTEGER, down INTEGER,
            limit_up INTEGER, limit_down INTEGER, total_amount REAL);
        """
    )
    _conn.commit()


def _db():
    if _conn is None:
        with _db_lock:
            if _conn is None:
                _init_db()
    return _conn


def _meta_get(key):
    row = _db().execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return row[0] if row else None


def _meta_set(key, value):
    with _db_lock:
        _db().execute(
            "INSERT INTO meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, str(value)),
        )
        _db().commit()


# ---------------------------------------------------------------- HTTP


def _get(url, params=None, tries=3, timeout=25):
    last = None
    for _ in range(tries):
        try:
            r = requests.get(url, params=params, headers=HEADERS, timeout=timeout)
            if r.status_code == 200:
                return r
            last = RuntimeError(f"HTTP {r.status_code}")
        except Exception as e:  # noqa: BLE001
            last = e
        time.sleep(0.4)
    raise last


# ---------------------------------------------------------------- 股票代码工具


def norm_code(code: str) -> str:
    """输入 000001 / sz000001 / 1 → 000001"""
    c = str(code).strip().lower()
    for p in ("sh", "sz", "bj"):
        if c.startswith(p):
            c = c[2:]
    return c.zfill(6)


def market_symbol(code: str) -> str:
    """000001 → sz000001（新浪 kline/list 用的 symbol）"""
    c = norm_code(code)
    if c.startswith(("6", "9", "5")) and not c.startswith("92"):
        return "sh" + c
    if c.startswith(("4", "8")) or c.startswith("92"):
        return "bj" + c
    return "sz" + c


def market_prefix(code: str) -> str:
    return market_symbol(code)[:2]


# ---------------------------------------------------------------- 全市场列表


def _fetch_stock_list_page(page, num=100):
    url = (
        "https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/"
        "Market_Center.getHQNodeData?page=%d&num=%d&sort=symbol&asc=1&node=hs_a"
        "&symbol=&_s_r_a=page" % (page, num)
    )
    text = _get(url).text
    return json.loads(text) if text.strip() else []


def load_stock_list(progress_cb=None):
    """分页拉取全部A股（每页100条）写入 stocks 表。"""
    today = dt.date.today().isoformat()
    if _meta_get("stock_list_date") == today and _stock_count() > 5000:
        return
    page = 1
    total_loaded = 0
    while True:
        try:
            rows = _fetch_stock_list_page(page)
        except Exception:  # noqa: BLE001
            break
        if not rows:
            break
        with _db_lock:
            _db().executemany(
                "INSERT INTO stocks(code,name,market,price,pct,amount,turnover,pe,pb,mktcap,updated)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?)"
                " ON CONFLICT(code) DO UPDATE SET name=excluded.name, price=excluded.price,"
                " pct=excluded.pct, amount=excluded.amount, turnover=excluded.turnover,"
                " pe=excluded.pe, pb=excluded.pb, mktcap=excluded.mktcap, updated=excluded.updated",
                [
                    (
                        r["code"].zfill(6), r["name"], r["symbol"][:2],
                        float(r.get("trade") or 0), float(r.get("changepercent") or 0),
                        float(r.get("amount") or 0), float(r.get("turnoverratio") or 0),
                        float(r.get("per") or 0), float(r.get("pb") or 0),
                        float(r.get("mktcap") or 0), today,
                    )
                    for r in rows
                ],
            )
            _db().commit()
        total_loaded += len(rows)
        if progress_cb:
            progress_cb(total_loaded)
        page += 1
    if total_loaded > 100:
        _meta_set("stock_list_date", today)


def _stock_count():
    return _db().execute("SELECT COUNT(*) FROM stocks").fetchone()[0]


_list_thread = None


def ensure_stock_list_async():
    """启动时后台加载全市场列表。"""
    global _list_thread
    if _meta_get("stock_list_date") == dt.date.today().isoformat() and _stock_count() > 5000:
        return
    if _list_thread and _list_thread.is_alive():
        return

    def _run():
        try:
            load_stock_list()
        except Exception as e:  # noqa: BLE001
            print("[datasource] stock list load failed:", e)

    _list_thread = threading.Thread(target=_run, daemon=True)
    _list_thread.start()


def _normalize_input(q):
    """全角转半角、去空格：兼容中文输入法下的全角数字/字母"""
    table = str.maketrans("０１２３４５６７８９ＡＢＣＤＥＦＧＨＩＪＫＬＭＮＯＰＱＲＳＴＵＶＷＸＹＺａｂｃｄｅｆｇｈｉｊｋｌｍｎｏｐｑｒｓｔｕｖｗｘｙｚ",
                          "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz")
    return str(q).translate(table).replace(" ", "").strip()


def search_stocks(q, limit=12):
    q = _normalize_input(q)
    corrected = False
    if not q:
        # 返回部分热门股票（按成交额排序）
        rows = _db().execute(
            "SELECT code,name,market,price,pct FROM stocks ORDER BY amount DESC LIMIT ?", (limit,)
        ).fetchall()
    else:
        like = "%" + q + "%"
        rows = _db().execute(
            # REPLACE 兼容新浪源名称自带空格的情况（如"五 粮 液"）
            "SELECT code,name,market,price,pct FROM stocks "
            "WHERE code LIKE ? OR REPLACE(name,' ','') LIKE ? ORDER BY amount DESC LIMIT ?",
            (q.zfill(6) + "%", like, limit),
        ).fetchall()
        if not rows and q.isdigit():
            # 数字输入：仅在库中真实存在该代码时返回，避免生成点击后404的僵尸条目
            c = norm_code(q)
            if len(c) == 6:
                row = _db().execute(
                    "SELECT code,name,market,price,pct FROM stocks WHERE code=?", (c,)
                ).fetchone()
                if row:
                    rows = [row]
                else:
                    # 输入环境可能产生字符倒序（实测 001216 → 612100）：倒序自动纠正
                    row2 = _db().execute(
                        "SELECT code,name,market,price,pct FROM stocks WHERE code=?", (c[::-1],)
                    ).fetchone()
                    if row2:
                        rows = [row2]
                        corrected = True
    results = [dict(code=r[0], name=(r[1] or "").replace(" ", ""), market=r[2], price=r[3], pct=r[4])
               for r in rows]
    if corrected and results:
        results[0]["corrected"] = True
    return results


def get_stock_name(code):
    row = _db().execute("SELECT name FROM stocks WHERE code=?", (norm_code(code),)).fetchone()
    return row[0].replace(" ", "") if row else ""


def stocks_loaded():
    """全市场列表是否已完成初始加载"""
    n = _db().execute("SELECT COUNT(*) FROM stocks").fetchone()[0]
    return n > 100


_names_cache = {"date": None, "names": []}


_CODE_RE = re.compile(r"(?<!\d)(\d{6})(?!\d)")


def resolve_stock_in_text(q):
    """从自然语言文本中识别提到的股票（最长名称优先，其次6位代码），返回 (code, name) 或 None"""
    if not q:
        return None
    today = dt.date.today().isoformat()
    if _names_cache["date"] != today:
        rows = _db().execute("SELECT code,name FROM stocks WHERE name<>''").fetchall()
        _names_cache["date"] = today
        # 名称去空格（新浪源部分名称带空格，如"五 粮 液"），保证自然语言能匹配
        _names_cache["names"] = sorted(((r[0], r[1].replace(" ", "")) for r in rows if r[1]),
                                       key=lambda x: -len(x[1]))
    for code, name in _names_cache["names"]:
        if name in q:
            return code, name
    for m in _CODE_RE.finditer(q):
        row = _db().execute("SELECT code,name FROM stocks WHERE code=?", (m.group(1),)).fetchone()
        if row:
            return row[0], row[1]
    return None


# ---------------------------------------------------------------- K线


def _fetch_kline_remote(symbol, datalen=10000):
    url = (
        "https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/"
        f"CN_MarketData.getKLineData?symbol={symbol}&scale=240&ma=no&datalen={datalen}"
    )
    return json.loads(_get(url, timeout=30).text or "[]")


def get_kline(code, use_cache=True):
    """返回上市以来全部日线（升序）：list[dict(day,open,high,low,close,volume)]"""
    symbol = market_symbol(code)
    today = dt.date.today().isoformat()
    flag = "kline_" + symbol
    if use_cache and _meta_get(flag) == today:
        rows = _db().execute(
            "SELECT day,open,high,low,close,volume FROM kline WHERE symbol=? ORDER BY day", (symbol,)
        ).fetchall()
        if rows:
            return [dict(day=r[0], open=r[1], high=r[2], low=r[3], close=r[4], volume=r[5]) for r in rows]
    data = _fetch_kline_remote(symbol)
    if data:
        with _db_lock:
            _db().executemany(
                "INSERT INTO kline(symbol,day,open,high,low,close,volume) VALUES(?,?,?,?,?,?,?)"
                " ON CONFLICT(symbol,day) DO UPDATE SET open=excluded.open, high=excluded.high,"
                " low=excluded.low, close=excluded.close, volume=excluded.volume",
                [
                    (symbol, r["day"], float(r["open"]), float(r["high"]),
                     float(r["low"]), float(r["close"]), float(r["volume"]))
                    for r in data
                ],
            )
            _db().commit()
        _meta_set(flag, today)
    rows = _db().execute(
        "SELECT day,open,high,low,close,volume FROM kline WHERE symbol=? ORDER BY day", (symbol,)
    ).fetchall()
    return [dict(day=r[0], open=r[1], high=r[2], low=r[3], close=r[4], volume=r[5]) for r in rows]


# ---------------------------------------------------------------- 实时行情


_rt_cache = {"ts": 0, "data": {}}


def get_realtime_snapshot(ttl=120):
    """全市场实时快照 dict[code] = {name,open,prev_close,price,high,low,volume,amount,
    bid1_price,bid1_vol,ask1_price,ask1_vol,date,time}（含买卖一档，供封单计算）"""
    if time.time() - _rt_cache["ts"] < ttl and _rt_cache["data"]:
        return _rt_cache["data"]
    codes = [r[0] for r in _db().execute("SELECT code FROM stocks").fetchall()]
    if not codes:
        return {}
    out = {}
    B = 300
    for i in range(0, len(codes), B):
        chunk = codes[i : i + B]
        url = "https://hq.sinajs.cn/list=" + ",".join(market_symbol(c) for c in chunk)
        try:
            text = _get(url, timeout=20).text
        except Exception:  # noqa: BLE001
            continue
        for line in text.splitlines():
            m = line.split('="')
            if len(m) != 2:
                continue
            sym = m[0].replace("var hq_str_", "").strip()
            f = m[1].rstrip('";').split(",")
            if len(f) < 32 or not f[0]:
                continue
            try:
                out[sym[2:]] = dict(
                    name=f[0], open=float(f[1] or 0), prev_close=float(f[2] or 0),
                    price=float(f[3] or 0), high=float(f[4] or 0), low=float(f[5] or 0),
                    volume=float(f[8] or 0), amount=float(f[9] or 0),
                    bid1_price=float(f[11] or 0), bid1_vol=float(f[10] or 0),
                    ask1_price=float(f[21] or 0), ask1_vol=float(f[20] or 0),
                    date=f[30], time=f[31],
                )
            except (ValueError, IndexError):
                continue
    if out:
        _rt_cache["ts"] = time.time()
        _rt_cache["data"] = out
    return out


def get_stock_quote(code):
    snap = get_realtime_snapshot()
    return snap.get(norm_code(code))


INDEX_LIST = [
    ("sh000001", "上证指数"),
    ("sz399001", "深证成指"),
    ("sz399006", "创业板指"),
    ("sh000688", "科创50"),
]


def get_index_realtime():
    url = "https://hq.sinajs.cn/list=" + ",".join("s_" + s for s, _ in INDEX_LIST)
    text = _get(url, timeout=15).text
    out = []
    for line in text.splitlines():
        try:
            sym = line.split("hq_str_s_")[1].split('="')[0]
            f = line.split('="')[1].rstrip('";').split(",")
            out.append(dict(code=sym, name=f[0], value=float(f[1]),
                            chg=float(f[2]), pct=float(f[3]),
                            volume=float(f[4]), amount=float(f[5])))
        except (IndexError, ValueError):
            continue
    return out
