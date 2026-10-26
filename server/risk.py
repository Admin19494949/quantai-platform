# -*- coding: utf-8 -*-
"""风险模型：波动率 / VaR / Beta / 回撤 / 行业暴露 / 风格暴露 / 风险贡献 / 风险平价

输入为持仓代码与权重（不传权重则等权），历史窗口默认 250 个交易日。
风险平价权重使用协方差矩阵的等风险贡献（ERC）迭代求解，而非简单逆波动率。
"""
import numpy as np
import pandas as pd

from . import datasource as ds, market_data, portfolio

TRADING_DAYS = 244
VAR_Z = 1.645          # 95% 单尾正态分位

_mkt_cache = {"ts": 0.0, "data": None}


def _market_returns(days=250):
    """市场代理收益：默认股票池等权日收益（带 10 分钟缓存）"""
    import time
    if _mkt_cache["data"] is not None and time.time() - _mkt_cache["ts"] < 600:
        return _mkt_cache["data"]
    items = portfolio.default_universe(portfolio.DEFAULT_UNIVERSE_SIZE)
    data = portfolio._load_all(items, "factor", days + 20)
    if len(data) < 5:
        return None
    price = pd.DataFrame({d["code"]: pd.Series(d["close"].values, index=d["day"])
                          for d in data}).sort_index()
    price = price.ffill().dropna(how="any")
    rets = price.pct_change().dropna()
    mkt = rets.mean(axis=1)
    _mkt_cache["data"] = mkt
    _mkt_cache["ts"] = time.time()
    return mkt


def _load_returns(codes, days):
    items = portfolio._resolve_universe(codes)
    data = portfolio._load_all(items, "factor", days + 20)
    if len(data) < 2:
        return None, None
    price = pd.DataFrame({d["code"]: pd.Series(d["close"].values, index=d["day"])
                          for d in data}).sort_index()
    coverage = price.notna().sum(axis=1)
    calendar = price.index[coverage >= max(2, int(len(data) * 0.6))][-days:]
    price = price.reindex(calendar).ffill().dropna(how="any")
    if price.shape[1] < 2:
        return None, None
    return price, price.pct_change().dropna()


def _erc_weights(cov, iters=800, tol=1e-7):
    """等风险贡献（ERC）权重，长仓约束"""
    n = cov.shape[0]
    w = np.ones(n) / n
    for _ in range(iters):
        mrc = cov @ w
        mrc = np.maximum(mrc, 1e-12)
        w_new = 1.0 / mrc
        w_new /= w_new.sum()
        if np.max(np.abs(w_new - w)) < tol:
            w = w_new
            break
        w = w_new
    return w


