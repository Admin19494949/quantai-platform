# -*- coding: utf-8 -*-
"""全市场横截面数据层：批量财务快照 + 行业分类 + 因子分布

数据源（东方财富 datacenter-web，经沙箱验证可用）：
- RPT_F10_FINANCE_MAINFINADATA：ROE/ROA/净利率/毛利率/资产负债率/营收增速/净利增速
- RPT_LICO_FN_CPD：行业分类（PUBLISHNAME 二级行业）

落库 SQLite（按自然日刷新一次，后台线程同步，不阻塞请求）：
- fin_snapshot(code,name,roe,roa,net_margin,gross_margin,debt_ratio,
               revenue_growth,profit_growth,bps,eps,report_date,updated)
- industry_map(code,name,industry,updated)
- vol_sample(code,vol_20d,mdd,updated)   —— 波动率抽样分布（轻量，不落 kline 表）

用途：
1. 因子分位补全（成长/质量因子此前因缺全市场分布而降级为 None）
2. 组合回测的行业约束
3. 风险模型的行业中性化
"""
import bisect
import concurrent.futures as cf
import datetime as dt
import threading
import time

import numpy as np
import requests

from . import datasource as ds

DC_URL = "https://datacenter-web.eastmoney.com/api/data/v1/get"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Referer": "https://data.eastmoney.com/",
}
PAGE_SIZE = 500
MAX_PAGES = 45
MIN_A_COVERAGE = 3000     # A股覆盖数低于此值视为该报告期未披露完整
VOL_SAMPLE_N = 160        # 波动率抽样只数
VOL_SAMPLE_TTL_DAYS = 7   # 波动率抽样刷新周期
DIST_TTL = 300            # 内存分布缓存秒数

_lock = threading.Lock()
_sync_lock = threading.Lock()
_thread = None
_mem = {"ts": 0.0, "dist": None}

# ---------------------------------------------------------------- 表结构


