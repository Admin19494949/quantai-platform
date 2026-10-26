# -*- coding: utf-8 -*-
"""多因子框架：价值/成长/质量/动量/波动率 五大类因子

因子体系对标 Fama-French + Barra 风险模型的简化版：
- 价值因子：E/P(PE倒数)、B/P(PB倒数)
- 成长因子：净利润增速、营收增速
- 质量因子：ROE、ROA、净利率、资产负债率(负向)
- 动量因子：近1月/3月/6月收益率
- 波动率因子：20日年化波动率、最大回撤(负向)
"""
import datetime as dt
import threading
import time

import numpy as np

from . import analysis, datasource as ds, fundamental as fund, market_data

FACTOR_DEFS = {
    "value": ["ep", "bp"],
    "growth": ["profit_growth", "revenue_growth"],
    "quality": ["roe", "roa", "net_margin", "debt_ratio_neg"],
    "momentum": ["ret_1m", "ret_3m", "ret_6m"],
    "volatility": ["vol_20d", "mdd_neg"],
}

FACTOR_NAMES = {
    "ep": "市盈率倒数(E/P)",
    "bp": "市净率倒数(B/P)",
    "profit_growth": "净利润增速",
    "revenue_growth": "营收增速",
    "roe": "净资产收益率",
    "roa": "总资产收益率",
    "net_margin": "销售净利率",
    "debt_ratio_neg": "低杠杆(资产负债率负向)",
    "ret_1m": "近1月收益率",
    "ret_3m": "近3月收益率",
    "ret_6m": "近6月收益率",
    "vol_20d": "20日年化波动率(负向)",
    "mdd_neg": "低回撤(最大回撤负向)",
}

FACTOR_WEIGHTS = {
    "ep": 0.5, "bp": 0.5,
    "profit_growth": 0.5, "revenue_growth": 0.5,
    "roe": 0.4, "roa": 0.3, "net_margin": 0.2, "debt_ratio_neg": 0.1,
    "ret_1m": 0.3, "ret_3m": 0.4, "ret_6m": 0.3,
    "vol_20d": 0.6, "mdd_neg": 0.4,
}

CATEGORY_WEIGHTS = {
    "value": 0.20, "growth": 0.15, "quality": 0.25,
    "momentum": 0.25, "volatility": 0.15,
}

_cache = {}
_cache_lock = threading.Lock()
TTL = 300


def _safe_float(x):
    if x is None:
        return None
    try:
        v = float(x)
        return None if np.isnan(v) or np.isinf(v) else v
    except (TypeError, ValueError):
        return None


def calc_kline_factors(df):
    """基于K线计算动量和波动率因子"""
    df = analysis.add_indicators(df.copy())
    close = df["close"]
    pct = df["pct"]
    n = len(df)
    factors = {}

    # 动量因子
    if n > 21:
        factors["ret_1m"] = _safe_float((close.iloc[-1] / close.iloc[-21] - 1) * 100)
    if n > 63:
        factors["ret_3m"] = _safe_float((close.iloc[-1] / close.iloc[-63] - 1) * 100)
    if n > 126:
        factors["ret_6m"] = _safe_float((close.iloc[-1] / close.iloc[-126] - 1) * 100)

    # 波动率因子
    if n > 20:
        vol_20 = pct.tail(20).std()
        factors["vol_20d"] = _safe_float(vol_20 * np.sqrt(244))  # 年化
    # 最大回撤（近250日）
    if n > 20:
        window = close.tail(min(250, n))
        cummax = window.cummax()
        dd = (window / cummax - 1) * 100
        factors["mdd_neg"] = _safe_float(abs(dd.min()))  # 负向因子：值越小越好

    return factors