def analyze(codes=None, weights=None, days=250, capital=1_000_000.0):
    """组合风险分析

    codes: 股票代码列表（为空用默认股票池）
    weights: dict code -> 权重（百分比或小数均可，内部归一化）
    capital: 用于把 VaR 换算成金额
    """
    price, rets = _load_returns(codes, days)
    if price is None:
        return dict(error="行情数据不足，无法计算风险指标")
    codes_used = list(price.columns)
    n = len(codes_used)

    if weights:
        raw = np.array([float(weights.get(c, 0) or 0) for c in codes_used])
        if raw.sum() <= 0:
            raw = np.ones(n)
    else:
        raw = np.ones(n)
    w = raw / raw.sum()

    cov = rets.cov().values * TRADING_DAYS
    port_ret_daily = rets.values @ w
    vol_annual = float(np.sqrt(max(w @ cov @ w, 0)) * 100)
    nav = (1 + pd.Series(port_ret_daily)).cumprod()
    cummax = nav.cummax()
    dd = (nav / cummax - 1)
    max_dd = float(dd.min() * 100)

    # VaR / ES（95%）
    daily_vol = float(np.std(port_ret_daily, ddof=1))
    var95_pct = daily_vol * VAR_Z * 100
    hist_var = float(np.percentile(port_ret_daily, 5) * 100)
    tail = port_ret_daily[port_ret_daily <= np.percentile(port_ret_daily, 5)]
    es95_pct = float(tail.mean() * 100) if len(tail) else hist_var

    # 基准：默认股票池等权（市场代理），用于 Beta
    mkt = _market_returns(days)
    if mkt is not None:
        mkt = mkt.reindex(rets.index).dropna()
        if len(mkt) > 30:
            p = pd.Series(port_ret_daily, index=rets.index).reindex(mkt.index)
            bvar = float(np.var(mkt.values, ddof=1))
            beta = float(np.cov(p.values, mkt.values)[0, 1] / bvar) if bvar > 0 else None
        else:
            beta = None
    else:
        beta = None
    ann_ret = float((nav.iloc[-1] ** (TRADING_DAYS / max(len(nav), 1)) - 1) * 100)
    sharpe = ann_ret / vol_annual if vol_annual > 0 else 0

    # 风险贡献
    mrc = cov @ w
    port_var = float(w @ cov @ w)
    rc = w * mrc / port_var if port_var > 0 else np.zeros(n)
    risk_contrib = sorted(
        [dict(code=c, weight=round(w[i] * 100, 1), risk_contrib=round(float(rc[i]) * 100, 1))
         for i, c in enumerate(codes_used)], key=lambda x: -x["risk_contrib"])

    # 风险平价建议权重
    erc = _erc_weights(cov)
    risk_parity = sorted(
        [dict(code=c, weight=round(float(erc[i]) * 100, 1)) for i, c in enumerate(codes_used)],
        key=lambda x: -x["weight"])

    # 相关性（最高相关对）
    corr = rets.corr()
    pairs = []
    for i in range(n):
        for j in range(i + 1, n):
            pairs.append(dict(a=codes_used[i], b=codes_used[j],
                              corr=round(float(corr.iloc[i, j]), 3)))
    pairs.sort(key=lambda x: -x["corr"])

    # 行业暴露
    ind_map = market_data.get_industry_map()
    ind_exp = {}
    for i, c in enumerate(codes_used):
        ind = ind_map.get(c) or "其他"
        ind_exp[ind] = ind_exp.get(ind, 0.0) + w[i] * 100
    ind_exp = {k: round(float(v), 1) for k, v in sorted(ind_exp.items(), key=lambda x: -x[1])}
    hhi = float(sum((v / 100) ** 2 for v in ind_exp.values()))
    top_weight = float(max(w) * 100)

    # 风格暴露（0-100 分位，越高代表该风格暴露越强）
    style = _style_exposure(codes_used, w, price)

    # 压力测试：历史上最差的 5 日、20 日累计收益
    s = pd.Series(port_ret_daily)
    roll5 = (1 + s).rolling(5).apply(np.prod, raw=True) - 1
    roll20 = (1 + s).rolling(20).apply(np.prod, raw=True) - 1
    stress = dict(
        worst_5d=round(float(roll5.min()) * 100, 2) if roll5.notna().any() else None,
        worst_20d=round(float(roll20.min()) * 100, 2) if roll20.notna().any() else None,
        vol_regime=dict(
            recent_20d=round(float(np.std(port_ret_daily[-20:], ddof=1) * np.sqrt(TRADING_DAYS) * 100), 2)
            if len(port_ret_daily) >= 20 else None,
            full=round(vol_annual, 2),
        ),
    )

    return dict(
        period=dict(start=str(price.index[0]), end=str(price.index[-1]), days=len(price)),
        capital=capital,
        holdings=[dict(code=c, name=ds.get_stock_name(c) or c,
                       industry=ind_map.get(c) or "其他",
                       weight=round(float(w[i]) * 100, 1)) for i, c in enumerate(codes_used)],
        risk=dict(
            annual_volatility=round(vol_annual, 2),
            var95_pct=round(var95_pct, 2),
            var95_amount=round(capital * var95_pct / 100, 2),
            historical_var95_pct=round(hist_var, 2),
            expected_shortfall95_pct=round(es95_pct, 2),
            max_drawdown=round(max_dd, 2),
            beta=round(beta, 2) if beta is not None else None,
            cagr=round(ann_ret, 2),
            sharpe=round(sharpe, 2),
            portfolio_hhi=round(hhi, 3),
            max_single_weight=round(top_weight, 1),
        ),
        industry_exposure=ind_exp,
        style_exposure=style,
        risk_contribution=risk_contrib,
        risk_parity_weights=risk_parity,
        top_correlations=pairs[:8],
        stress_test=stress,
        disclaimer=("风险指标基于历史日收益率与协方差矩阵估算，VaR 采用 95% 置信度；"
                    "历史表现不代表未来，极端行情下实际损失可能超过 VaR。"),
    )


def _style_exposure(codes, w, price):
    """风格暴露：价值/成长/质量/动量/低波动，取全市场（或股票池）分位加权平均"""
    dist = market_data.get_distributions()
    snaps = {s["code"]: s for s in market_data.get_all_snapshots()}
    try:
        rows = ds._db().execute(
            f"SELECT code,pe FROM stocks WHERE code IN ({','.join('?'*len(codes))})",
            codes).fetchall()
        pe_map = {r[0]: r[1] for r in rows}
    except Exception:  # noqa: BLE001
        pe_map = {}
    ep_arr = sorted(1.0 / float(p) for p in pe_map.values() if p and p > 0)

    ret60, vol20 = {}, {}
    for c in codes:
        s = price[c]
        ret60[c] = float(s.iloc[-1] / s.iloc[-61] - 1) * 100 if len(s) > 61 else None
        r = s.pct_change().dropna()
        vol20[c] = float(r.iloc[-20:].std() * np.sqrt(TRADING_DAYS) * 100) if len(r) >= 20 else None
    ret60_arr = sorted(v for v in ret60.values() if v is not None)
    vol_arr = sorted(v for v in vol20.values() if v is not None)

    pct = market_data.percentile_rank
    scores = {"value": [], "growth": [], "quality": [], "momentum": [], "low_vol": []}
    for i, c in enumerate(codes):
        s = snaps.get(c) or {}
        pe = pe_map.get(c)
        if pe and pe > 0:
            v = pct(ep_arr, 1.0 / float(pe))
            if v is not None:
                scores["value"].append((w[i], v))
        v = pct(dist.get("profit_growth") or [], s.get("profit_growth"))
        if v is not None:
            scores["growth"].append((w[i], v))
        v = pct(dist.get("roe") or [], s.get("roe"))
        if v is not None:
            scores["quality"].append((w[i], v))
        v = pct(ret60_arr, ret60.get(c))
        if v is not None:
            scores["momentum"].append((w[i], v))
        v = pct(vol_arr, vol20.get(c))
        if v is not None:
            scores["low_vol"].append((w[i], 100 - v))   # 波动越低，低波动风格暴露越高

    out = {}
    for k, pairs in scores.items():
        if not pairs:
            out[k] = None
            continue
        tot = sum(x[0] for x in pairs)
        out[k] = round(float(sum(x[0] * x[1] for x in pairs) / tot), 1) if tot > 0 else None
    return out
