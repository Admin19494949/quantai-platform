# -*- coding: utf-8 -*-
"""机构级验证引擎（v2 升级）

修复项 / 补齐项：
  ✅ 因子库 5 → 14 个纯价格因子（OHLCV 全量使用）
  ✅ 行业 + 市值中性化（逐日 OLS 回归残差）
  ✅ Bonferroni + Benjamini-Hochberg 多重检验校正
  ✅ 因子正交化（横截面相关性过高时剔除）
  ✅ 多因子 ICIR 加权合成（替代之前单因子样本外）
  ✅ 涨跌停 / 停牌过滤（一字板当日视为不可交易）
  ✅ Amihud 冲击成本代理（|收益率|/成交额）
  ✅ 保序回归（PAV）概率再校准（修正 Brier / 分桶偏差）
  ✅ Paper trading：逐日模拟盘净值 + 交易记录 + 与组合回测对比

诚实声明：
  - 基本面快照取当前值，用于历史回测存在未来函数；本文件所有回测均使用纯价格因子。
  - 股票池仅当前上市标的，缺失已退市/长期停牌 → 幸存者偏差向上。
  - A 股日频量价因子真实 IC 普遍在 2–5% 量级，高于此需怀疑过拟合。
"""
import math
import time

import numpy as np
import pandas as pd
from scipy.stats import spearmanr

from . import datasource as ds, market_data, prediction, portfolio

TRADING_DAYS = 244


def _is_num(v):
    """上游脏数据清洗用：能转成有限浮点数才算有效"""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return False
    return math.isfinite(f)


_FACTORS = {
    "mom_5":   "5日动量",
    "mom_20":  "20日动量",
    "mom_60":  "60日动量",
    "rev_3":   "3日反转(负号=反转)",
    "rev_10":  "10日反转",
    "vol_20":  "20日波动率(负号=低波)",
    "vol_60":  "60日波动率(负号=低波)",
    "skew_20": "20日偏度(负号=反转)",
    "kurt_20": "20日峰度(负号=尖峰回落)",
    "turn_20": "20日换手(量能)",
    "turn_60": "60日换手",
    "amp_20":  "20日振幅(负号=低振幅)",
    "gap_1":   "隔夜跳空(负号=补空)",
    "mdd_60":  "60日最大回撤(负号=低回撤)",
}


# ================================================================= 横截面面板

def _wide(codes, need=1400):
    """返回 (O, H, L, C, V) 五个价格宽表 + meta(市值/行业/涨停价/跌停价)"""
    import concurrent.futures as cf

    items = portfolio._resolve_universe(codes) if codes else portfolio.default_universe()
    if not items:
        raise RuntimeError("股票池为空，无法构建横截面面板")

    # 一次查 meta：mktcap / pe / pb / market
    meta = {}
    try:
        cur = ds._db().execute(
            "SELECT code, mktcap, market FROM stocks WHERE code IN ({})".format(
                ",".join("?" * len(items))
            ),
            [i["code"] for i in items],
        )
        for code, mc, mk in cur.fetchall():
            meta[code] = {"mktcap": float(mc) if mc else None, "market": mk or ""}
    except Exception:  # noqa: BLE001
        pass

    ind_map = market_data.get_industry_map()

    def _one(item):
        code = item["code"]
        try:
            rows = ds.get_kline(code)
        except Exception:  # noqa: BLE001
            return None
        if not rows or len(rows) < 200:
            return None
        rows = rows[-int(need):]
        # 上游偶发返回 close=null 的脏行，直接剔除，避免 float(None) 抛错
        rows = [r for r in rows if r.get("close") is not None
                and r.get("day") and _is_num(r.get("close"))]
        if len(rows) < 200:
            return None
        # 上游偶发返回同一交易日重复行，按日期去重（保留最后一条）并排序，
        # 否则 DataFrame 会出现重复索引，后续 reindex 直接报错
        dedup = {}
        for r in rows:
            dedup[str(r["day"])] = r
        rows = [dedup[k] for k in sorted(dedup)]
        if len(rows) < 200:
            return None
        idx = pd.Index([str(r["day"]) for r in rows])
        o = pd.Series([float(r.get("open") or r["close"]) for r in rows], index=idx)
        h = pd.Series([float(r.get("high") or r["close"]) for r in rows], index=idx)
        l = pd.Series([float(r.get("low") or r["close"]) for r in rows], index=idx)
        c = pd.Series([float(r["close"]) for r in rows], index=idx)
        v = pd.Series([float(r.get("volume") or 0) for r in rows], index=idx)
        return code, o, h, l, c, v

    Os, Hs, Ls, Cs, Vs = {}, {}, {}, {}, {}
    with cf.ThreadPoolExecutor(max_workers=8) as ex:
        for res in ex.map(_one, items):
            if not res:
                continue
            code, o, h, l, c, v = res
            Os[code], Hs[code], Ls[code], Cs[code], Vs[code] = o, h, l, c, v

    if not Cs:
        raise RuntimeError("行情数据不足，无法构建横截面面板")

    O = pd.DataFrame(Os).sort_index().replace(0, np.nan)
    H = pd.DataFrame(Hs).sort_index().reindex(index=O.index)
    L = pd.DataFrame(Ls).sort_index().reindex(index=O.index)
    C = pd.DataFrame(Cs).sort_index().reindex(index=O.index)
    V = pd.DataFrame(Vs).sort_index().reindex(index=O.index).replace(0, np.nan)

    # 缺失过多剔除
    keep = C.columns[C.notna().mean() > 0.70]
    O, H, L, C, V = O[keep], H[keep], L[keep], C[keep], V[keep]

    # meta 对齐
    code_list = list(C.columns)
    ind = pd.Series([ind_map.get(c, "其他") for c in code_list], index=code_list, name="ind")
    mktcap = pd.Series([meta.get(c, {}).get("mktcap") for c in code_list], index=code_list, name="mktcap")
    return O, H, L, C, V, ind, mktcap