def _init_tables():
    conn = ds._db()
    with ds._db_lock:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS fin_snapshot(
                code TEXT PRIMARY KEY, name TEXT,
                roe REAL, roa REAL, net_margin REAL, gross_margin REAL,
                debt_ratio REAL, revenue_growth REAL, profit_growth REAL,
                bps REAL, eps REAL, report_date TEXT, updated TEXT);
            CREATE TABLE IF NOT EXISTS industry_map(
                code TEXT PRIMARY KEY, name TEXT, industry TEXT, updated TEXT);
            CREATE TABLE IF NOT EXISTS vol_sample(
                code TEXT PRIMARY KEY, vol_20d REAL, mdd REAL, updated TEXT);
            CREATE INDEX IF NOT EXISTS idx_ind_industry ON industry_map(industry);
            """
        )
        conn.commit()


# ---------------------------------------------------------------- HTTP

def _get_json(params, tries=3, timeout=25):
    last = None
    for _ in range(tries):
        try:
            r = requests.get(DC_URL, params=params, headers=HEADERS, timeout=timeout)
            if r.status_code == 200:
                return r.json()
            last = RuntimeError(f"HTTP {r.status_code}")
        except Exception as e:  # noqa: BLE001
            last = e
        time.sleep(0.5)
    raise last


def _fetch_all(report_name, columns, filter_str, max_pages=MAX_PAGES):
    """分页拉取全部记录（每页 500）"""
    out = []
    for page in range(1, max_pages + 1):
        d = _get_json({
            "reportName": report_name,
            "columns": columns,
            "pageNumber": str(page),
            "pageSize": str(PAGE_SIZE),
            "sortColumns": "SECURITY_CODE",
            "sortTypes": "1",
            "filter": filter_str,
        })
        res = d.get("result") or {}
        data = res.get("data") or []
        if not data:
            break
        out.extend(data)
        total = res.get("count") or 0
        if len(out) >= total:
            break
    return out


# ---------------------------------------------------------------- 工具

def _is_a_share(code):
    """6位A股代码（剔除B股900/200开头）"""
    if not code or len(code) != 6 or not code.isdigit():
        return False
    if code.startswith("900") or code.startswith("200"):
        return False
    return code[0] in "03684" or code.startswith("92")


def _recent_quarter_ends(back=6):
    """最近若干个已过去的季度末日期（降序）"""
    today = dt.date.today()
    ends = []
    d = today.replace(day=1)
    for _ in range(48):
        if d.month in (3, 6, 9, 12):
            last = (d.replace(day=28) + dt.timedelta(days=4)).replace(day=1) - dt.timedelta(days=1)
            if last <= today:
                ends.append(last.isoformat())
                if len(ends) >= back:
                    break
        d = (d - dt.timedelta(days=1)).replace(day=1)
    return ends


def _f(x):
    """安全转 float"""
    if x is None:
        return None
    try:
        v = float(x)
        return None if np.isnan(v) or np.isinf(v) else v
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------- 同步：财务快照

FIN_COLUMNS = ("SECURITY_CODE,SECURITY_NAME_ABBR,ROEJQ,ZZCJLL,XSJLL,XSMLL,ZCFZL,"
               "TOTALOPERATEREVETZ,PARENTNETPROFITTZ,BPS,EPSJB")


def _known_stock_codes():
    """stocks 表中的真实 A 股代码集合（用于剔除债券/基金等同码段证券）"""
    try:
        return {r[0] for r in ds._db().execute("SELECT code FROM stocks").fetchall()}
    except Exception:  # noqa: BLE001
        return set()


def _pick_report_rows(report_name, columns, date_field="REPORT_DATE", back=6):
    """返回 (报告期, A股行列表)：从最近季度往回找，取首个覆盖≥3000只A股的报告期

    date_field：东财报表的日期字段名（MAINFINADATA 用 REPORT_DATE，LICO_FN_CPD 用 REPORTDATE）
    """
    known = _known_stock_codes()
    for rd in _recent_quarter_ends(back):
        try:
            data = _fetch_all(report_name, columns, f"({date_field}='{rd}')")
        except Exception:  # noqa: BLE001
            continue
        a_rows = [
            r for r in data
            if _is_a_share(r.get("SECURITY_CODE") or "")
            and (not known or (r.get("SECURITY_CODE") or "").strip() in known)
        ]
        if len(a_rows) >= MIN_A_COVERAGE:
            return rd, a_rows
    return None, []


def sync_financials(force=False):
    """同步全市场财务快照（ROE/ROA/净利率/毛利率/负债率/营收增速/净利增速）"""
    today = dt.date.today().isoformat()
    if not force and ds._meta_get("fin_sync_date") == today:
        return dict(skipped=True, report_date=ds._meta_get("fin_report_date"), rows=_fin_count())
    _init_tables()
    rd, rows = _pick_report_rows("RPT_F10_FINANCE_MAINFINADATA", FIN_COLUMNS)
    if not rows:
        return dict(error="无法获取全市场财务数据")
    recs = []
    for r in rows:
        code = (r.get("SECURITY_CODE") or "").strip()
        recs.append((
            code, (r.get("SECURITY_NAME_ABBR") or "").replace(" ", ""),
            _f(r.get("ROEJQ")), _f(r.get("ZZCJLL")), _f(r.get("XSJLL")), _f(r.get("XSMLL")),
            _f(r.get("ZCFZL")), _f(r.get("TOTALOPERATEREVETZ")), _f(r.get("PARENTNETPROFITTZ")),
            _f(r.get("BPS")), _f(r.get("EPSJB")), rd, today,
        ))
    with ds._db_lock:
        conn = ds._db()
        conn.execute("DELETE FROM fin_snapshot")   # 全量替换（数据已完整取回，安全）
        conn.executemany(
            "INSERT INTO fin_snapshot(code,name,roe,roa,net_margin,gross_margin,debt_ratio,"
            "revenue_growth,profit_growth,bps,eps,report_date,updated) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(code) DO UPDATE SET name=excluded.name, roe=excluded.roe, roa=excluded.roa,"
            " net_margin=excluded.net_margin, gross_margin=excluded.gross_margin,"
            " debt_ratio=excluded.debt_ratio, revenue_growth=excluded.revenue_growth,"
            " profit_growth=excluded.profit_growth, bps=excluded.bps, eps=excluded.eps,"
            " report_date=excluded.report_date, updated=excluded.updated",
            recs,
        )
        # 清除本次未覆盖的历史残留行（此前版本可能写入债券/基金等同码段证券）
        conn.execute("DELETE FROM fin_snapshot WHERE updated<>?", (today,))
        conn.commit()
    ds._meta_set("fin_sync_date", today)
    ds._meta_set("fin_report_date", rd)
    with _lock:
        _mem["dist"] = None      # 失效内存分布
    return dict(report_date=rd, rows=len(recs))


def _fin_count():
    try:
        return ds._db().execute("SELECT COUNT(*) FROM fin_snapshot").fetchone()[0]
    except Exception:  # noqa: BLE001
        return 0


# ---------------------------------------------------------------- 同步：行业分类

def sync_industry(force=False):
    """同步行业分类（东财二级行业 PUBLISHNAME）"""
    today = dt.date.today().isoformat()
    if not force and ds._meta_get("ind_sync_date") == today:
        return dict(skipped=True, rows=_ind_count())
    _init_tables()
    rd, rows = _pick_report_rows(
        "RPT_LICO_FN_CPD", "SECURITY_CODE,SECURITY_NAME_ABBR,PUBLISHNAME",
        date_field="REPORTDATE")
    if not rows:
        return dict(error="无法获取行业分类数据")
    recs = []
    for r in rows:
        ind = (r.get("PUBLISHNAME") or "").strip()
        if not ind:
            continue
        recs.append((
            (r.get("SECURITY_CODE") or "").strip(),
            (r.get("SECURITY_NAME_ABBR") or "").replace(" ", ""), ind, today,
        ))
    with ds._db_lock:
        conn = ds._db()
        conn.execute("DELETE FROM industry_map")   # 全量替换
        conn.executemany(
            "INSERT INTO industry_map(code,name,industry,updated) VALUES(?,?,?,?) "
            "ON CONFLICT(code) DO UPDATE SET name=excluded.name, industry=excluded.industry,"
            " updated=excluded.updated",
            recs,
        )
        conn.execute("DELETE FROM industry_map WHERE updated<>?", (today,))
        conn.commit()
    ds._meta_set("ind_sync_date", today)
    return dict(report_date=rd, rows=len(recs))


def _ind_count():
    try:
        return ds._db().execute("SELECT COUNT(*) FROM industry_map").fetchone()[0]
    except Exception:  # noqa: BLE001
        return 0


# ---------------------------------------------------------------- 同步：波动率抽样

def _sample_vol_one(code):
    """单只抽样：用新浪近 250 日 K 线算 20 日年化波动率与近 250 日最大回撤"""
    try:
        symbol = ds.market_symbol(code)
        data = ds._fetch_kline_remote(symbol, 250)
        if not data or len(data) < 40:
            return None
        close = np.array([float(x["close"]) for x in data if float(x.get("close") or 0) > 0])
        if len(close) < 40:
            return None
        pct = np.diff(close) / close[:-1]
        vol20 = float(np.std(pct[-20:]) * np.sqrt(244) * 100)
        cum = np.maximum.accumulate(close)
        mdd = float(np.min(close / cum - 1) * 100)   # 负数
        return code, round(vol20, 2), round(abs(mdd), 2)
    except Exception:  # noqa: BLE001
        return None


def sync_vol_sample(force=False, n=VOL_SAMPLE_N):
    """抽样估计波动率/回撤的全市场分布（只取抽样，控制耗时与落库体量）"""
    today = dt.date.today().isoformat()
    last = ds._meta_get("vol_sync_date")
    if not force and last:
        try:
            if (dt.date.today() - dt.date.fromisoformat(last)).days < VOL_SAMPLE_TTL_DAYS:
                return dict(skipped=True, rows=_vol_count())
        except ValueError:
            pass
    _init_tables()
    codes = [r[0] for r in ds._db().execute(
        "SELECT code FROM stocks WHERE price>0 AND amount>0 ORDER BY amount DESC LIMIT ?", (n,)
    ).fetchall()]
    if not codes:
        return dict(error="股票列表尚未就绪")
    with cf.ThreadPoolExecutor(max_workers=8) as ex:
        res = [r for r in ex.map(_sample_vol_one, codes) if r]
    if not res:
        return dict(error="波动率抽样全部失败")
    with ds._db_lock:
        conn = ds._db()
        conn.executemany(
            "INSERT INTO vol_sample(code,vol_20d,mdd,updated) VALUES(?,?,?,?) "
            "ON CONFLICT(code) DO UPDATE SET vol_20d=excluded.vol_20d, mdd=excluded.mdd,"
            " updated=excluded.updated",
            [(c, v, m, today) for c, v, m in res],
        )
        conn.commit()
    ds._meta_set("vol_sync_date", today)
    with _lock:
        _mem["dist"] = None
    return dict(rows=len(res), sample_n=n)


def _vol_count():
    try:
        return ds._db().execute("SELECT COUNT(*) FROM vol_sample").fetchone()[0]
    except Exception:  # noqa: BLE001
        return 0


# ---------------------------------------------------------------- 后台同步

def ensure_async():
    """后台线程同步全部横截面数据（幂等：当日已同步则跳过）"""
    global _thread
    if _thread and _thread.is_alive():
        return
    today = dt.date.today().isoformat()
    if (ds._meta_get("fin_sync_date") == today and ds._meta_get("ind_sync_date") == today
            and ds._meta_get("vol_sync_date")):
        return

    def _run():
        with _sync_lock:
            for fn, label in ((sync_financials, "financials"),
                              (sync_industry, "industry"),
                              (sync_vol_sample, "vol")):
                try:
                    print(f"[market_data] sync {label}: {fn()}")
                except Exception as e:  # noqa: BLE001
                    print(f"[market_data] sync {label} failed: {e}")

    _thread = threading.Thread(target=_run, daemon=True)
    _thread.start()


# ---------------------------------------------------------------- 读取接口

def get_distributions():
    """各基本因子的全市场分布（升序数组），用于分位计算"""
    now = time.time()
    with _lock:
        if _mem["dist"] and now - _mem["ts"] < DIST_TTL:
            return _mem["dist"]
    _init_tables()
    rows = ds._db().execute(
        "SELECT roe,roa,net_margin,debt_ratio,revenue_growth,profit_growth FROM fin_snapshot"
    ).fetchall()
    cols = ["roe", "roa", "net_margin", "debt_ratio", "revenue_growth", "profit_growth"]
    dist = {c: [] for c in cols}
    for r in rows:
        for i, c in enumerate(cols):
            v = r[i]
            if v is not None:
                dist[c].append(float(v))
    for c in cols:
        dist[c].sort()
    # 资产负债率负向化：100 - debt_ratio，值越大（杠杆越低）越好
    dist["debt_ratio_neg"] = sorted(100.0 - x for x in dist["debt_ratio"])
    # 波动率/回撤抽样分布（原始值：数值越小代表风险越低，方向由调用方反转）
    vrows = ds._db().execute("SELECT vol_20d,mdd FROM vol_sample").fetchall()
    dist["vol_20d"] = sorted(float(x[0]) for x in vrows if x[0] is not None)
    dist["mdd"] = sorted(float(x[1]) for x in vrows if x[1] is not None)
    with _lock:
        _mem["dist"] = dist
        _mem["ts"] = now
    return dist


def percentile_rank(sorted_arr, value):
    """在升序数组中求 value 的分位（0-100），越大越靠前"""
    if value is None or not sorted_arr:
        return None
    cnt = bisect.bisect_right(sorted_arr, float(value))
    return round(cnt / len(sorted_arr) * 100, 1)


def get_industry_map():
    """code -> 行业名称"""
    _init_tables()
    rows = ds._db().execute("SELECT code,industry FROM industry_map").fetchall()
    return {r[0]: r[1] for r in rows if r[1]}


def get_industry(code):
    """单只股票的行业"""
    try:
        row = ds._db().execute(
            "SELECT industry FROM industry_map WHERE code=?", (ds.norm_code(code),)).fetchone()
        return row[0] if row else None
    except Exception:  # noqa: BLE001
        return None


def get_fin_snapshot(code):
    """单只股票的最新财务快照"""
    try:
        row = ds._db().execute(
            "SELECT name,roe,roa,net_margin,gross_margin,debt_ratio,revenue_growth,"
            "profit_growth,bps,eps,report_date FROM fin_snapshot WHERE code=?",
            (ds.norm_code(code),)).fetchone()
        if not row:
            return None
        keys = ["name", "roe", "roa", "net_margin", "gross_margin", "debt_ratio",
                "revenue_growth", "profit_growth", "bps", "eps", "report_date"]
        return dict(zip(keys, row))
    except Exception:  # noqa: BLE001
        return None


def get_all_snapshots():
    """全市场财务快照 list[dict]（供组合选股/因子打分批量使用）"""
    _init_tables()
    rows = ds._db().execute(
        "SELECT code,name,roe,roa,net_margin,gross_margin,debt_ratio,revenue_growth,"
        "profit_growth,bps,eps,report_date FROM fin_snapshot").fetchall()
    keys = ["code", "name", "roe", "roa", "net_margin", "gross_margin", "debt_ratio",
            "revenue_growth", "profit_growth", "bps", "eps", "report_date"]
    return [dict(zip(keys, r)) for r in rows]


def status():
    """同步状态（供 API 展示）"""
    _init_tables()
    return dict(
        fin_rows=_fin_count(),
        ind_rows=_ind_count(),
        vol_rows=_vol_count(),
        fin_report_date=ds._meta_get("fin_report_date"),
        fin_sync_date=ds._meta_get("fin_sync_date"),
        ind_sync_date=ds._meta_get("ind_sync_date"),
        vol_sync_date=ds._meta_get("vol_sync_date"),
        industries=len({r[0] for r in ds._db().execute(
            "SELECT DISTINCT industry FROM industry_map").fetchall()}),
    )
