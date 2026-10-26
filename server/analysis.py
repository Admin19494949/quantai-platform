# -*- coding: utf-8 -*-
"""量化分析引擎：技术指标 / 每日分析 / 上市全历史分析 / 短期涨跌多因子信号"""
import datetime as dt

import numpy as np
import pandas as pd


# ---------------------------------------------------------------- 基础


def build_df(rows):
    df = pd.DataFrame(rows)
    for col in ("open", "high", "low", "close", "volume"):
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df = df.dropna()
    df = df[df["close"] > 0].reset_index(drop=True)  # 剔除停牌/脏数据的零价行
    df["pct"] = df["close"].pct_change().replace([np.inf, -np.inf], np.nan) * 100
    return df


def add_indicators(df):
    c, h, l, v = df["close"], df["high"], df["low"], df["volume"]
    for n in (5, 10, 20, 60, 120, 250):
        if len(df) >= n:
            df[f"ma{n}"] = c.rolling(n).mean()
    df["vol_ma5"] = v.rolling(5).mean()
    # MACD（12,26,9）
    ema12 = c.ewm(span=12, adjust=False).mean()
    ema26 = c.ewm(span=26, adjust=False).mean()
    df["dif"] = ema12 - ema26
    df["dea"] = df["dif"].ewm(span=9, adjust=False).mean()
    df["macd"] = 2 * (df["dif"] - df["dea"])
    # RSI（Wilder）
    for n in (6, 12, 24):
        diff = c.diff()
        up = diff.clip(lower=0).ewm(alpha=1 / n, adjust=False).mean()
        dn = (-diff.clip(upper=0)).ewm(alpha=1 / n, adjust=False).mean()
        df[f"rsi{n}"] = 100 - 100 / (1 + up / dn.replace(0, np.nan))
    # KDJ（9,3,3）
    low9 = l.rolling(9).min()
    high9 = h.rolling(9).max()
    rsv = (c - low9) / (high9 - low9).replace(0, np.nan) * 100
    df["kdj_k"] = rsv.ewm(com=2, adjust=False).mean()
    df["kdj_d"] = df["kdj_k"].ewm(com=2, adjust=False).mean()
    df["kdj_j"] = 3 * df["kdj_k"] - 2 * df["kdj_d"]
    # BOLL（20,2）
    mid = c.rolling(20).mean()
    std = c.rolling(20).std()
    df["boll_mid"], df["boll_up"], df["boll_low"] = mid, mid + 2 * std, mid - 2 * std
    # ATR14
    tr = pd.concat([h - l, (h - c.shift()).abs(), (l - c.shift()).abs()], axis=1).max(axis=1)
    df["atr14"] = tr.ewm(alpha=1 / 14, adjust=False).mean()
    return df


def _last(df, col):
    s = df[col].dropna()
    return None if s.empty else float(s.iloc[-1])


def _f(x, n=2):
    if x is None:
        return None
    try:
        x = float(x)
    except (TypeError, ValueError):
        return None
    if np.isnan(x) or np.isinf(x):
        return None
    return round(x, n)


def serialize_kline(df, columns=None):
    cols = columns or ["day", "open", "high", "low", "close", "volume", "pct",
                       "ma5", "ma10", "ma20", "ma60", "vol_ma5", "dif", "dea", "macd"]
    cols = [c for c in cols if c in df.columns]
    out = df[cols].copy()
    return json_records(out)


def json_records(df):
    res = []
    for row in df.itertuples(index=False):
        d = {}
        for k, v in zip(df.columns, row):
            if isinstance(v, float):
                d[k] = None if (np.isnan(v) or np.isinf(v)) else round(v, 4)
            else:
                d[k] = v
        res.append(d)
    return res


def serialize_kline_compact(df, columns=None):
    """K 线压缩为 fields + rows 数组：全量历史（数千行）体积比字典数组小一半左右，
    用于公网隧道等低带宽场景。取用字段与 serialize_kline 保持一致。"""
    cols = columns or ["day", "open", "high", "low", "close", "volume",
                       "ma5", "ma10", "ma20", "ma60", "dif", "dea", "macd"]
    cols = [c for c in cols if c in df.columns]
    out = df[cols]
    rows = []
    for row in out.itertuples(index=False):
        r = []
        for v in row:
            if isinstance(v, float):
                r.append(None if (np.isnan(v) or np.isinf(v)) else round(v, 4))
            else:
                r.append(v)
        rows.append(r)
    return {"fields": list(out.columns), "rows": rows}