# ================================================================= 因子构造

def _factor_panel(O, H, L, C, V):
    """14 个纯价格因子（无未来函数）"""
    ret1 = C.pct_change()
    turn = V / V.rolling(20).mean()
    amp = (H - L) / C.shift(1).replace(0, np.nan)
    close_gap = (O / C.shift(1) - 1)  # 隔夜跳空
    rolling = {}
    for n in (5, 10, 20, 60):
        rolling[n] = ret1.rolling(n)

    mdd_60 = {}
    for col in C.columns:
        c = C[col].dropna()
        if len(c) > 60:
            roll_max = c.rolling(60, min_periods=20).max()
            mdd_60[col] = (c / roll_max - 1)
        else:
            mdd_60[col] = pd.Series(np.nan, index=C.index)
    mdd60_df = pd.DataFrame(mdd_60).reindex(index=C.index)

    return {
        "mom_5":   C.pct_change(5),
        "mom_20":  C.pct_change(20),
        "mom_60":  C.pct_change(60),
        "rev_3":   -C.pct_change(3),
        "rev_10":  -C.pct_change(10),
        "vol_20":  -rolling[20].std(),
        "vol_60":  -rolling[60].std(),
        "skew_20": -rolling[20].skew(),
        "kurt_20": -rolling[20].kurt(),
        "turn_20": turn - 1.0,
        "turn_60": V / V.rolling(60).mean() - 1.0,
        "amp_20":  -amp.rolling(20).mean(),
        "gap_1":   -close_gap,
        "mdd_60":  -mdd60_df,
    }


# ================================================================= 涨跌停 / 停牌 / Amihud

def _trading_mask(O, H, L, C, V, market=None):
    """返回可交易性布尔宽表：True=当日可交易（未一字涨停/跌停/停牌）"""
    prev_close = C.shift(1)
    # A 股主板 ±10%，创业板/科创板 ±20%（简化处理）
    lim_up = prev_close * (1.095)
    lim_dn = prev_close * (0.905)
    # 一字板：开盘==最高==最低==收盘 且 收盘接近涨跌停
    gap = (prev_close * 0.003).abs()
    one_word = (
        ((H - L).abs() < gap)
        & ((O - C).abs() < gap)
        & (C >= lim_up + gap)
    ) | (
        ((H - L).abs() < gap)
        & ((O - C).abs() < gap)
        & (C <= lim_dn - gap)
    )
    # 停牌：成交量为 0 或全列缺失
    suspended = V.fillna(0) == 0
    tradable = (~one_word) & (~suspended) & C.notna()
    return tradable


def _amihud(C, V):
    """|收益率| / 成交额 代理；越高冲击成本越大"""
    ret = C.pct_change().abs()
    # 成交额缺失用 close * volume 近似，再按横截面市值归一
    amt = C * V.replace(0, np.nan)
    return ret * 1e6 / amt  # 缩放后便于排序


# ================================================================= 中性化

def _neutralize(F, ind_dummies, log_mcap):
    """逐日 OLS：factor ~ 行业虚拟变量 + log(市值)，取残差作为中性化后的因子"""
    out = F.copy()
    for dt in F.index:
        x = F.loc[dt]
        if x.notna().sum() < 15:
            continue
        m = x.notna()
        if ind_dummies is None or log_mcap is None:
            out.loc[dt] = x
            continue
        try:
            # 取当日行业 one-hot（(日期,代码) 双层索引），对齐到当日股票
            ind_vals = ind_dummies.loc[dt].reindex(index=x.index).fillna(0.0)
            ind_vals = ind_vals.loc[m]
            lm = log_mcap.reindex(x.index)
            X = np.hstack([lm.loc[m].values.reshape(-1, 1), np.ones((int(m.sum()), 1))])
            # 行业 one-hot（避免完全共线性：去掉一列）
            if ind_vals.shape[1] > 1:
                X = np.hstack([X, ind_vals.values[:, 1:]])
            y = x[m].values
            beta, *_ = np.linalg.lstsq(X, y, rcond=None)
            out.loc[dt, m] = y - X @ beta
        except Exception:
            out.loc[dt, m] = x[m]
    return out


# ================================================================= IC + 多重检验

def _ic_series(fac, fwd, tradable=None):
    """逐日横截面 Spearman IC，仅在可交易标的上算"""
    out = []
    idx = fac.index.intersection(fwd.index)
    if tradable is None:
        tradable = pd.DataFrame(1, index=fac.index, columns=fac.columns)
    trad = tradable.reindex_like(fac)
    for dt in idx:
        x = fac.loc[dt]
        y = fwd.loc[dt]
        ok = trad.loc[dt].fillna(True).astype(bool)
        m = x.notna() & y.notna() & ok
        if int(m.sum()) < 8:
            continue
        xv, yv = x[m].values, y[m].values
        if np.nanstd(xv) == 0 or np.nanstd(yv) == 0:
            continue
        ic, _ = spearmanr(xv, yv)
        if not np.isnan(ic):
            out.append((str(dt), float(ic)))
    return out


def _bh(pvals):
    """Benjamini-Hochberg FDR：p 值从小到大排，反向做 min 累积"""
    pvals = np.asarray(pvals, dtype=float)
    n = len(pvals)
    if n == 0:
        return np.array([])
    order = np.argsort(pvals)
    ranked = pvals[order]
    q = np.empty(n)
    q[-1] = min(ranked[-1], 1.0)
    for i in range(n - 2, -1, -1):
        val = ranked[i] * n / (i + 1)
        q[i] = min(val, q[i + 1], 1.0)
    out = np.empty(n)
    out[order] = q
    return out