def calc_fundamental_factors(code):
    """基于基本面数据计算价值/成长/质量因子

    优先用新浪财务页（字段更全），缺失字段回落到 market_data 全市场快照。
    """
    f = fund.get_fundamental(code)
    val = f.get("valuation", {})
    fin = f.get("financial", {})
    snap = market_data.get_fin_snapshot(code) or {}
    factors = {}

    # 价值因子
    pe = _safe_float(val.get("pe"))
    if pe and pe > 0:
        factors["ep"] = round(1.0 / pe * 100, 4)  # 盈利收益率%
    pb = _safe_float(val.get("pb"))
    if pb and pb > 0:
        factors["bp"] = round(1.0 / pb, 4)  # 账面市值比

    def _pick(key, *sources):
        for src in sources:
            v = _safe_float(src.get(key))
            if v is not None:
                return v
        return None

    # 成长因子
    factors["profit_growth"] = _pick("profit_growth", fin, snap)
    factors["revenue_growth"] = _pick("revenue_growth", fin, snap)

    # 质量因子
    factors["roe"] = _pick("roe", fin, snap)
    factors["roa"] = _pick("roa", fin, snap)
    factors["net_margin"] = _pick("net_margin", fin, snap)
    debt = _pick("debt_ratio", fin, snap)
    if debt is not None:
        factors["debt_ratio_neg"] = round(100 - debt, 2)  # 负向转换：杠杆越低分越高

    return factors


def get_factor_exposure(code):
    """单股因子暴露：各因子值 + 全市场分位"""
    code = ds.norm_code(code)
    key = f"factor_{code}"
    now = time.time()
    with _cache_lock:
        if key in _cache and now - _cache[key]["ts"] < TTL:
            return _cache[key]["data"]

    # 计算该股因子值
    rows = ds.get_kline(code)
    df = analysis.build_df(rows)
    kf = calc_kline_factors(df) if len(df) > 20 else {}
    ff = calc_fundamental_factors(code)
    my_factors = {**kf, **ff}

    # 计算全市场分位
    percentiles = calc_percentiles(my_factors)

    # 分类得分（0-100）
    category_scores = {}
    for cat, factor_keys in FACTOR_DEFS.items():
        vals = []
        for fk in factor_keys:
            p = percentiles.get(fk)
            if p is not None:
                vals.append(p)
        category_scores[cat] = round(sum(vals) / len(vals), 1) if vals else None

    # 综合得分
    total = 0
    weight_sum = 0
    for cat, score in category_scores.items():
        if score is not None:
            w = CATEGORY_WEIGHTS[cat]
            total += score * w
            weight_sum += w
    composite = round(total / weight_sum, 1) if weight_sum else None

    result = dict(
        code=code,
        name=ds.get_stock_name(code),
        industry=market_data.get_industry(code),
        factors=my_factors,
        percentiles=percentiles,
        category_scores=category_scores,
        composite_score=composite,
        factor_names=FACTOR_NAMES,
        update_time=dt.datetime.now().strftime("%Y-%m-%d %H:%M"),
    )
    with _cache_lock:
        _cache[key] = {"ts": now, "data": result}
    return result