# ---------------------------------------------------------------- 每日交易数据分析


def daily_analysis(code, name, df):
    df = add_indicators(df.copy())
    r = df.iloc[-1]
    close = float(r["close"])
    ind = {
        "close": _f(close, 3), "pct": _f(r["pct"]),
        "ma": {f"ma{n}": _f(r.get(f"ma{n}"), 3) for n in (5, 10, 20, 60)},
        "macd": {"dif": _f(r["dif"], 3), "dea": _f(r["dea"], 3), "hist": _f(r["macd"], 3)},
        "rsi": {"rsi6": _f(r["rsi6"]), "rsi12": _f(r["rsi12"]), "rsi24": _f(r["rsi24"])},
        "kdj": {"k": _f(r["kdj_k"]), "d": _f(r["kdj_d"]), "j": _f(r["kdj_j"])},
        "boll": {"up": _f(r["boll_up"], 3), "mid": _f(r["boll_mid"], 3), "low": _f(r["boll_low"], 3)},
        "volume": {"vol": _f(r["volume"], 0), "vol_ma5": _f(r["vol_ma5"], 0),
                   "vol_ratio": _f(r["volume"] / r["vol_ma5"], 2) if r["vol_ma5"] else None},
        "atr14": _f(r["atr14"], 3),
    }
    signals = []

    def sig(nm, verdict, desc):
        signals.append(dict(name=nm, verdict=verdict, desc=desc))  # verdict: 多/空/中性

    # 均线
    ma_state = []
    for n in (5, 10, 20, 60):
        m = r.get(f"ma{n}")
        if m and not np.isnan(m):
            ma_state.append((n, close > m))
    above = sum(1 for _, ok in ma_state)
    if above == len(ma_state):
        sig("均线系统", "多", "价格站上全部主要均线，多头排列")
    elif above == 0:
        sig("均线系统", "空", "价格跌破全部主要均线，空头排列")
    else:
        sig("均线系统", "中性", f"价格位于均线之间（{above}/{len(ma_state)}之上），趋势纠缠")
    # MACD
    if r["dif"] > r["dea"]:
        cross = "近3日金叉" if (df["dif"].iloc[-3:] < df["dea"].iloc[-3:]).any() and df["dif"].iloc[-4:].diff().iloc[-1] else "多头运行"
        sig("MACD", "多", f"DIF({ind['macd']['dif']}) 高于 DEA({ind['macd']['dea']})，红柱放大，{cross}")
    else:
        sig("MACD", "空", f"DIF({ind['macd']['dif']}) 低于 DEA({ind['macd']['dea']})，绿柱显示空头动能")
    # RSI
    rsi6 = r["rsi6"]
    if np.isnan(rsi6):
        sig("RSI", "中性", "样本不足")
    elif rsi6 >= 80:
        sig("RSI", "空", f"RSI6={rsi6:.1f} 严重超买，短线回调风险大")
    elif rsi6 >= 60:
        sig("RSI", "多", f"RSI6={rsi6:.1f} 处于强势区")
    elif rsi6 <= 20:
        sig("RSI", "多", f"RSI6={rsi6:.1f} 严重超卖，存在技术性反弹需求")
    elif rsi6 <= 40:
        sig("RSI", "空", f"RSI6={rsi6:.1f} 处于弱势区")
    else:
        sig("RSI", "中性", f"RSI6={rsi6:.1f} 中性区间")
    # KDJ
    j = r["kdj_j"]
    if j > 100:
        sig("KDJ", "空", f"J值={j:.1f} 超买钝化")
    elif j < 0:
        sig("KDJ", "多", f"J值={j:.1f} 超卖，随时可能金叉反弹")
    else:
        sig("KDJ", "中性", f"K={r['kdj_k']:.1f} D={r['kdj_d']:.1f} J={j:.1f}")
    # BOLL
    if close > r["boll_up"]:
        sig("布林带", "多", "价格突破上轨，超强走势（注意乖离）")
    elif close < r["boll_low"]:
        sig("布林带", "空", "价格跌破下轨，超跌状态")
    else:
        pos = (close - r["boll_low"]) / (r["boll_up"] - r["boll_low"]) * 100
        sig("布林带", "中性", f"价格处于带内 {pos:.0f}% 位置")
    # 量能
    vr = ind["volume"]["vol_ratio"]
    if vr:
        chg_up = (r["pct"] or 0) > 0
        if vr >= 1.5:
            sig("量能", "多" if chg_up else "空", f"量比 {vr} 显著放量，{('上攻有量' if chg_up else '放量下跌需警惕')}")
        elif vr <= 0.6:
            sig("量能", "中性", f"量比 {vr} 明显缩量，观望情绪浓")
        else:
            sig("量能", "中性", f"量比 {vr} 温和")

    # 区间统计
    for n in (5, 20, 60):
        pass
    d5 = df.tail(6)
    stats = {
        "pct_5d": _f((close / d5["close"].iloc[0] - 1) * 100),
        "pct_20d": _f((close / df["close"].iloc[-21] - 1) * 100) if len(df) > 21 else None,
        "pct_60d": _f((close / df["close"].iloc[-61] - 1) * 100) if len(df) > 61 else None,
        "high_20d": _f(df["high"].tail(20).max(), 3),
        "low_20d": _f(df["low"].tail(20).min(), 3),
        "avg_amount_5d_vol": _f(df["volume"].tail(5).mean(), 0),
        "up_days_20": int((df["pct"].tail(20) > 0).sum()),
        "down_days_20": int((df["pct"].tail(20) < 0).sum()),
    }
    # 支撑 / 压力
    support = max(_f(df["low"].tail(20).min(), 3) or 0, _f(r["boll_low"], 3) or 0)
    resistance = _f(df["high"].tail(20).max(), 3)
    # 月度季节性（基于上市以来全部数据，各月平均收益）
    m = df.set_index(pd.to_datetime(df["day"]))
    mret = m["close"].resample("ME").last().pct_change().replace([np.inf, -np.inf], np.nan).dropna() * 100
    monthly_avg = [None] * 12
    if len(mret):
        g = mret.groupby(mret.index.month).mean()
        monthly_avg = [round(float(g.get(i, np.nan)), 2) if i in g.index else None for i in range(1, 13)]
    return dict(
        code=code, name=name, indicators=ind, signals=signals, stats=stats,
        support_resistance={"support": support, "resistance": resistance},
        kline=serialize_kline(df.tail(250)),
        monthly_avg=monthly_avg,
        date=str(r["day"]),
    )