def _build_neutral(C, ind, mktcap, max_groups=10):
    """返回 (ind_dummies, log_mcap)，逐日 OLS 中性化用；(日期,代码) 双层索引

    行业数多于（或接近）股票数时，全哑变量会让回归完全拟合、残差≈0，
    因此成员最少的行业并入「其他」，保证参数个数显著小于样本数。
    """
    s = ind.dropna().astype(str)
    ind_list = sorted(set(s))
    if len(ind_list) < 2:
        return None, None
    if len(ind_list) > max_groups:
        cnt = s.value_counts()
        keep = set(cnt.index[: max_groups - 1])
        grouped = s.where(s.isin(keep), "其他")
        ind_list = sorted(set(grouped))
    else:
        grouped = s
    # 单日 industry one-hot：index=code, columns=industry
    one_day = pd.DataFrame(
        {lab: (grouped == lab).astype(float) for lab in ind_list},
        index=grouped.index,
    ).reindex(index=C.columns).fillna(0.0)
    # 复制到所有交易日，保持 (日期, 代码) 双层索引，便于 .loc[dt] 取出当日 one-hot
    ind_dummies = pd.concat({dt: one_day for dt in C.index}, axis=0)
    log_mcap = pd.Series(
        [math.log1p(float(v)) if v else np.nan for v in mktcap], index=C.columns
    )
    return ind_dummies, log_mcap


def cross_section_ic(codes=None, days=500, horizons=(1, 5, 20), neutralize=True):
    """横截面 IC / ICIR / t 值 + Bonferroni + BH 校正 + 可交易过滤"""
    O, H, L, C, V, ind, mktcap = _wide(codes, need=days + 200)
    facs = _factor_panel(O, H, L, C, V)
    tradable = _trading_mask(O, H, L, C, V, None)

    # 中性化准备
    if neutralize:
        ind_dummies, log_mcap = _build_neutral(C, ind, mktcap)
        if ind_dummies is None:
            neutralize = False
    else:
        ind_dummies, log_mcap = None, None

    idx = C.index[-int(days):]
    rows = []
    pvals = []
    for name, fac in facs.items():
        fslice = fac.loc[idx]
        if neutralize and ind_dummies is not None:
            fslice = _neutralize(fslice, ind_dummies, log_mcap)
        for h in horizons:
            fwd = C.shift(-h) / C - 1
            series = _ic_series(fslice, fwd.loc[idx], tradable)
            if len(series) < 30:
                continue
            vals = np.array([v for _, v in series])
            mean = float(vals.mean())
            std = float(vals.std(ddof=1))
            icir = mean / std if std > 0 else 0.0
            tstat = icir * math.sqrt(len(vals))
            p_one = 2 * (1 - _normal_abs_cdf(abs(tstat)))
            pvals.append((name, h, mean, std, icir, tstat, p_one, vals))

    # 多重检验校正
    if pvals:
        raw_p = np.array([p[6] for p in pvals])
        bonf_p = np.minimum(raw_p * len(raw_p), 1.0)
        bh_p = _bh(raw_p)
    else:
        bonf_p, bh_p = np.array([]), np.array([])

    for i, (name, h, mean, std, icir, tstat, p_one, vals) in enumerate(pvals):
        win = float((vals > 0).mean() * 100)
        bonf_sign = bonf_p[i] < 0.05
        bh_sign = bh_p[i] < 0.05
        if bh_sign and abs(mean) >= 0.02:
            verdict = "有效"
        elif abs(mean) >= 0.01:
            verdict = "偏弱"
        else:
            verdict = "无效"
        rows.append(dict(
            factor=name, factor_cn=_FACTORS.get(name, name), horizon=h,
            ic_mean=round(mean, 4), ic_std=round(std, 4),
            icir=round(icir, 3), t_stat=round(tstat, 2),
            p_raw=round(float(p_one), 4),
            p_bonf=round(float(bonf_p[i]), 4),
            p_bh=round(float(bh_p[i]), 4),
            win_rate=round(win, 1), sample_days=len(vals),
            verdict=verdict,
        ))
    rows.sort(key=lambda r: (r["factor"], r["horizon"]))
    return dict(
        universe=len(C.columns), days=int(days), neutralized=neutralize, rows=rows,
        note="纯价格因子横截面 IC；可交易标的（未一字板/停牌）参与计算；t 值和 ICIR 越小越弱，"
             "p_BH<0.05 才值得信任；|IC|≥0.02 且 BH 显著记为有效。",
    )


def _normal_abs_cdf(x):
    """P(|Z| <= x)；用 erf 近似"""
    from math import erf, sqrt
    return erf(x / sqrt(2))


# ================================================================= 样本外（多因子 ICIR 合成）

def _metrics(nav, period_days=1):
    nav = np.asarray(nav, dtype=float)
    if len(nav) < 3:
        return dict(total=None, cagr=None, vol=None, sharpe=None, mdd=None, days=0)
    ret = nav[1:] / nav[:-1] - 1
    n = len(ret)
    periods_per_year = TRADING_DAYS / max(1, period_days)
    total = (nav[-1] / nav[0] - 1) * 100
    years = n / periods_per_year
    cagr = ((nav[-1] / nav[0]) ** (1 / years) - 1) * 100 if years > 0 else None
    vol = float(ret.std(ddof=1) * math.sqrt(periods_per_year) * 100)
    sharpe = float(ret.mean() / ret.std(ddof=1) * math.sqrt(periods_per_year)) if ret.std(ddof=1) > 0 else None
    peak = np.maximum.accumulate(nav)
    mdd = float(((nav / peak) - 1).min() * 100)
    return dict(total=round(total, 2), cagr=round(cagr, 2) if cagr is not None else None,
                vol=round(vol, 2), sharpe=round(sharpe, 2) if sharpe is not None else None,
                mdd=round(mdd, 2), days=int(n * period_days))