def calc_percentiles(my_factors):
    """计算各因子在全市场的分位（0-100，分数越高越优）

    数据来源：
    - 价值因子（E/P、B/P）：stocks 表 PE/PB 全市场分布
    - 成长/质量因子：market_data 全市场财务快照（东财批量报表，约 5500 只）
    - 动量因子：全市场实时快照涨跌幅分布（近似，非真实区间收益率）
    - 波动率因子：market_data 抽样（约 160 只）近 250 日真实波动率/回撤分布
    """
    percentiles = {}
    # 价值因子用 stocks 表 PE/PB 分布
    try:
        with ds._db() as conn:
            rows = conn.execute(
                "SELECT pe,pb FROM stocks WHERE pe>0 AND pb>0 AND pe<2000"
            ).fetchall()
        pes = sorted(float(r[0]) for r in rows)
        pbs = sorted(float(r[1]) for r in rows)

        # E/P 分位 = PE 的反向分位（PE越小，E/P越高，分位越高）
        pe = my_factors.get("ep")
        if pe is not None and pes:
            # ep=1/pe*100, 对应的pe=100/ep
            implied_pe = 100.0 / pe if pe > 0 else float("inf")
            cnt = sum(1 for x in pes if x <= implied_pe)
            percentiles["ep"] = round((1 - cnt / len(pes)) * 100, 1)

        pb = my_factors.get("bp")
        if pb is not None and pbs:
            implied_pb = 1.0 / pb if pb > 0 else float("inf")
            cnt = sum(1 for x in pbs if x <= implied_pb)
            percentiles["bp"] = round((1 - cnt / len(pbs)) * 100, 1)
    except Exception:  # noqa: BLE001
        pass

    # 成长/质量/波动率因子：全市场横截面分布（正向因子直接取分位，负向因子反转）
    try:
        dist = market_data.get_distributions()
        # (因子键, 分布键, 是否负向)
        spec = [
            ("profit_growth", "profit_growth", False),
            ("revenue_growth", "revenue_growth", False),
            ("roe", "roe", False),
            ("roa", "roa", False),
            ("net_margin", "net_margin", False),
            ("debt_ratio_neg", "debt_ratio_neg", False),
            ("vol_20d", "vol_20d", True),
            ("mdd_neg", "mdd", True),
        ]
        for fk, dk, negative in spec:
            v = my_factors.get(fk)
            arr = dist.get(dk) or []
            if v is None or not arr:
                continue
            p = market_data.percentile_rank(arr, v)
            if p is not None:
                percentiles[fk] = round(100 - p, 1) if negative else p
    except Exception:  # noqa: BLE001
        pass

    # 动量因子：用全市场实时快照涨跌幅近似分布（非真实区间收益，仅作相对强弱参考）
    try:
        snap = ds.get_realtime_snapshot()
        pcts = sorted(
            (q["price"] / q["prev_close"] - 1) * 100
            for q in snap.values()
            if q.get("price") and q.get("prev_close") and q["prev_close"] > 0
        )
        for fk in ("ret_1m", "ret_3m", "ret_6m"):
            v = my_factors.get(fk)
            if v is not None and pcts:
                cnt = sum(1 for x in pcts if x <= v)
                percentiles[fk] = round(cnt / len(pcts) * 100, 1)
    except Exception:  # noqa: BLE001
        pass

    return percentiles


def evaluate_ic(factor_name, lookback=252):
    """因子IC评估：因子值与未来收益的秩相关系数
    这里用单股自身的时序IC近似（因子值用标准化的历史收益率代理）"""
    # 简化版：用动量因子(ret_1m)的IC作为示例
    # 真正的全市场IC需要拉取所有股票的因子值和收益，耗时较长
    rows = ds.get_kline(factor_name) if len(factor_name) == 6 and factor_name.isdigit() else None
    if not rows:
        return dict(factor=factor_name, ic_mean=None, icir=None, ic_win_rate=None, note="需传入股票代码进行时序IC评估")
    df = analysis.build_df(rows)
    if len(df) < lookback + 20:
        return dict(factor=factor_name, ic_mean=None, icir=None, note="历史数据不足")
    df = analysis.add_indicators(df)
    # 用过去20日收益率作为因子值，未来5日收益率作为收益
    df["factor"] = df["close"].pct_change(20) * 100
    df["fwd_ret"] = df["close"].shift(-5) / df["close"] - 1
    df = df.dropna(subset=["factor", "fwd_ret"])
    if len(df) < 30:
        return dict(factor=factor_name, ic_mean=None, icir=None, note="有效样本不足")
    # 秩相关
    from scipy.stats import spearmanr
    ic_vals = []
    window = 60
    for i in range(window, len(df)):
        sub = df.iloc[i - window : i]
        if len(sub) < 20:
            continue
        try:
            ic, _ = spearmanr(sub["factor"], sub["fwd_ret"])
            if not np.isnan(ic):
                ic_vals.append(ic)
        except Exception:
            continue
    if not ic_vals:
        return dict(factor=factor_name, ic_mean=None, icir=None, note="IC计算失败")
    ic_arr = np.array(ic_vals)
    ic_mean = float(np.mean(ic_arr))
    ic_std = float(np.std(ic_arr))
    icir = ic_mean / ic_std if ic_std > 0 else 0
    ic_win = float(np.mean(ic_arr > 0) * 100)
    return dict(
        factor=factor_name,
        ic_mean=round(ic_mean, 4),
        icir=round(icir, 3),
        ic_win_rate=round(ic_win, 1),
        sample_count=len(ic_vals),
        note="时序IC(因子=20日动量, 收益=未来5日)",
    )