# ---------------------------------------------------------------- 上市以来全历史分析


def full_history_analysis(code, name, df, compact_kline=False):
    df = add_indicators(df.copy())
    n = len(df)
    first, last = df.iloc[0], df.iloc[-1]
    years = max(n / 244.0, 1 / 244.0)
    close = df["close"]
    total_ret = (last["close"] / first["close"] - 1) * 100 if first["close"] > 0 else None
    cagr = ((last["close"] / first["close"]) ** (1 / years) - 1) * 100 if last["close"] > 0 and first["close"] > 0 else None
    daily_ret = close.pct_change().replace([np.inf, -np.inf], np.nan).dropna()
    annual_vol = float(daily_ret.std() * np.sqrt(244) * 100)
    sharpe = (cagr / annual_vol) if cagr is not None and annual_vol else None
    # 最大回撤
    cummax = close.cummax()
    dd = close / cummax - 1
    mdd_idx = int(dd.idxmin())
    mdd = float(dd.min()) * 100
    peak_idx = int(close[: mdd_idx + 1].idxmax())
    # 月度收益
    m = df.set_index(pd.to_datetime(df["day"]))
    mret = m["close"].resample("ME").last().pct_change().replace([np.inf, -np.inf], np.nan).dropna() * 100
    monthly_avg = [None] * 12
    if len(mret):
        g = mret.groupby(mret.index.month).mean()
        monthly_avg = [round(float(g.get(i, np.nan)), 2) if i in g.index else None for i in range(1, 13)]
    # 年度收益
    yclose = m["close"].resample("YE").last()
    yret = (yclose.pct_change().replace([np.inf, -np.inf], np.nan) * 100).dropna()
    yearly = []
    for ts, v in yret.items():
        yearly.append(dict(year=str(ts.year), ret=round(float(v), 2)))
    if not yearly and not yclose.empty:
        yearly.append(dict(year=str(yclose.index[-1].year),
                           ret=round(float((yclose.iloc[-1] / yclose.iloc[0] - 1) * 100), 2)))
    # 日收益分布（-8% ~ +8%，1%一档）
    hist, edges = np.histogram(daily_ret.clip(-8, 8), bins=16, range=(-8, 8))
    distribution = [dict(lo=round(edges[i], 1), hi=round(edges[i + 1], 1), count=int(hist[i])) for i in range(16)]
    up_days = int((daily_ret > 0).sum())
    win_rate = up_days / len(daily_ret) * 100 if len(daily_ret) else 0
    # 最值
    di = daily_ret
    best_i, worst_i = int(di.idxmax()), int(di.idxmin())
    best = dict(date=str(df.loc[best_i, "day"]), pct=round(float(di.max()), 2))
    worst = dict(date=str(df.loc[worst_i, "day"]), pct=round(float(di.min()), 2))
    return dict(
        code=code, name=name,
        ipo_date=str(first["day"]), last_date=str(last["day"]),
        total_days=n, years=round(years, 1),
        return_metrics=dict(
            total_return=_f(total_ret), cagr=_f(cagr), annual_volatility=_f(annual_vol),
            sharpe=_f(sharpe), max_drawdown=_f(mdd),
            max_dd_start=str(df.loc[peak_idx, "day"]), max_dd_end=str(df.loc[mdd_idx, "day"]),
            daily_win_rate=_f(win_rate, 1), up_days=up_days, down_days=int((daily_ret < 0).sum()),
        ),
        yearly=yearly, monthly_avg=monthly_avg, distribution=distribution,
        best_worst=dict(best_day=best, worst_day=worst),
        kline=(serialize_kline_compact(df) if compact_kline else serialize_kline(df)),
    )