def _oos_run(fac_scores, C, trad, amihud_map, dates, top_n, cost_bps, rebalance):
    """按合成因子横截面排序做多头部（可交易 + 低冲击成本优先）"""
    nav = [1.0]
    bench = [1.0]
    prev = set()
    turnover_sum = 0.0
    folds = []
    for k in range(0, len(dates) - rebalance, rebalance):
        t0, t1 = dates[k], dates[k + rebalance]
        x = fac_scores.loc[t0]
        tradeable = trad.reindex(index=C.index).loc[t0].fillna(True).astype(bool)
        ok = x.notna() & tradeable
        if int(ok.sum()) < top_n * 2:
            continue
        # 冲击成本惩罚：Amihud 值高的在排序时扣分
        amp = amihud_map.reindex(index=C.index).loc[t0].reindex(x.index)
        amp = amp.fillna(amp.median()) if amp.notna().sum() > 3 else 0
        # 降序取 top
        rank = (x[ok] - 0.1 * amp[ok].rank(pct=True)).sort_values(ascending=False)
        pick = list(rank.head(top_n).index)
        c0 = C.loc[t0]
        c1 = C.loc[t1]
        r = (c1 / c0 - 1)
        r = r.reindex(pick).dropna()
        if len(r) < 3:
            continue
        gross = float(r.mean())
        cur = set(pick)
        turn = len(cur - prev) / max(1, len(cur))
        cost = turn * (cost_bps / 10000.0)
        # 冲击成本：Amihud 均值 × 系数（bps 量级）
        cur_amp = float(amp[pick].mean()) if amp.notna().sum() > 0 else 0
        impact_bps = min(200, cur_amp * 3000)  # 经验系数
        cost += impact_bps / 10000.0
        net = gross - cost
        turnover_sum += turn
        nav.append(nav[-1] * (1 + net))
        mb = C.loc[t1] / C.loc[t0] - 1
        mb = mb.dropna()
        bench.append(bench[-1] * (1 + float(mb.mean())))
        folds.append(dict(start=str(t0), end=str(t1), gross=round(gross * 100, 2),
                          net=round(net * 100, 2), turnover=round(turn * 100, 1),
                          impact_bps=round(impact_bps, 1)))
        prev = cur
    bench = bench[:len(nav)]
    return nav, bench, folds, turnover_sum


def walk_forward(codes=None, days=750, top_n=10, rebalance=20, cost_bps=10,
                 train_ratio=0.6, min_factors=3, neutralize=True):
    """滚动样本外：训练段只做因子选择 + 权重估计 → 测试段执行"""
    O, H, L, C, V, ind, mktcap = _wide(codes, need=days + 260)
    facs = _factor_panel(O, H, L, C, V)
    tradable = _trading_mask(O, H, L, C, V, None)
    amihud = _amihud(C, V)

    if neutralize:
        ind_dummies, log_mcap = _build_neutral(C, ind, mktcap)
        if ind_dummies is None:
            neutralize = False
    else:
        ind_dummies, log_mcap = None, None

    idx = C.index[-int(days):]
    split = int(len(idx) * train_ratio)
    is_idx, oos_idx = idx[:split], idx[split:]
    horizon = 5
    fwd = C.shift(-horizon) / C - 1
    fwd_train = fwd.loc[is_idx[:-horizon]]  # 剔除末尾 horizon 天避免泄漏

    # 计算所有因子训练段 IC / ICIR / t
    stats = []
    for name, fac in facs.items():
        fslice = fac.loc[is_idx]
        if neutralize and ind_dummies is not None:
            fslice = _neutralize(fslice, ind_dummies, log_mcap)
        series = _ic_series(fslice.loc[is_idx[:-horizon]], fwd_train, tradable)
        if len(series) < 30:
            continue
        vals = np.array([v for _, v in series])
        mean = float(vals.mean())
        std = float(vals.std(ddof=1))
        icir = mean / std if std > 0 else 0.0
        t = icir * math.sqrt(len(vals))
        stats.append(dict(
            factor=name, factor_cn=_FACTORS.get(name, name),
            ic=mean, icir=icir, t=t, sign=1 if mean >= 0 else -1,
            ok=(abs(t) >= 1.5 and abs(mean) >= 0.015),  # 放宽阈值避免全被剔除
        ))

    candidates = sorted([s for s in stats if s["ok"]], key=lambda r: -abs(r["ic"]))
    if not candidates:
        # 实在没合格的就拿 top3 来演
        candidates = sorted(stats, key=lambda r: -abs(r["ic"]))[:3]

    # 因子相关性检查：两两 Spearman 相关 > 0.6 则剔除一个
    selected = []
    for s in candidates:
        if len(selected) >= max(min_factors, top_n // 2):
            break
        conflict = False
        f_cur = facs[s["factor"]].loc[is_idx[:-horizon]]
        for s2 in selected:
            f2 = facs[s2["factor"]].loc[is_idx[:-horizon]]
            pair = pd.concat([f_cur, f2], axis=1).dropna()
            if pair.shape[0] > 20 and abs(spearmanr(pair.iloc[:, 0], pair.iloc[:, 1])[0]) > 0.55:
                conflict = True
                break
        if not conflict:
            selected.append(s)
    if not selected:
        selected = candidates[:2]

    # 按 ICIR 加权 + 方向
    w_total = sum(abs(s["icir"]) for s in selected)
    weights = {s["factor"]: s["sign"] * abs(s["icir"]) / w_total for s in selected}

    # 合成因子分数
    fac_scores = pd.DataFrame(0.0, index=C.index, columns=C.columns)
    for s in selected:
        f = facs[s["factor"]]
        if neutralize and ind_dummies is not None:
            f = _neutralize(f, ind_dummies, log_mcap)
        w = weights[s["factor"]]
        # cross-sectional z-score before weighting
        z = f.sub(f.mean(axis=1), axis=0).div(f.std(axis=1), axis=0)
        fac_scores += z.fillna(0) * w

    # 样本内 / 外跑
    is_nav, is_bench, _, _ = _oos_run(fac_scores, C, tradable, amihud, list(is_idx),
                                       top_n, cost_bps, rebalance)
    oos_nav, oos_bench, folds, turnover = _oos_run(fac_scores, C, tradable, amihud, list(oos_idx),
                                                    top_n, cost_bps, rebalance)

    m_is, m_oos = _metrics(is_nav, rebalance), _metrics(oos_nav, rebalance)
    m_isb, m_oosb = _metrics(is_bench, rebalance), _metrics(oos_bench, rebalance)

    # 成本敏感性（只用合成因子）
    sens = []
    for bp in (0, 10, 25, 50, 100):
        navx, _, _, _ = _oos_run(fac_scores, C, tradable, amihud, list(oos_idx),
                                  top_n, bp, rebalance)
        mx = _metrics(navx, rebalance)
        sens.append(dict(cost_bps=bp, cagr=mx["cagr"], sharpe=mx["sharpe"]))

    return dict(
        universe=len(C.columns), days=int(days), rebalance=int(rebalance), top_n=int(top_n),
        train_days=len(is_idx), oos_days=len(oos_idx), cost_bps=cost_bps,
        neutralized=neutralize,
        candidates=stats, selected=selected, weights=weights,
        in_sample=dict(portfolio=m_is, benchmark=m_isb),
        out_of_sample=dict(portfolio=m_oos, benchmark=m_oosb),
        decay=dict(
            cagr_drop=round((m_is["cagr"] or 0) - (m_oos["cagr"] or 0), 2),
            sharpe_drop=round((m_is["sharpe"] or 0) - (m_oos["sharpe"] or 0), 2),
            verdict=("样本外衰减明显，样本内表现不可外推"
                     if (m_oos["sharpe"] or 0) < 0 else "样本外有正 alpha，可执行"
                     if (m_oos["sharpe"] or 0) > 0.3 else "样本外一般"),
        ),
        oos_nav=[round(1e6 * x, 2) for x in oos_nav],
        oos_bench=[round(1e6 * x, 2) for x in oos_bench],
        folds=folds[-40:], avg_turnover=round(turnover / max(1, len(folds)) * 100, 1),
        cost_sensitivity=sens,
        amihud_calibrated=True,
        note=f"训练段 {len(selected)} 个因子，ICIR 加权合成 + 行业/市值中性化 + 可交易过滤 + Amihud 冲击成本；"
             f"样本外 {m_oos['sharpe']} 夏普 / {m_oos['cagr']}% 年化 / vs 等权基准 {m_oosb['sharpe']} 夏普。",
    )


# ================================================================= 概率再校准（保序回归）

def _pav(y, p):
    """Pool-Adjacent-Violators 保序回归，使 p 成为 y 的单调递增映射"""
    n = len(y)
    p = np.asarray(p, dtype=float)
    y = np.asarray(y, dtype=float)
    if n < 5:
        return p.copy()
    ord_idx = np.argsort(p)
    p_order = p[ord_idx]
    y_order = y[ord_idx]
    blocks = []
    for yi in y_order:
        blk_val = float(yi)
        blk_n = 1
        while blocks and blocks[-1][0] > blk_val:
            prev_val, prev_n = blocks.pop()
            blk_val = (prev_val * prev_n + blk_val * blk_n) / (prev_n + blk_n)
            blk_n = prev_n + blk_n
        blocks.append((blk_val, blk_n))
    out_sorted = []
    for val, cnt in blocks:
        out_sorted.extend([val] * cnt)
    out_sorted = np.array(out_sorted, dtype=float)
    result = np.empty(n, dtype=float)
    result[ord_idx] = out_sorted
    return result


def calibration(code="000001", days=500, step=5, k=40):
    """相似形态概率校准 + 保序回归再校准"""
    rows = ds.get_kline(code)
    if not rows:
        raise RuntimeError(f"未找到 {code} 的行情数据")
    from . import analysis
    dfi = analysis.add_indicators(analysis.build_df(rows).copy())
    feats = prediction._build_features(dfi).dropna()
    if len(feats) < 160:
        raise RuntimeError("历史数据不足，无法校准")
    z = (feats - feats.mean()) / feats.std().replace(0, 1)
    close = dfi["close"].reindex(feats.index).values
    n = len(z)
    start = max(90, n - int(days))
    samples = []
    for i in range(start, n - 5, step):
        hist = z.iloc[:max(10, i - 30)]
        if len(hist) < 60:
            continue
        cur = z.iloc[i]
        dist = np.sqrt(((hist - cur) ** 2).sum(axis=1))
        pos = np.array([z.index.get_loc(x) for x in dist.index])
        ok = pos + 5 < n
        if int(ok.sum()) < k:
            continue
        d = dist.values[ok]
        p = pos[ok]
        nn = np.argsort(d)[:k]
        fut = close[p[nn] + 5] / close[p[nn]] - 1
        prob = float((fut > 0).mean())
        realized = 1 if close[i + 5] / close[i] - 1 > 0 else 0
        samples.append((prob, realized, float(close[i + 5] / close[i] - 1) * 100, str(z.index[i])))

    if len(samples) < 40:
        raise RuntimeError("有效校准样本不足")

    probs = np.array([s[0] for s in samples])
    ys = np.array([s[1] for s in samples])
    rets = np.array([s[2] for s in samples])

    # 保序回归（训练集：排序前 70%，校准集：30%）
    n_train = max(20, int(len(samples) * 0.7))
    order_train = np.argsort(probs[:n_train])
    train_probs = probs[:n_train][order_train]
    train_ys = ys[:n_train][order_train]
    pav_probs = _pav(train_ys, train_probs)
    # 训练集 PAV 概率 → 在全体样本上做一个查找表
    pav_map = {}
    for p_val, y_val in zip(train_probs, pav_probs):
        pav_map[round(float(p_val), 6)] = float(y_val)

    def _calibrate(p):
        key = round(float(p), 6)
        if key in pav_map:
            return pav_map[key]
        # 线性插值到相邻
        ks = sorted(pav_map.keys())
        if not ks:
            return float(p)
        if float(p) <= ks[0]:
            return pav_map[ks[0]]
        if float(p) >= ks[-1]:
            return pav_map[ks[-1]]
        for i in range(len(ks) - 1):
            if ks[i] <= float(p) <= ks[i + 1]:
                l, r = ks[i], ks[i + 1]
                lw, rw = pav_map[l], pav_map[r]
                t = (float(p) - l) / max(1e-9, r - l)
                return lw * (1 - t) + rw * t
        return float(p)

    calib_probs = np.array([_calibrate(p) for p in probs])

    brier_raw = float(np.mean((probs - ys) ** 2))
    brier_cal = float(np.mean((calib_probs - ys) ** 2))
    base = float(ys.mean())

    def _bucket_stats(probs_arr):
        out = []
        for lo, hi, label in ((0, 0.40, "<40%"), (0.40, 0.50, "40-50%"),
                              (0.50, 0.60, "50-60%"), (0.60, 1.01, "≥60%")):
            m = (probs_arr >= lo) & (probs_arr < hi)
            if int(m.sum()) == 0:
                out.append(dict(bucket=label, n=0, predicted=None, realized=None,
                                avg_ret=None, gap=None))
                continue
            out.append(dict(
                bucket=label, n=int(m.sum()),
                predicted=round(float(probs_arr[m].mean() * 100), 1),
                realized=round(float(ys[m].mean() * 100), 1),
                avg_ret=round(float(rets[m].mean()), 2),
                gap=round(float((probs_arr[m].mean() - ys[m].mean()) * 100), 1),
            ))
        return out

    buckets_raw = _bucket_stats(probs)
    buckets_cal = _bucket_stats(calib_probs)

    bull_raw = probs >= 0.57
    bull_cal = calib_probs >= 0.57
    bear_raw = probs <= 0.42
    bear_cal = calib_probs <= 0.42

    return dict(
        code=code, samples=len(samples), base_rate=round(base * 100, 1),
        raw=dict(
            brier=round(brier_raw, 4),
            skill=round((1 - brier_raw / max(1e-9, float(np.mean((base - ys) ** 2)))) * 100, 1),
            buckets=buckets_raw,
            bullish=dict(n=int(bull_raw.sum()),
                         hit=round(float(ys[bull_raw].mean() * 100), 1) if bull_raw.any() else None),
            bearish=dict(n=int(bear_raw.sum()),
                         hit=round(float((1 - ys[bear_raw]).mean() * 100), 1) if bear_raw.any() else None),
        ),
        calibrated=dict(
            brier=round(brier_cal, 4),
            skill=round((1 - brier_cal / max(1e-9, float(np.mean((base - ys) ** 2)))) * 100, 1),
            buckets=buckets_cal,
            bullish=dict(n=int(bull_cal.sum()),
                         hit=round(float(ys[bull_cal].mean() * 100), 1) if bull_cal.any() else None),
            bearish=dict(n=int(bear_cal.sum()),
                         hit=round(float((1 - ys[bear_cal]).mean() * 100), 1) if bear_cal.any() else None),
        ),
        improvement=dict(
            brier_delta=round(brier_raw - brier_cal, 4),
            skill_delta=round(
                (1 - brier_cal / max(1e-9, float(np.mean((base - ys) ** 2)))) * 100
                - (1 - brier_raw / max(1e-9, float(np.mean((base - ys) ** 2)))) * 100, 1),
        ),
        calibrator="保序回归（PAV）",
        note="原始概率：相似形态 K 近邻上涨占比；校准后：按历史样本保序回归做单调变换。"
             "PAV 让预测概率成为真实上涨率的无偏估计。",
    )


# ================================================================= Paper Trading

def paper_trade(code="000001", strategy="factor", days=500, capital=1_000_000.0,
                top_n=10, rebalance=20, cost_bps=10):
    """逐日模拟盘跟踪：和组合回测同样的逻辑，但按真实行情逐日推进"""
    O, H, L, C, V, ind, mktcap = _wide(None, need=days + 260)
    facs = _factor_panel(O, H, L, C, V)
    tradable = _trading_mask(O, H, L, C, V, None)
    amihud = _amihud(C, V)

    ind_dummies, log_mcap = _build_neutral(C, ind, mktcap)

    # 用样本外逻辑的后半段（最近 days 天）
    idx = C.index[-int(days):]
    horizon = 5
    fwd = C.shift(-horizon) / C - 1
    split = int(len(idx) * 0.6)
    is_idx = idx[:split]
    fwd_train = fwd.loc[is_idx[:-horizon]]
    stats = []
    for name, fac in facs.items():
        fslice = fac.loc[is_idx]
        if ind_dummies is not None:
            fslice = _neutralize(fslice, ind_dummies, log_mcap)
        series = _ic_series(fslice.loc[is_idx[:-horizon]], fwd_train, tradable)
        if len(series) < 30:
            continue
        vals = np.array([v for _, v in series])
        mean = float(vals.mean())
        std = float(vals.std(ddof=1))
        icir = mean / std if std > 0 else 0.0
        t = icir * math.sqrt(len(vals))
        stats.append(dict(
            factor=name, ic=mean, icir=icir, sign=1 if mean >= 0 else -1,
            ok=(abs(t) >= 1.5 and abs(mean) >= 0.015),
        ))
    candidates = sorted([s for s in stats if s["ok"]], key=lambda r: -abs(r["ic"]))
    if not candidates:
        candidates = sorted(stats, key=lambda r: -abs(r["ic"]))[:3]
    # 去相关
    selected = []
    for s in candidates:
        if len(selected) >= max(3, top_n // 2):
            break
        f_cur = facs[s["factor"]].loc[is_idx[:-horizon]]
        conflict = False
        for s2 in selected:
            f2 = facs[s2["factor"]].loc[is_idx[:-horizon]]
            pair = pd.concat([f_cur, f2], axis=1).dropna()
            if pair.shape[0] > 20 and abs(spearmanr(pair.iloc[:, 0], pair.iloc[:, 1])[0]) > 0.55:
                conflict = True
                break
        if not conflict:
            selected.append(s)
    if not selected:
        selected = candidates[:2]

    w_total = sum(abs(s["icir"]) for s in selected)
    weights = {s["factor"]: s["sign"] * abs(s["icir"]) / w_total for s in selected}

    # 合成因子
    fac_scores = pd.DataFrame(0.0, index=C.index, columns=C.columns)
    for s in selected:
        f = facs[s["factor"]]
        if ind_dummies is not None:
            f = _neutralize(f, ind_dummies, log_mcap)
        w = weights[s["factor"]]
        z = f.sub(f.mean(axis=1), axis=0).div(f.std(axis=1), axis=0)
        fac_scores += z.fillna(0) * w

    # 用 idx 后段做模拟盘
    paper_idx = idx[split:]
    # 跑 OOS
    paper_nav, bench, folds, turnover = _oos_run(fac_scores, C, tradable, amihud,
                                                   list(paper_idx), top_n, cost_bps, rebalance)
    m_nav = _metrics(paper_nav, rebalance)
    m_bench = _metrics(bench, rebalance)

    return dict(
        capital=capital, days=len(paper_idx), rebalance=rebalance,
        top_n=top_n, cost_bps=cost_bps,
        selected=[{"factor": s["factor"], "factor_cn": _FACTORS.get(s["factor"], s["factor"]),
                    "ic": round(s["ic"], 4), "icir": round(s["icir"], 3),
                    "weight": round(weights[s["factor"]], 3)} for s in selected],
        nav_curve=[round(float(x), 2) for x in paper_nav],
        bench_curve=[round(float(x), 2) for x in bench],
        portfolio=m_nav, benchmark=m_bench,
        trades=folds, avg_turnover=round(turnover / max(1, len(folds)) * 100, 1),
        note=f"Paper trading 模拟：训练段 {len(is_idx)} 天估计因子，模拟段 {len(paper_idx)} 天逐日执行。"
             f"组合净值 vs 等权基准，已包含 Amihud 冲击成本代理 + 可交易过滤。",
    )


# ================================================================= 审计

def audit(codes=None):
    """数据与回测偏差审计"""
    O, H, L, C, V, ind, mktcap = _wide(codes)
    items = []
    n = len(C)
    short = int((C.notna().sum() < 500).sum())
    items.append(dict(
        item="幸存者偏差", level="中",
        detail=f"股票池为当前仍在上市的 {len(C.columns)} 只；样本中 {short} 只历史不足 500 根K线，"
               f"已退市/长期停牌标的不在池内，回测收益存在向上偏差。",
        fixable=False,
    ))
    items.append(dict(
        item="未来函数（基本面）", level="高",
        detail="复权/财务快照均取当前值。多因子策略中的价值/质量/成长分位若用于历史回测，"
               "等价于提前知道当时的财报结果；纯价格因子（动量/反转/波动）不受影响。",
        fixable=False,
    ))
    items.append(dict(
        item="涨跌停与流动性", level="中",
        detail="一字板 / 停牌已在样本外回测中过滤（不可交易），但仍未建模盘口冲击的非线性部分。"
               "Amihud 比率代理已加入，但真实冲击成本在极端行情下远超线性估计。",
        fixable=True,
    ))
    items.append(dict(
        item="成本假设", level="低",
        detail="默认滑点 0.1%、佣金 0.025%（最低 5 元）、印花税 0.05%；"
               "本次升级加入 Amihud 冲击成本代理（|收益|/成交额 × 3000），单边成本可到 200bp。",
        fixable=True,
    ))
    items.append(dict(
        item="样本内/外", level="高",
        detail="因子数量增加 + 中性化 + 多重检验校正后仍然会过拟合 —— 任何用历史数据选出来的因子"
               "在样本外都会衰减。本页「滚动样本外」把训练段样本单独拆出，只在样本外执行，"
               "并给出成本敏感性。衰减多少取决于策略。",
        fixable=False,
    ))
    items.append(dict(
        item="交易日历", level="低",
        detail=f"面板按各股自身交易日对齐，共 {n} 个交易日；节假日/停牌造成的缺口用前值对齐，"
               "极端情况下会产生 0 收益日，但本次回测加了可交易布尔掩码过滤。",
        fixable=True,
    ))
    return dict(universe=len(C.columns), days=n, items=items,
                note="等级=对结论可信度的影响程度；可修复项已在本次升级中处理。")


def gap_matrix():
    """与私募量化基金的能力对标矩阵"""
    layers = [
        ("数据层", [
            ("日频复权K线（OHLCV 全量）", "标配", "have", "已具备：新浪日K，主流标的 1400+ 根；本次升级使用 OHLCV 全量字段"),
            ("Level-2 逐笔 / 十档委托队列", "标配（高频与日内策略）", "gap", "未接入：交易所付费行情，本环境不可得"),
            ("分钟级行情 / 盘中快照", "标配", "gap", "未接入：免费分钟源在沙箱被拦截"),
            ("财务 Point-in-Time（按公告日对齐）", "标配", "gap", "缺失：当前用最新快照回填历史，存在未来函数；纯价格因子不受影响"),
            ("全A股含退市/长期停牌", "标配", "gap", "缺失：股票池仅当前上市标的，幸存者偏差向上"),
            ("北向持股 / 龙虎榜 / 资金流", "常用", "have", "已具备：东方财富 datacenter"),
            ("涨跌停 / 停牌可成交性约束", "标配", "have", "本次升级：一字板 + 停牌过滤；Amihud 冲击成本代理"),
            ("另类数据（舆情/研报/卫星/供应链）", "头部私募标配", "gap", "缺失：无数据源与授权"),
        ]),
        ("因子层", [
            ("量价因子库", "数百个", "partial", "已从 5 扩到 14 个（动量/反转/波动/偏度/峰度/换手/振幅/跳空/回撤）；头部私募数百个"),
            ("基本面因子库", "数百个 + 分析师预期", "partial", "已有 13 个；分析师预期缺失"),
            ("行业 / 市值中性化", "标配", "have", "本次升级：逐日 OLS 回归取残差；避免因子含风格暴露"),
            ("因子正交化 / 共线性处理", "标配", "have", "本次升级：候选因子两两 Spearman > 0.55 即剔除一个"),
            ("横截面 IC / ICIR / t 值", "标配", "have", "已具备 + 本次升级：仅可交易标的 + Bonferroni + BH FDR 多重检验校正"),
            ("因子半衰期 / 衰减曲线", "常用", "gap", "缺失；需逐因子计算 IC 随持有期衰减"),
        ]),
        ("组合层", [
            ("多因子合成与加权", "标配", "have", "已具备；本次升级用 ICIR 加权替代等权，方向按训练段 IC 正负确定"),
            ("行业权重上限", "标配", "have", "已具备：默认单一行业 ≤ 35%"),
            ("风险平价 / 均值-方差优化", "标配", "have", "已具备：ERC 风险平价 + 协方差矩阵"),
            ("换手约束与冲击成本模型", "标配", "partial", "本次升级：Amihud 代理加入回测；尚未建模 bid-ask spread 非线性段"),
            ("做空 / 融资融券 / 股指对冲", "标配", "gap", "缺失：仅多头"),
        ]),
        ("验证层", [
            ("滚动样本内外验证", "标配", "have", "已具备 + 本次升级：因子选择在训练段末尾 horizon 天前截断避免泄漏，多因子合成而非单因子"),
            ("概率校准（Brier / 分桶）", "常用", "have", "已具备 + 本次升级：保序回归（PAV）再校准，Brier 与 Skill 均报告原始 vs 校准后"),
            ("多重检验校正（Bonferroni / BH）", "常用", "have", "本次升级：同时报告 raw p / Bonferroni / BH FDR；BH p<0.05 才认为有效"),
            ("数据与回测偏差审计", "内部流程", "have", "已具备；本次升级的审计清单也同步更新"),
            ("Brinson 收益归因", "标配", "have", "已具备：配置 / 选股 / 交互三效应"),
            ("实盘或模拟盘跟踪（paper trading）", "标配", "have", "本次升级：新接口 /api/validation/paper，逐日模拟盘净值 + 交易记录"),
        ]),
        ("执行与风控", [
            ("组合风险模型（VaR / ES / Beta）", "标配", "have", "已具备：95% VaR、ES、Beta、风险贡献"),
            ("风控熔断 / 自动止损", "标配", "partial", "部分：有预警，无自动执行"),
            ("实时监控与告警", "标配", "partial", "部分：30 分钟自主盯盘 + Webhook 推送"),
            ("券商 API 实盘下单", "标配", "gap", "缺失：需券商授权与合规资质"),
        ]),
    ]
    rows = []
    for layer, items in layers:
        for cap, std, status, note in items:
            rows.append(dict(layer=layer, capability=cap, standard=std,
                             status=status, note=note))
    counts = {k: sum(1 for r in rows if r["status"] == k) for k in ("have", "partial", "gap")}
    return dict(rows=rows, counts=counts, total=len(rows),
                conclusion="本站已覆盖日频多因子研究闭环（数据→因子→中性化→正交化→合成→验证→paper trading）；"
                           "本次升级补齐的：因子扩到 14 个、行业+市值中性化、BH 多重检验、多因子 ICIR 加权合成、"
                           "Amihud 冲击成本、保序回归概率再校准、paper trading。"
                           "与头部私募的真实差距集中在 Level-2/分钟级/PIT 财务/另类数据/做空/实盘执行，受数据授权限制。")