# ---------------------------------------------------------------- 短期涨跌预测（多因子评分）


def short_term_analysis(code, name, df):
    df = add_indicators(df.copy())
    r = df.iloc[-1]
    close = float(r["close"])
    factors = []

    def add(nm, s, detail):
        factors.append(dict(name=nm, score=round(s, 1), detail=detail))  # score: -1 ~ 1

    # 1. 均线趋势（权重20）
    ma_up20 = r["ma20"] is not None and not np.isnan(r["ma20"]) and len(df) > 1 and \
        (np.isnan(df["ma20"].iloc[-2]) or r["ma20"] >= df["ma20"].iloc[-2])
    if close > r["ma5"] > r["ma10"] > r["ma20"]:
        s1, d1 = 1.0, "多头排列：收盘价 > MA5 > MA10 > MA20"
    elif close > r["ma20"] and ma_up20:
        s1, d1 = 0.6, "价格站上20日线且20日线向上"
    elif close < r["ma5"] < r["ma10"] < r["ma20"]:
        s1, d1 = -1.0, "空头排列：收盘价 < MA5 < MA10 < MA20"
    elif close < r["ma20"]:
        s1, d1 = -0.6, "价格跌破20日线，趋势转弱"
    else:
        s1, d1 = 0.0, "均线纠缠，方向不明"
    add("均线趋势", s1, d1)
    # 2. MACD（15）
    prev = df.iloc[-2]
    golden = prev["dif"] <= prev["dea"] < r["dif"] or (r["dif"] > r["dea"] and df["dif"].iloc[-5] <= df["dea"].iloc[-5])
    dead = prev["dif"] >= prev["dea"] > r["dif"]
    hist_grow = r["macd"] > prev["macd"]
    if golden:
        s2, d2 = 0.9, f"MACD刚金叉（DIF {r['dif']:.3f} 上穿 DEA {r['dea']:.3f}）"
    elif dead:
        s2, d2 = -0.9, f"MACD死叉（DIF {r['dif']:.3f} 下穿 DEA {r['dea']:.3f}）"
    elif r["dif"] > r["dea"]:
        s2, d2 = (0.5 if hist_grow else 0.25), "多头动能运行" + ("，红柱放大" if hist_grow else "")
    else:
        s2, d2 = (-0.5 if hist_grow else -0.25), "空头动能运行" + ("，绿柱放大" if hist_grow else "")
    add("MACD动能", s2, d2)
    # 3. RSI（12）
    rsi6 = r["rsi6"]
    if np.isnan(rsi6):
        s3, d3 = 0, "样本不足"
    elif rsi6 >= 80:
        s3, d3 = -0.6, f"RSI6={rsi6:.0f} 严重超买，回踩概率大"
    elif rsi6 >= 60:
        s3, d3 = 0.7, f"RSI6={rsi6:.0f} 强势区，多头占优"
    elif rsi6 <= 20:
        s3, d3 = 0.5, f"RSI6={rsi6:.0f} 超卖，反弹概率上升"
    elif rsi6 <= 40:
        s3, d3 = -0.5, f"RSI6={rsi6:.0f} 弱势区"
    else:
        s3, d3 = 0, f"RSI6={rsi6:.0f} 中性"
    add("RSI强弱", s3, d3)
    # 4. KDJ（10）
    prevk, prevd = prev["kdj_k"], prev["kdj_d"]
    if prevk <= prevd < r["kdj_k"] and r["kdj_j"] < 90:
        s4, d4 = 0.8, f"KDJ低位金叉（J={r['kdj_j']:.0f}）"
    elif prevk >= prevd > r["kdj_k"] and r["kdj_j"] > 10:
        s4, d4 = -0.8, f"KDJ高位死叉（J={r['kdj_j']:.0f}）"
    elif r["kdj_j"] > 100:
        s4, d4 = -0.5, "J值超买钝化"
    elif r["kdj_j"] < 0:
        s4, d4 = 0.5, "J值超卖，易引发反抽"
    else:
        s4, d4 = 0, "KDJ中性"
    add("KDJ摆动", s4, d4)
    # 5. 短期动量（18）
    ret5 = (close / df["close"].iloc[-6] - 1) * 100 if len(df) > 5 else 0
    ret10 = (close / df["close"].iloc[-11] - 1) * 100 if len(df) > 10 else 0
    s5 = max(-1, min(1, (0.5 * ret5 + 0.5 * ret10) / 8))
    add("价格动量", s5, f"近5日 {ret5:+.1f}%，近10日 {ret10:+.1f}%")
    # 6. 量价配合（15）
    vr = r["volume"] / r["vol_ma5"] if r["vol_ma5"] and r["vol_ma5"] > 0 else 1
    up = (r["pct"] or 0) > 0
    if vr >= 1.4 and up:
        s6, d6 = 0.9, f"放量上涨（量比{vr:.2f}），资金流入迹象"
    elif vr >= 1.4 and not up:
        s6, d6 = -0.9, f"放量下跌（量比{vr:.2f}），抛压沉重"
    elif vr <= 0.7 and up:
        s6, d6 = 0.1, "缩量上涨，动能存疑"
    elif vr <= 0.7:
        s6, d6 = -0.1, "缩量下跌，抛压有限"
    else:
        s6, d6 = 0.2 if up else -0.2, f"量能平稳（量比{vr:.2f}）"
    add("量价关系", s6, d6)
    # 7. 20日区间位置（10）
    h20, l20 = df["high"].tail(20).max(), df["low"].tail(20).min()
    pos20 = (close - l20) / (h20 - l20) * 100 if h20 > l20 else 50
    if pos20 >= 95:
        s7, d7 = 0.8, "创20日新高，突破形态（若放量则更强）"
    elif pos20 <= 10:
        s7, d7 = -0.5, "贴近20日新低，弱势探底"
    elif pos20 <= 35:
        s7, d7 = 0.2, "处于20日区间低位，安全边际较高"
    elif pos20 >= 75:
        s7, d7 = 0.1, "处于20日区间高位，追高风险大"
    else:
        s7, d7 = 0, f"20日区间位置 {pos20:.0f}%"
    add("区间位置", s7, d7)

    weights = {"均线趋势": 20, "MACD动能": 15, "RSI强弱": 12, "KDJ摆动": 10,
               "价格动量": 18, "量价关系": 15, "区间位置": 10}
    score = sum(f["score"] * weights[f["name"]] for f in factors)
    score = max(-100, min(100, round(score)))
    if score >= 30:
        direction, conf = "看多", ("强烈" if score >= 60 else "中等")
    elif score <= -30:
        direction, conf = "看空", ("强烈" if score <= -60 else "中等")
    else:
        direction, conf = "震荡", "信号不明确"
    up_prob = int(max(15, min(85, 50 + score * 0.35)))
    lo = close * (1 - abs(df["atr14"].iloc[-1]) / close * 2.2)
    hi = close * (1 + abs(df["atr14"].iloc[-1]) / close * 2.2)
    return dict(
        code=code, name=name, date=str(r["day"]),
        score=score, direction=direction, confidence=conf,
        up_prob_5d=up_prob,
        expected_range=[round(lo, 2), round(hi, 2)],
        factors=[dict(**f, weight=weights[f["name"]],
                      weighted=round(f["score"] * weights[f["name"]], 1)) for f in factors],
        recent_kline=serialize_kline(df.tail(60)),
    )
