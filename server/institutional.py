# -*- coding: utf-8 -*-
"""机构级能力引擎（对标顶级私募量化基金）

本模块把头部私募（Two Sigma / Citadel / AQR / 幻方 / 九坤 / 明汯 / 鸣石）公开披露的
核心投研组件，在「日频免费行情」这一数据约束下尽可能落地为可计算、可验证的实现：

  1. barra_model       —— Barra 风格风险模型：10 个风格因子 + 行业因子 + 因子协方差
                          + 特质风险 + 组合风险分解 / 暴露约束
  2. optimize_portfolio —— 带风险约束的组合优化（投影梯度：单票/行业/风格/换手约束）
  3. execution_tca     —— Almgren-Chriss 冲击成本 + TWAP/VWAP/POV 执行对比 + 容量曲线
  4. deflated_sharpe   —— Deflated Sharpe Ratio（Bailey & López de Prado）+ PBO 过拟合概率
  5. factor_decay      —— 因子 IC 衰减曲线与半衰期
  6. ml_alpha          —— 直方图 GBDT 非线性因子合成 vs 线性 ICIR 加权（样本外对比）
  7. strategy_alloc    —— 多策略资金配置（等权 / 风险平价 / 最小方差 / 最大夏普）
  8. inst_matrix       —— 顶级私募「功能 × 数据支撑 × 个人劣势」全景矩阵

诚实声明（必须与结果一起展示）：
  - Beta / 动量 / 规模 / 流动性 / 特质波动 等风格因子由日频量价构造，无未来函数；
  - 价值(BP) / 成长 / 杠杆 / 盈利收益率 依赖当前财务快照，历史回填属近似，仅用于
    「风险归因与暴露约束」，不用于生成 alpha；
  - 冲击成本为 Almgren-Chriss 经验参数代理，非本市场 TCA 实测，仅用于量级比较；
  - 股票池仅当前上市标的，缺少已退市/长期停牌 → 存在幸存者偏差向上。
"""
import math
import time

import numpy as np
import pandas as pd
from scipy.stats import norm, spearmanr

from . import datasource as ds, market_data, portfolio, validation

TRADING_DAYS = 244
EULER = 0.5772156649015329

# ------------------------------------------------------------------ 面板缓存
_CACHE = {}
_CACHE_TTL = 900.0


def _panel(codes=None, need=1400):
    """构建横截面面板（含行情/因子/可交易/冲击成本），带 15 分钟缓存"""
    key = (tuple(sorted(codes)) if codes else None, int(need))
    now = time.time()
    hit = _CACHE.get(key)
    if hit and now - hit[0] < _CACHE_TTL:
        return hit[1]
    O, H, L, C, V, ind, mktcap = validation._wide(codes, need=need)
    dat = dict(
        O=O, H=H, L=L, C=C, V=V, ind=ind, mktcap=mktcap,
        fac=validation._factor_panel(O, H, L, C, V),
        trad=validation._trading_mask(O, H, L, C, V, None),
        amihud=validation._amihud(C, V),
    )
    _CACHE[key] = (now, dat)
    if len(_CACHE) > 6:
        oldest = min(_CACHE, key=lambda k: _CACHE[k][0])
        _CACHE.pop(oldest, None)
    return dat


def _fund_map(codes):
    """取 PE / PB / ROE / 负债率 / 利润增速（当前快照）"""
    out = {}
    if not codes:
        return out
    try:
        q = ",".join("?" * len(codes))
        for row in ds._db().execute(
                f"SELECT code,pe,pb FROM stocks WHERE code IN ({q})", list(codes)).fetchall():
            out.setdefault(row[0], {})["pe"] = row[1]
            out[row[0]]["pb"] = row[2]
    except Exception:  # noqa: BLE001
        pass
    try:
        want = set(codes)
        for s in market_data.get_all_snapshots():
            if s["code"] in want:
                out.setdefault(s["code"], {}).update(dict(
                    roe=s.get("roe"), debt=s.get("debt_ratio"),
                    pg=s.get("profit_growth"), eps=s.get("eps"), bps=s.get("bps")))
    except Exception:  # noqa: BLE001
        pass
    return out


# ------------------------------------------------------------------ 横截面标准化

def _zscore_cs(df):
    """逐行（逐日）去极值 + 标准化；常数列返回 0"""
    med = df.median(axis=1)
    mad = df.sub(med, axis=0).abs().median(axis=1)
    scale = (1.4826 * mad).where(lambda s: s > 1e-9)
    z = df.sub(med, axis=0).div(scale, axis=0).clip(-3, 3)
    mu = z.mean(axis=1)
    sd = z.std(axis=1).where(lambda s: s > 1e-9)
    return z.sub(mu, axis=0).div(sd, axis=0).fillna(0.0)


def _ic_fast(fs, fwd, trad, min_n=8):
    """向量化逐日横截面 Spearman IC（与 validation._ic_series 等价，但一次算完）

    Spearman = 横截面秩上的 Pearson 相关。原实现逐日调用 scipy.spearmanr，
    14 因子 × 8 持有期 × 500 天的调用量是接口耗时主因；此处改为整表 rank +
    行内去均值求相关，结果一致、耗时降一到两个数量级。
    """
    ix = fs.index.intersection(fwd.index)
    if len(ix) == 0:
        return pd.Series(dtype=float)
    F = fs.loc[ix]
    R = fwd.loc[ix]
    T = trad.reindex_like(F)
    T = T.fillna(True).astype(bool) if T is not None else pd.DataFrame(True, index=F.index, columns=F.columns)
    m = F.notna() & R.notna() & T
    n = m.sum(axis=1)
    Fr = F.where(m).rank(axis=1)
    Rr = R.where(m).rank(axis=1)
    Fd = Fr.sub(Fr.mean(axis=1), axis=0)
    Rd = Rr.sub(Rr.mean(axis=1), axis=0)
    cov = (Fd * Rd).sum(axis=1)
    sf = np.sqrt((Fd ** 2).sum(axis=1))
    sr = np.sqrt((Rd ** 2).sum(axis=1))
    ic = cov / (sf * sr)
    ic = ic.where((n >= min_n) & (sf > 1e-12) & (sr > 1e-12))
    return ic.dropna()


def _neutralize_many(facs, ind_d, mcap, dates):
    """批量行业+市值中性化：每个交易日只构造一次投影矩阵，再作用到全部因子

    与 validation._neutralize 数学上等价（OLS 残差），但把「每日一次 lstsq」
    改为「每日一次 pinv + 每个因子一次矩阵乘」，用于多因子场景提速数十倍。
    """
    keys = list(facs)
    cols = facs[keys[0]].columns
    out = {k: pd.DataFrame(np.nan, index=list(dates), columns=cols) for k in keys}
    eye = np.eye(len(cols))
    for dt in dates:
        ind_vals = ind_d.loc[dt].reindex(index=cols).fillna(0.0)
        lm = mcap.reindex(cols).fillna(0.0).to_numpy(dtype=float)
        X = np.hstack([lm.reshape(-1, 1), np.ones((len(cols), 1)),
                       ind_vals.values[:, 1:] if ind_vals.shape[1] > 1 else np.empty((len(cols), 0))])
        P = eye - X @ np.linalg.pinv(X)
        # 同一日内多个因子常共享相同的缺失掩码，按掩码缓存精确投影矩阵
        pmasks = {}
        for k in keys:
            y = facs[k].loc[dt].reindex(cols).to_numpy(dtype=float)
            m = ~np.isnan(y)
            n_ok = int(m.sum())
            if n_ok < 15:
                out[k].loc[dt] = y
                continue
            r = y.copy()
            if n_ok == len(cols):
                # 全样本无缺失：P 即为精确投影矩阵
                r[m] = P @ y[m]
            else:
                # 有缺失时，只在有效子样本上做 OLS 才与 validation._neutralize 严格等价。
                # 子样本投影按掩码缓存，避免同掩码因子重复求 pinv。
                hkey = m.tobytes()
                Ps = pmasks.get(hkey)
                if Ps is None:
                    Xm = X[m]
                    Ps = np.eye(n_ok) - Xm @ np.linalg.pinv(Xm)
                    pmasks[hkey] = Ps
                r[m] = Ps @ y[m]
            out[k].loc[dt] = r
    return out


# ================================================================== 1. Barra 风险模型

_STYLES = {
    "beta": "Beta(市场敏感度)",
    "momentum": "动量(120日剔除近21日)",
    "size": "规模(log 市值)",
    "value": "价值(BP=1/PB)",
    "growth": "成长(利润增速)",
    "leverage": "杠杆(资产负债率)",
    "liquidity": "流动性(20日换手)",
    "resvol": "特质波动率(60日)",
    "nlsize": "非线性规模(size³)",
    "eyield": "盈利收益率(EP=1/PE)",
}


def _style_exposures(dat, fund, days=250):
    """构造 10 个风格因子的日频暴露（未标准化）"""
    C, V = dat["C"], dat["V"]
    ret = C.pct_change()
    mkt = ret.mean(axis=1)  # 等权市场代理
    idx = C.index[-int(days):]

    ex = {}
    # Beta：250 日滚动
    cov = ret.rolling(250, min_periods=120).cov(mkt)
    var = mkt.rolling(250, min_periods=120).var()
    ex["beta"] = cov.div(var, axis=0)
    # 动量：120 日收益剔除最近 21 日
    ex["momentum"] = C.shift(21) / C.shift(141) - 1
    # 规模 / 非线性规模
    mc = dat["mktcap"]
    size = pd.Series({c: (math.log1p(float(mc[c])) if mc.get(c) else np.nan)
                      for c in C.columns})
    ex["size"] = pd.DataFrame([size] * len(C.index), index=C.index, columns=C.columns)
    sz_z = _zscore_cs(ex["size"])
    ex["nlsize"] = sz_z ** 3
    # 流动性：20 日均换手（用成交量相对均值做代理）
    turn = V / V.rolling(20, min_periods=10).mean()
    ex["liquidity"] = turn.rolling(20, min_periods=10).mean()
    # 特质波动率：对市场回归后的 60 日残差波动
    beta_ = ex["beta"].fillna(1.0)
    resid = ret.sub(beta_.mul(mkt, axis=0))
    ex["resvol"] = resid.rolling(60, min_periods=30).std()
    # 静态基本面因子
    bp = pd.Series({c: (1.0 / float(fund.get(c, {}).get("pb"))
                        if (fund.get(c, {}).get("pb") or 0) > 0 else np.nan)
                    for c in C.columns})
    ep = pd.Series({c: (1.0 / float(fund.get(c, {}).get("pe"))
                        if (fund.get(c, {}).get("pe") or 0) > 0 else np.nan)
                    for c in C.columns})
    growth = pd.Series({c: float(fund.get(c, {}).get("pg") or np.nan)
                        if fund.get(c, {}).get("pg") is not None else np.nan
                        for c in C.columns})
    lev = pd.Series({c: float(fund.get(c, {}).get("debt") or np.nan)
                     if fund.get(c, {}).get("debt") is not None else np.nan
                     for c in C.columns})
    for k, s in (("value", bp), ("eyield", ep), ("growth", growth), ("leverage", lev)):
        ex[k] = pd.DataFrame([s] * len(C.index), index=C.index, columns=C.columns)

    out = {}
    for k in _STYLES:
        out[k] = _zscore_cs(ex[k].reindex(index=C.index)).loc[idx]
    return out


def _ind_dummies(ind, dates, max_groups=10):
    """行业哑变量（静态，按日期展开为 (日期, 代码) 双层索引）

    股票池较小（数十只）而行业数可达数十个时，若直接用全行业哑变量，
    横截面回归的参数个数会超过样本数，导致完全拟合、残差≈0。
    此处把成员最少的行业并入「其他」，保证参数个数 < 股票数。
    """
    s = ind.dropna().astype(str)
    labels = sorted(set(s))
    if len(labels) > max_groups:
        cnt = s.value_counts()
        keep = set(cnt.index[: max_groups - 1])
        grouped = s.where(s.isin(keep), "其他")
        labels = sorted(set(grouped))
    else:
        grouped = s
    one = pd.DataFrame({lab: (grouped == lab).astype(float) for lab in labels},
                       index=grouped.index)
    one = one.reindex(index=ind.index).fillna(0.0)
    m = pd.concat({dt: one for dt in dates}, axis=0)
    return m, labels


def barra_model(codes=None, days=250):
    """Barra 风格风险模型：暴露 → 因子收益 → 协方差 → 组合风险分解"""
    dat = _panel(codes, need=max(days + 320, 700))
    C = dat["C"]
    idx = C.index[-int(days):]
    fund = _fund_map(list(C.columns))
    styles = _style_exposures(dat, fund, days=len(C.index))
    ind_d, ind_labels = _ind_dummies(dat["ind"], C.index)
    ret = C.pct_change()

    K = list(_STYLES)
    names = [k for k in K] + [f"IND:{l}" for l in ind_labels]
    # 逐日横截面回归：r_i = a + Σ f_k·X_ik + Σ d_j·IND_ij + e_i
    f_ret, s_ret = [], []
    dates_used = []
    for dt in idx[:-1]:
        y = ret.loc[dt]
        Xs = [styles[k].loc[dt] for k in K]
        Xi = [ind_d.loc[dt][l] for l in ind_labels]
        X = pd.concat(Xs + Xi, axis=1)
        X.columns = names
        ok = y.notna() & X.notna().all(axis=1)
        if int(ok.sum()) < 15:
            continue
        Xm = X[ok].to_numpy(dtype=float)
        ym = y[ok].to_numpy(dtype=float)
        A = np.column_stack([np.ones(len(ym)), Xm])
        try:
            coef, *_ = np.linalg.lstsq(A, ym, rcond=None)
        except np.linalg.LinAlgError:
            continue
        f_ret.append(coef[1:])
        s_ret.append(pd.Series(ym - A @ coef, index=X[ok].index))
        dates_used.append(dt)
    if len(f_ret) < 60:
        raise RuntimeError("有效交易日不足 60，无法估计因子协方差")

    FR = pd.DataFrame(f_ret, index=dates_used, columns=names)
    SR = pd.DataFrame(s_ret, index=dates_used)
    F = FR.cov().to_numpy() * TRADING_DAYS          # 年化因子协方差
    spec = SR.var().reindex(C.columns).fillna(SR.var().median()) * TRADING_DAYS

    # 最新一日暴露
    last = idx[-1]
    Xlast = pd.concat([styles[k].loc[last] for k in K] + [ind_d.loc[last][l] for l in ind_labels],
                      axis=1)
    Xlast.columns = names
    Xlast = Xlast.reindex(index=C.columns).fillna(0.0)

    # 两个基准组合：等权（风格暴露天然≈0）与市值加权（体现规模暴露）
    n = len(C.columns)
    Xa = Xlast.to_numpy(dtype=float)
    mc = pd.Series(dat["mktcap"]).reindex(C.columns).fillna(0.0).clip(lower=0).to_numpy(dtype=float)
    weights = {
        "equal": np.ones(n) / n,
        "cap": mc / mc.sum() if mc.sum() > 0 else np.ones(n) / n,
    }

    def _decomp(w):
        exp = Xa.T @ w
        f_var = float(exp @ F @ exp)
        s_var = float(np.sum((w ** 2) * spec.to_numpy()))
        tot = f_var + s_var
        return exp, f_var, s_var, tot

    decomps = {k: _decomp(v) for k, v in weights.items()}
    exp_eq, _, _, _ = decomps["equal"]
    exp_cap, f_cap, s_cap, tot_cap = decomps["cap"]

    # 各因子风险贡献（以市值加权组合为例，占组合总风险）
    contrib = []
    for i, nm in enumerate(names):
        c = float(exp_cap[i] * (F[i] @ exp_cap))
        if abs(c) > 1e-12:
            contrib.append(dict(factor=nm, name=_STYLES.get(nm, nm),
                                exposure=round(float(exp_cap[i]), 3),
                                risk_contrib=round(c / max(tot_cap, 1e-12) * 100, 2)))
    contrib.sort(key=lambda x: -abs(x["risk_contrib"]))

    # 暴露描述：组合暴露 + 横截面分布
    expo_rows = []
    for i, nm in enumerate(names):
        vals = Xa[:, i]
        expo_rows.append(dict(
            factor=nm, name=_STYLES.get(nm, nm),
            portfolio=round(float(exp_cap[i]), 3),
            portfolio_eq=round(float(exp_eq[i]), 3),
            cross_mean=round(float(np.nanmean(vals)), 3),
            cross_min=round(float(np.nanmin(vals)), 2),
            cross_max=round(float(np.nanmax(vals)), 2),
            std=round(float(np.nanstd(vals)), 3),
        ))

    def _risk(f_var, s_var, tot):
        return dict(
            total_vol=round(math.sqrt(max(tot, 0)) * 100, 2),
            factor_vol=round(math.sqrt(max(f_var, 0)) * 100, 2),
            specific_vol=round(math.sqrt(max(s_var, 0)) * 100, 2),
            factor_share=round(f_var / max(tot, 1e-12) * 100, 2),
            specific_share=round(s_var / max(tot, 1e-12) * 100, 2),
        )

    return dict(
        days=len(dates_used), universe=len(C.columns),
        styles=[dict(key=k, name=v) for k, v in _STYLES.items()],
        industries=ind_labels,
        exposures=expo_rows,
        risk=dict(cap=_risk(f_cap, s_cap, tot_cap),
                  equal=_risk(*decomps["equal"][1:])),
        contributions=contrib[:12],
        factor_cov_top=[dict(a=names[i], b=names[j],
                             corr=round(float(F[i, j] / math.sqrt(max(F[i, i] * F[j, j], 1e-12))), 3))
                        for i in range(min(len(names), 12))
                        for j in range(i + 1, min(len(names), 12))][:15],
        note=("风格暴露为横截面标准化值（|z|≤3）。Beta/动量/规模/流动性/特质波动为纯量价，无未来函数；"
              "价值/成长/杠杆/盈利收益率取当前财务快照，属近似，仅用于风险归因与暴露约束，不用于生成 alpha。"
              "等权组合的风格暴露天然≈0（逐日横截面已去均值），故风险以行业偏离与特质波动为主；"
              "市值加权组合可体现规模暴露。"),
    )


# ================================================================== 2. 组合优化

def _project(w, cap, ind_cap, ind_vec, style_cap, style_exp):
    """把权重投影到可行域：单票上限 / 行业上限 / 风格暴露上限 / 非负 / 求和=1"""
    w = np.clip(w, 0.0, cap)
    # 行业上限：超限行业按比例压缩，释放的权重留待再分配
    labels = np.unique(ind_vec)
    for _ in range(8):
        fixed = False
        for lab in labels:
            m = ind_vec == lab
            s = w[m].sum()
            if s > ind_cap + 1e-12:
                w[m] *= ind_cap / s
                fixed = True
        if not fixed:
            break
    # 风格暴露上限：沿违规方向做线性收缩
    for _ in range(6):
        e = style_exp.T @ w
        viol = np.abs(e) > style_cap
        if not viol.any():
            break
        for k in np.where(viol)[0]:
            v = style_exp[:, k]
            cur = float(v @ w)
            target = math.copysign(style_cap, cur)
            # 求解 w' = w - t·v 使 v·w' = target
            denom = float(v @ v)
            if denom > 1e-12:
                t = (cur - target) / denom
                w = w - t * v
                w = np.clip(w, 0.0, cap)
    s = w.sum()
    if s <= 1e-12:
        w = np.ones_like(w) / len(w)
    else:
        w = w / s
    return w


def optimize_portfolio(codes=None, days=400, top_n=12, risk_aversion=3.0,
                       max_weight=0.12, max_industry=0.35, style_cap=1.0,
                       turnover_penalty=0.5, cost_bps=15):
    """带 Barra 风险约束的组合优化（最大化 alpha − λ·风险 − 换手成本）"""
    dat = _panel(codes, need=max(days + 320, 700))
    C = dat["C"]
    idx = C.index[-int(days):]
    fund = _fund_map(list(C.columns))
    styles = _style_exposures(dat, fund, days=len(C.index))
    K = list(_STYLES)

    # alpha：中性化后的多因子 ICIR 加权（复用 validation 逻辑，训练段估计权重）
    fac = dat["fac"]
    horizon = 5
    fwd = C.shift(-horizon) / C - 1
    split = int(len(idx) * 0.6)
    train, test = idx[:split], idx[split:]
    ind_d, _ = _ind_dummies(dat["ind"], C.index)
    mcap = pd.Series([math.log1p(float(v)) if v else np.nan for v in dat["mktcap"]],
                     index=C.columns)

    stats = []
    neu = _neutralize_many(fac, ind_d, mcap, train)   # 批量中性化
    for name, f in fac.items():
        fs = neu[name]
        ic_s = _ic_fast(fs.loc[train[:-horizon]], fwd.loc[train[:-horizon]], dat["trad"])
        vals = ic_s.to_numpy(dtype=float)
        if len(vals) < 30:
            continue
        ic = float(vals.mean())
        icir = ic / float(vals.std(ddof=1)) if vals.std(ddof=1) > 0 else 0.0
        if abs(icir * math.sqrt(len(vals))) >= 1.5 and abs(ic) >= 0.015:
            stats.append(dict(factor=name, ic=ic, icir=icir, sign=1 if ic >= 0 else -1))
    stats.sort(key=lambda r: -abs(r["icir"]))
    pick = []
    for s in stats:
        if len(pick) >= 5:
            break
        f1 = fac[s["factor"]].loc[train]
        conflict = False
        for p in pick:
            pair = pd.concat([f1, fac[p["factor"]].loc[train]], axis=1).dropna()
            if pair.shape[0] > 20 and abs(spearmanr(pair.iloc[:, 0], pair.iloc[:, 1])[0]) > 0.55:
                conflict = True
                break
        if not conflict:
            pick.append(s)
    if not pick:
        pick = sorted(stats, key=lambda r: -abs(r["ic"]))[:3] or [dict(
            factor="mom_20", ic=0.02, icir=0.1, sign=1)]
    wtot = sum(abs(s["icir"]) for s in pick)
    comp = pd.DataFrame(0.0, index=C.index, columns=C.columns)
    for s in pick:
        f = validation._neutralize(fac[s["factor"]], ind_d, mcap)
        z = f.sub(f.mean(axis=1), axis=0).div(f.std(axis=1).where(lambda x: x > 1e-9), axis=0)
        comp += z.fillna(0.0) * (s["sign"] * abs(s["icir"]) / wtot)

    # 用最近一日 alpha；风险用 Barra 风格 + 特质
    last = idx[-1]
    alpha_raw = comp.loc[last].reindex(C.columns).fillna(0.0)
    alpha = (alpha_raw - alpha_raw.mean()) / (alpha_raw.std() or 1.0)

    # alpha 必须换算到「收益率」量纲，否则与风险项单位不一致会导致优化解极端化。
    # 依据 IC 关系：1 个横截面标准差的 alpha ≈ IC × 未来收益横截面标准差。
    cs_std5 = float(fwd.loc[train[:-horizon]].std(axis=1).mean())
    ic_avg = sum(abs(s["ic"]) * abs(s["icir"]) for s in pick) / max(wtot, 1e-9)
    mu_ann = ic_avg * cs_std5 * (TRADING_DAYS / horizon)   # 年化预期收益（小数）
    a = alpha.to_numpy(dtype=float) * mu_ann

    Xl = pd.concat([styles[k].loc[last] for k in K], axis=1)
    Xl.columns = K
    Xl = Xl.reindex(index=C.columns).fillna(0.0)
    Xa = Xl.to_numpy(dtype=float)

    # 特质风险（60 日年化波动，缺失用全样本兜底）
    ret = C.pct_change()
    spec = (ret.rolling(60, min_periods=30).std().loc[last].reindex(C.columns)
            * math.sqrt(TRADING_DAYS)).fillna(ret.std() * math.sqrt(TRADING_DAYS)).to_numpy()
    spec = np.nan_to_num(spec, nan=0.3)

    # 因子协方差：用「按暴露排序的多空组合收益」估计（因子模拟组合 FMP），年化
    fmp = {}
    for k in K:
        z = styles[k].loc[idx]
        r = ret.loc[idx]
        vals = []
        for dt in idx:
            zz, rr = z.loc[dt], r.loc[dt]
            msk = zz.notna() & rr.notna()
            if int(msk.sum()) < 10:
                vals.append(np.nan)
                continue
            lo_q, hi_q = zz[msk].quantile([0.2, 0.8])
            lo = rr[msk][zz[msk] <= lo_q].mean()
            hi = rr[msk][zz[msk] >= hi_q].mean()
            vals.append(float(hi - lo))
        fmp[k] = pd.Series(vals, index=idx)
    FR = pd.DataFrame(fmp).dropna(how="all")
    Fc = FR.fillna(0.0).cov().to_numpy() * TRADING_DAYS
    D = np.maximum(Fc, 0) * 0.25   # 保守缩放：单因子年化波动约 10–20%
    Sigma = Xa @ D @ Xa.T + np.diag(spec ** 2)

    w0 = np.ones(len(a)) / len(a)
    ind_vec = dat["ind"].reindex(C.columns).fillna("其他").to_numpy()
    w = w0.copy()
    lr = 0.15
    for _ in range(300):
        grad = a - risk_aversion * (Sigma @ w) - turnover_penalty * (w - w0)
        w = _project(w + lr * grad, max_weight, max_industry, ind_vec, style_cap, Xa)
        lr *= 0.995

    # 约束校验
    exp = Xa.T @ w
    ind_w = pd.Series(w, index=C.columns).groupby(ind_vec).sum()
    chk = dict(
        sum=round(float(w.sum()), 4),
        max_weight=round(float(w.max()), 4),
        max_industry=round(float(ind_w.max()), 4),
        max_style=round(float(np.abs(exp).max()), 4),
        min_weight=round(float(w.min()), 4),
    )
    er = float(a @ w)
    vol = float(math.sqrt(max(w @ Sigma @ w, 0)))
    base_vol = float(math.sqrt(max(w0 @ Sigma @ w0, 0)))
    pred_ann = er * 100                          # 年化预期超额收益（百分点）
    pred5 = pred_ann * horizon / TRADING_DAYS    # 折算 5 日
    # 换手与成本
    turn = float(np.abs(w - w0).sum() / 2)
    cost = turn * 2 * cost_bps / 1e4

    holdings = []
    for i, c in enumerate(C.columns):
        if w[i] > 0.005:
            holdings.append(dict(code=c, weight=round(float(w[i]) * 100, 2),
                                 alpha_z=round(float(alpha.iloc[i]), 3),
                                 spec_vol=round(float(spec[i]) * 100, 1)))
    holdings.sort(key=lambda x: -x["weight"])

    ind_rows = [dict(industry=k, weight=round(float(v) * 100, 2))
                for k, v in ind_w.sort_values(ascending=False).items()]
    style_rows = [dict(key=k, name=_STYLES[k], after=round(float(exp[i]), 3),
                       before=round(float((Xa.T @ w0)[i]), 3))
                  for i, k in enumerate(K)]

    return dict(
        universe=len(a), days=len(idx), top_n=int(top_n),
        selected_factors=[dict(factor=s["factor"], factor_cn=validation._FACTORS.get(s["factor"], s["factor"]),
                               ic=round(s["ic"], 4), icir=round(s["icir"], 3),
                               weight=round(s["sign"] * abs(s["icir"]) / wtot, 3)) for s in pick],
        holdings=holdings[:20],
        industries=ind_rows,
        styles=style_rows,
        constraints=chk,
        metrics=dict(alpha_score=round(float(mu_ann), 4),
                     pred_return_5d_pct=round(pred5, 3),
                     pred_return_ann_pct=round(pred_ann, 2),
                     expected_vol=round(vol * 100, 2),
                     info_ratio=round(pred_ann / (vol * 100), 3) if vol > 0 else 0.0,
                     turnover=round(turn * 100, 1), cost_pct=round(cost * 100, 3),
                     baseline_vol=round(base_vol * 100, 2),
                     vol_change_pct=round((vol / max(base_vol, 1e-9) - 1) * 100, 2)),
        note=("投影梯度求解 max wᵀα − λ·wᵀΣw − κ‖w−w₀‖，约束：非负、Σw=1、单票≤{}%、"
              "单行业≤{}%、风格暴露|z|≤{}。α 来自训练段 ICIR 加权多因子，Σ 来自 Barra 风格+特质风险。"
              "约束可能因互相冲突而近似满足（见 constraints）。").format(
            int(max_weight * 100), int(max_industry * 100), style_cap),
    )


# ================================================================== 3. 执行 / TCA

def _ac_impact_bps(participation, sigma_daily):
    """Almgren-Chriss 冲击成本代理（永久 γ=0.314 + 临时 η=0.142，指数 0.6）"""
    p = max(float(participation), 0.0)
    if p <= 0:
        return 0.0
    perm = 0.314 * sigma_daily * p
    temp = 0.142 * sigma_daily * (p ** 0.6)
    return (perm + temp) * 1e4


def _proxy_backtest(dat, idx, top_frac=0.2):
    """轻量代理回测：多因子打分取前 top_frac 等权持有，次日收益

    返回 (年化毛收益, 平均单边换手)。用于 TCA 容量曲线，避免硬编码收益假设；
    不使用未来数据，但股票池为当前上市标的，存在幸存者偏差向上。
    """
    C, trad = dat["C"], dat["trad"]
    ret = C.pct_change().shift(-1)
    comp = None
    for name in ("amp_20", "rev_10", "vol_20", "mdd_60"):
        f = dat["fac"].get(name)
        if f is None:
            continue
        f = f.loc[idx]
        z = f.sub(f.mean(axis=1), axis=0).div(
            f.std(axis=1).where(lambda x: x > 1e-9), axis=0).fillna(0.0)
        comp = z if comp is None else comp + z
    if comp is None:
        return 0.0, 0.0
    prev, daily, turns = set(), [], []
    for dt in idx[:-1]:
        s = comp.loc[dt]
        ok = s.notna() & trad.loc[dt].fillna(True) & ret.loc[dt].notna()
        if int(ok.sum()) < 10:
            continue
        k = max(1, int(ok.sum() * top_frac))
        sel = set(s[ok].nlargest(k).index)
        daily.append(float(ret.loc[dt][list(sel)].mean()))
        turns.append(len(sel ^ prev) / max(len(sel), 1) / 2 if prev else 1.0)
        prev = sel
    if len(daily) < 30:
        return 0.0, 0.0
    nav = float(np.prod([1 + x for x in daily]))
    cagr = nav ** (TRADING_DAYS / len(daily)) - 1
    return cagr, float(np.mean(turns))


def execution_tca(codes=None, days=400, aum_list=None, participation=0.10):
    """算法交易执行对比 + 冲击成本 + 容量曲线"""
    aum_list = aum_list or [0.5, 1, 2, 5, 10, 20, 50]  # 亿元
    dat = _panel(codes, need=max(days + 60, 500))
    C, V = dat["C"], dat["V"]
    idx = C.index[-int(days):]
    ret = C.pct_change()
    sigma = ret.rolling(60, min_periods=20).std().loc[idx[-1]].reindex(C.columns)
    sigma = sigma.fillna(ret.std().median())

    # 近 20 日日均成交额（元）
    amt = (C * V).loc[idx[-20:]].mean().reindex(C.columns).fillna(0.0)
    amt = amt.where(amt > 0, 1e6)

    # 组合：等权持有 10 只
    top = amt.sort_values(ascending=False).head(10).index.tolist()
    n = len(top)

    # 三种算法：在给定参与率下的预计成本（bps）
    algo_rows = []
    for name, mult, desc in (
            ("TWAP", 1.0, "时间均匀切分，成本≈永久+临时冲击全量"),
            ("VWAP", 0.85, "按成交量分布切分，避开开盘/收盘冲击，成本×0.85"),
            ("POV 10%", 0.70, "跟随 10% 成交量，拉长执行窗口，成本×0.70")):
        costs = []
        for c in top:
            trade = 1.0 / n  # 占组合比重
            order_amount = trade * 1e8  # 以 1 亿 AUM 为基准
            adv = float(amt[c])
            part = order_amount / adv if adv > 0 else 0
            costs.append(_ac_impact_bps(part * mult, float(sigma[c])) * mult)
        algo_rows.append(dict(algo=name, desc=desc,
                              avg_impact_bps=round(float(np.mean(costs)), 2),
                              max_impact_bps=round(float(np.max(costs)), 2)))

    # 容量曲线：不同 AUM 下的年化成本拖累
    gross_cagr, avg_turn = _proxy_backtest(dat, idx)
    turn_daily = avg_turn * 2 / 5   # 单边换手 × 双边 / 平均持有 5 日的近似日均换手
    cap_rows = []
    for aum in aum_list:
        costs = []
        for c in top:
            pos = (aum * 1e8) / n
            part = pos / float(amt[c])
            costs.append(_ac_impact_bps(part, float(sigma[c])))
        avg_bps = float(np.mean(costs))
        annual_cost = avg_bps / 1e4 * turn_daily * TRADING_DAYS * 100  # 百分点
        cap_rows.append(dict(aum=float(aum), avg_impact_bps=round(avg_bps, 2),
                             annual_cost_pct=round(annual_cost, 2),
                             net_cagr=round(gross_cagr * 100 - annual_cost, 2),
                             pct_of_adv=round(aum * 1e8 / n / float(np.mean([amt[c] for c in top])) * 100, 2)))
    cap_rows.sort(key=lambda r: r["aum"])

    # 容量上限：净年化跌破 0 的 AUM
    soft = [r for r in cap_rows if r["net_cagr"] <= 0]
    capacity = soft[0]["aum"] if soft else None

    return dict(
        universe=len(C.columns), days=len(idx), portfolio_size=n,
        holdings=[dict(code=c, adv_yi=round(float(amt[c]) / 1e8, 2),
                       vol_pct=round(float(sigma[c]) * math.sqrt(TRADING_DAYS) * 100, 1))
                  for c in top],
        algos=algo_rows,
        capacity=cap_rows,
        capacity_limit_yi=capacity,
        gross_cagr_pct=round(gross_cagr * 100, 2),
        avg_turnover_pct=round(avg_turn * 100, 1),
        note=("冲击成本用 Almgren-Chriss 代理：永久 0.314·σ·(X/V) + 临时 0.142·σ·(X/V)^0.6，"
              "σ 为日波动率、X/V 为占 ADV 比例。经验参数来自美股实证，A 股未做 TCA 实测，"
              "仅用于不同算法/不同规模之间的量级比较与容量判断。毛收益由本站「多因子打分前20%等权、"
              "次日收益」代理回测实时计算（含幸存者偏差向上），非可直接对标的实盘收益。"),
    )


# ================================================================== 4. DSR + PBO

def deflated_sharpe(codes=None, days=600):
    """Deflated Sharpe Ratio（校正试验次数）+ PBO（组合对称交叉验证）"""
    dat = _panel(codes, need=max(days + 320, 900))
    C, V, trad = dat["C"], dat["V"], dat["trad"]
    fac = dat["fac"]
    idx = C.index[-int(days):]
    horizon = 5
    fwd = C.shift(-horizon) / C - 1

    # 每个因子构造多空组合的日收益序列（14 个试验）
    strategies = {}
    ic_rows = []
    for name, f in fac.items():
        z = f.sub(f.mean(axis=1), axis=0).div(f.std(axis=1).where(lambda s: s > 1e-9), axis=0)
        z = z.fillna(0.0)
        # 每日多空：top 30% - bottom 30%
        rets = []
        for dt in idx[:-horizon]:
            zi = z.loc[dt]
            ok = zi.notna() & trad.loc[dt].fillna(True)
            if int(ok.sum()) < 12:
                rets.append(0.0)
                continue
            zz = zi[ok]
            k = max(1, int(len(zz) * 0.3))
            hi = zz.nlargest(k).index
            lo = zz.nsmallest(k).index
            r1 = fwd.loc[dt, hi].mean()
            r0 = fwd.loc[dt, lo].mean()
            if pd.isna(r1) or pd.isna(r0):
                rets.append(0.0)
            else:
                rets.append(float(r1 - r0))
        strategies[name] = pd.Series(rets, index=idx[:-horizon], dtype=float)

    R = pd.DataFrame(strategies).dropna(how="all").fillna(0.0)
    if len(R) < 100 or R.shape[1] < 3:
        raise RuntimeError("多空收益序列不足，无法做 DSR/PBO")

    # 各试验的年化夏普
    mu = R.mean()
    sd = R.std().replace(0, np.nan)
    srs = (mu / sd * math.sqrt(TRADING_DAYS)).dropna()
    best_name = srs.idxmax()
    best_sr = float(srs.max())
    N = int(len(srs))

    # DSR：SR0 由试验间夏普离散度决定
    var_sr = float(srs.var(ddof=1))
    sr0 = math.sqrt(var_sr) * ((1 - EULER) * norm.ppf(1 - 1.0 / N)
                               + EULER * norm.ppf(1 - 1.0 / (N * math.e)))
    best_ret = R[best_name]
    T = int(len(best_ret))
    sk = float(best_ret.skew())
    ku = float(best_ret.kurtosis()) + 3.0
    denom = 1 - sk * best_sr / math.sqrt(TRADING_DAYS) + (ku - 1) / 4 * (best_sr / math.sqrt(TRADING_DAYS)) ** 2
    denom = max(denom, 1e-9)
    z = (best_sr - sr0) * math.sqrt(T - 1) / math.sqrt(denom)
    dsr = float(norm.cdf(z))

    # PBO：8 块 × C(8,4)=70 种切分
    pbo, logits = _pbo(R, blocks=8)
    # 各因子夏普与 p 值
    rows = []
    for name in srs.index:
        s = float(srs[name])
        t = s * math.sqrt(T) / math.sqrt(max(1 - float(best_ret.skew()) * s / math.sqrt(TRADING_DAYS), 1e-9))
        rows.append(dict(factor=name, factor_cn=validation._FACTORS.get(name, name),
                         sharpe=round(s, 2), ann_ret=round(float(mu[name]) * TRADING_DAYS * 100, 2),
                         ann_vol=round(float(sd[name]) * math.sqrt(TRADING_DAYS) * 100, 2),
                         t=round(float(t), 2)))
    rows.sort(key=lambda r: -r["sharpe"])

    return dict(
        days=T, universe=len(C.columns), n_trials=N,
        best=dict(factor=best_name, factor_cn=validation._FACTORS.get(best_name, best_name),
                  sharpe=round(best_sr, 2), deflated_sr0=round(sr0, 2),
                  dsr=round(dsr, 4), t_stat=round(float(z / math.sqrt(max(T - 1, 1)) * math.sqrt(TRADING_DAYS)), 2)),
        pbo=round(pbo, 4),
        pbo_logit_mean=round(float(np.mean(logits)), 3) if logits else None,
        trials=rows,
        verdict=_dsr_verdict(dsr, pbo),
        note=("DSR 由 Bailey & López de Prado 方法计算：SR₀ 随试验数 N 与试验间夏普离散度上升，"
              "真实能力需显著超过 SR₀。PBO<0.5 才算「样本内最优在样本外仍优于中位数」。"
              f"本次 N={N} 个因子试验，T={T} 个交易日。"),
    )


def _dsr_verdict(dsr, pbo):
    if dsr >= 0.95 and pbo <= 0.3:
        return "可信：校正试验次数后仍显著，且过拟合概率低"
    if dsr >= 0.9 and pbo <= 0.5:
        return "可参考：显著性尚可，但需警惕样本外衰减"
    if pbo > 0.5:
        return "高风险：样本内最优在样本外大概率落到中位数以下（过拟合）"
    return "不显著：校正多重试验后已无统计优势"


def _pbo(R, blocks=8):
    """Combinatorially Symmetric Cross-Validation 估计过拟合概率"""
    from itertools import combinations
    n = len(R)
    b = blocks
    bs = max(1, n // b)
    R = R.iloc[-b * bs:]
    chunks = [R.iloc[i * bs:(i + 1) * bs] for i in range(b)]
    logits = []
    half = b // 2
    for comb in combinations(range(b), half):
        is_idx = set(comb)
        oos_idx = [i for i in range(b) if i not in is_idx]
        is_mean = pd.concat([chunks[i] for i in range(b) if i in is_idx]).mean()
        oos_mean = pd.concat([chunks[i] for i in oos_idx]).mean()
        if is_mean.isna().all() or oos_mean.isna().all():
            continue
        best = is_mean.idxmax()
        # 该策略在样本外的相对排名（0~1）
        ranks = oos_mean.rank(pct=True)
        w = float(ranks.get(best, 0.5))
        w = min(max(w, 1e-6), 1 - 1e-6)
        logits.append(math.log(w / (1 - w)))
    if not logits:
        return 0.5, []
    pbo = float(np.mean(np.array(logits) <= 0))
    return pbo, logits


# ================================================================== 5. 因子衰减

def factor_decay(codes=None, days=500):
    """每个因子的 IC 随持有期衰减 + 指数拟合半衰期"""
    dat = _panel(codes, need=max(days + 320, 800))
    C, trad = dat["C"], dat["trad"]
    fac = dat["fac"]
    idx = C.index[-int(days):]
    horizons = [1, 2, 3, 5, 10, 20, 40, 60]
    ind_d, _ = _ind_dummies(dat["ind"], C.index)
    mcap = pd.Series([math.log1p(float(v)) if v else np.nan for v in dat["mktcap"]],
                     index=C.columns)

    rows, curves = [], {}
    neu = _neutralize_many(fac, ind_d, mcap, idx)   # 一次性中性化，避免逐因子逐日回归
    fwds = {h: (C.shift(-h) / C - 1).loc[idx] for h in horizons}   # 每个持有期只算一次
    for name, f in fac.items():
        fs = neu[name]
        ic_list = []
        for h in horizons:
            fwd = fwds[h].loc[idx[:-h]]
            ic_s = _ic_fast(fs.loc[idx[:-h]], fwd, trad)
            ic_list.append(float(ic_s.mean()) if len(ic_s) else 0.0)
        curves[name] = ic_list
        # 半衰期：只对与 h=1 同号、且 |IC| 足够大的点做 log-linear 回归（符号翻转即失效）
        s0 = 1.0 if ic_list[0] >= 0 else -1.0
        hs = [h for h, v in zip(horizons, ic_list)
              if v * s0 > 0 and abs(v) > 1e-4]
        half = None
        if len(hs) >= 3:
            xs = np.array(hs, dtype=float)
            ys = np.log([abs(curves[name][horizons.index(h)]) for h in hs])
            try:
                slope, _ = np.polyfit(xs, ys, 1)
                if slope < -1e-6:
                    half = round(float(math.log(2) / (-slope)), 1)
            except Exception:  # noqa: BLE001
                half = None
        decay_ratio = (abs(ic_list[-1]) / abs(ic_list[0]) if abs(ic_list[0]) > 1e-9 else None)
        rows.append(dict(
            factor=name, factor_cn=validation._FACTORS.get(name, name),
            ic_1=round(ic_list[0], 4), ic_5=round(ic_list[3], 4),
            ic_20=round(ic_list[5], 4), ic_60=round(ic_list[7], 4),
            half_life=half,
            decay_ratio=round(decay_ratio, 3) if decay_ratio is not None else None,
            persistence=("慢衰减(可低频调仓)" if half and half >= 20 else
                         "中速" if half and half >= 5 else
                         "快衰减(需高频调仓)" if half else "不可估"),
        ))
    rows.sort(key=lambda r: -(r["half_life"] or 0))
    return dict(days=len(idx), horizons=horizons, rows=rows,
                curves=[dict(factor=k, factor_cn=validation._FACTORS.get(k, k), ic=v)
                        for k, v in curves.items()],
                note=("半衰期由 |IC| 对持有期做指数拟合 ln|IC|=ln IC₀−λh，half=ln2/λ。"
                      "半衰期越长，调仓频率可以越低、交易成本越省；越短则必须快进快出，"
                      "对执行成本与延迟更敏感。"))


# ================================================================== 6. GBDT 非线性合成

class _GBDT:
    """直方图梯度提升树（纯 numpy，二分类/回归通用），用于横截面排序学习"""

    def __init__(self, n_trees=30, depth=3, lr=0.06, min_leaf=40, n_bins=16):
        self.n_trees = n_trees
        self.depth = depth
        self.lr = lr
        self.min_leaf = min_leaf
        self.n_bins = n_bins
        self.trees = []
        self.edges = None

    def _bin(self, X):
        B = np.empty(X.shape, dtype=np.int16)
        for f in range(X.shape[1]):
            B[:, f] = np.searchsorted(self.edges[f], X[:, f])
        return B

    def fit(self, X, y):
        X = np.asarray(X, dtype=np.float64)
        y = np.asarray(y, dtype=np.float64)
        n, nf = X.shape
        self.edges = []
        for f in range(nf):
            qs = np.linspace(0, 1, self.n_bins + 1)[1:-1]
            self.edges.append(np.unique(np.quantile(X[:, f], qs)))
        B = self._bin(X)
        pred = np.full(n, y.mean())
        self.trees = []
        for _ in range(self.n_trees):
            resid = y - pred
            tree = self._grow(B, resid, np.arange(n), 0)
            self.trees.append(tree)
            pred += self.lr * self._pred_tree(tree, B, np.arange(n))
        self.base = float(y.mean())
        return self

    def _grow(self, B, resid, idx, depth):
        if depth >= self.depth or len(idx) < 2 * self.min_leaf:
            return ("leaf", float(resid[idx].mean()))
        best = None
        tot_sum = float(resid[idx].sum())
        tot_cnt = len(idx)
        tot_sq = tot_sum * tot_sum / max(tot_cnt, 1)
        for f in range(B.shape[1]):
            b = B[idx, f]
            cnt = np.bincount(b, minlength=self.n_bins + 2)
            ssum = np.bincount(b, weights=resid[idx], minlength=self.n_bins + 2)
            cum_c = np.cumsum(cnt)[:-1]
            cum_s = np.cumsum(ssum)[:-1]
            right_c = tot_cnt - cum_c
            right_s = tot_sum - cum_s
            ok = (cum_c >= self.min_leaf) & (right_c >= self.min_leaf)
            if not ok.any():
                continue
            gain = np.where(ok, cum_s ** 2 / np.maximum(cum_c, 1)
                            + right_s ** 2 / np.maximum(right_c, 1) - tot_sq, -np.inf)
            k = int(np.argmax(gain))
            if best is None or gain[k] > best[0]:
                best = (float(gain[k]), f, k)
        if best is None or best[0] <= 0:
            return ("leaf", float(resid[idx].mean()))
        _, f, k = best
        mask = B[idx, f] <= k
        left, right = idx[mask], idx[~mask]
        if len(left) < self.min_leaf or len(right) < self.min_leaf:
            return ("leaf", float(resid[idx].mean()))
        return ("node", f, k,
                self._grow(B, resid, left, depth + 1),
                self._grow(B, resid, right, depth + 1))

    def _pred_tree(self, node, B, idx):
        out = np.zeros(len(idx), dtype=np.float64)
        stack = [(node, np.arange(len(idx)))]
        while stack:
            nd, sel = stack.pop()
            if nd[0] == "leaf":
                out[sel] = nd[1]
                continue
            _, f, k, L, R = nd
            m = B[idx[sel], f] <= k
            stack.append((L, sel[m]))
            stack.append((R, sel[~m]))
        return out

    def predict(self, X):
        X = np.asarray(X, dtype=np.float64)
        B = self._bin(X)
        out = np.full(len(X), self.base)
        idx = np.arange(len(X))
        for tree in self.trees:
            out += self.lr * self._pred_tree(tree, B, idx)
        return out


def ml_alpha(codes=None, days=700, train_ratio=0.6, horizon=5):
    """GBDT 非线性因子合成 vs 线性 ICIR 加权（样本外 IC 对比）"""
    dat = _panel(codes, need=max(days + 320, 1000))
    C, trad = dat["C"], dat["trad"]
    fac = dat["fac"]
    idx = C.index[-int(days):]
    split = int(len(idx) * train_ratio)
    train, test = idx[:split], idx[split:]
    fwd = C.shift(-horizon) / C - 1
    ind_d, _ = _ind_dummies(dat["ind"], C.index)
    mcap = pd.Series([math.log1p(float(v)) if v else np.nan for v in dat["mktcap"]],
                     index=C.columns)

    # 训练段按 IC 选前 8 个因子（一次性批量中性化，避免逐日重复回归）
    neu = _neutralize_many(fac, ind_d, mcap, idx)
    stats = []
    for name, f in fac.items():
        fs = neu[name].loc[train]
        ic_s = _ic_fast(fs.loc[train[:-horizon]], fwd.loc[train[:-horizon]], trad)
        if len(ic_s) >= 30:
            stats.append((name, float(ic_s.mean())))
    stats.sort(key=lambda x: -abs(x[1]))
    top = [n for n, _ in stats[:8]] or list(fac)[:8]

    # 构造训练矩阵（逐日横截面 z-score 后堆叠，目标=未来收益横截面排名）
    def build(days_list):
        Xs, ys = [], []
        for dt in days_list:
            cols, y = [], []
            ok = C.loc[dt].notna() & trad.loc[dt].fillna(True)
            for name in top:
                z = neu[name].loc[dt]
                cols.append(z.reindex(C.columns))
                ok &= z.reindex(C.columns).notna()
            yv = fwd.loc[dt].reindex(C.columns)
            ok &= yv.notna()
            if int(ok.sum()) < 15:
                continue
            M = pd.concat(cols, axis=1)
            M.columns = top
            M = M[ok]
            yv = yv[ok]
            # 目标：横截面排名映射到 [-0.5, 0.5]
            r = yv.rank(pct=True) - 0.5
            Xs.append(M.to_numpy(dtype=float))
            ys.append(r.to_numpy(dtype=float))
        if not Xs:
            return np.empty((0, len(top))), np.empty(0)
        return np.vstack(Xs), np.concatenate(ys)

    Xtr, ytr = build(list(train[:-horizon]))
    if len(Xtr) < 500:
        raise RuntimeError("训练样本不足，无法拟合 GBDT")
    # 下采样控制耗时
    if len(Xtr) > 45000:
        sel = np.random.default_rng(42).choice(len(Xtr), 45000, replace=False)
        Xtr, ytr = Xtr[sel], ytr[sel]
    Xtr = np.nan_to_num(Xtr, nan=0.0, posinf=0.0, neginf=0.0)

    gb = _GBDT(n_trees=30, depth=3, lr=0.06, min_leaf=40, n_bins=16).fit(Xtr, ytr)

    # 样本外逐日 IC
    def daily_ic(pred_fn):
        ics = []
        for dt in test[:-horizon]:
            ok = C.loc[dt].notna() & trad.loc[dt].fillna(True) & fwd.loc[dt].notna()
            if int(ok.sum()) < 12:
                continue
            cols = []
            for name in top:
                fs = validation._neutralize(fac[name].loc[[dt]], ind_d, mcap)
                cols.append(fs.loc[dt].reindex(C.columns))
            M = pd.concat(cols, axis=1)
            M.columns = top
            Mv = np.nan_to_num(M[ok].to_numpy(dtype=float), nan=0.0, posinf=0.0, neginf=0.0)
            pv = pred_fn(Mv)
            yv = fwd.loc[dt][ok].to_numpy(dtype=float)
            if len(pv) < 12:
                continue
            rho = spearmanr(pv, yv)[0]
            if not pd.isna(rho):
                ics.append(float(rho))
        return ics

    gb_ics = daily_ic(lambda M: gb.predict(np.nan_to_num(M, nan=0.0)))
    # 线性对照：ICIR 加权（用训练段 IC 符号）
    wmap = {n: (1 if ic >= 0 else -1) * abs(ic) for n, ic in stats[:8]}
    wtot = sum(abs(v) for v in wmap.values()) or 1.0

    def lin_pred(M):
        s = np.zeros(len(M))
        for i, n in enumerate(top):
            s += M[:, i] * (wmap.get(n, 0.0) / wtot)
        return s

    lin_ics = daily_ic(lin_pred)

    def summary(ics, name):
        if not ics:
            return dict(name=name, ic_mean=0.0, icir=0.0, win_rate=0.0, n=0)
        a = np.array(ics)
        return dict(name=name, ic_mean=round(float(a.mean()), 4),
                    icir=round(float(a.mean() / a.std(ddof=1)), 3) if a.std(ddof=1) > 0 else 0.0,
                    win_rate=round(float((a > 0).mean()) * 100, 1), n=len(a))

    gs = summary(gb_ics, "GBDT 非线性")
    ls = summary(lin_ics, "线性 ICIR 加权")
    improve = round(gs["ic_mean"] - ls["ic_mean"], 4)

    # 特征重要性（按分裂次数）
    imp = {n: 0 for n in top}
    for tree in gb.trees:
        stack = [tree]
        while stack:
            nd = stack.pop()
            if nd[0] == "leaf":
                continue
            _, f, k, L, R = nd
            imp[top[f]] += 1
            stack.extend([L, R])
    tot = sum(imp.values()) or 1
    imp_rows = [dict(factor=k, factor_cn=validation._FACTORS.get(k, k),
                     splits=v, importance=round(v / tot * 100, 1))
                for k, v in sorted(imp.items(), key=lambda x: -x[1])]

    return dict(
        days=len(idx), train_days=len(train), test_days=len(test), horizon=horizon,
        features=top, n_train=int(len(Xtr)),
        gbdt=gs, linear=ls, improve_ic=improve,
        verdict=("GBDT 显著优于线性（+{:.4f}）".format(improve) if improve >= 0.005 else
                 "GBDT 与线性相当（±0.005 内）" if abs(improve) < 0.005 else
                 "线性优于 GBDT（{:.4f}）".format(improve)),
        importance=imp_rows,
        note=("GBDT 用直方图分裂（深度 3、30 棵树、学习率 0.06），目标为未来{}日收益的横截面排名；"
              "线性对照为训练段 ICIR 加权。两者用同一批因子与同一段样本外区间，"
              "仅比较「非线性交互」是否带来额外 IC。A 股日频数据信噪比低，"
              "GBDT 提升有限甚至为负是常见结果。").format(horizon),
    )


# ================================================================== 7. 多策略配置

def _factor_strategies(dat, idx, horizon=5):
    """由因子面板直接构造 5 条子策略日收益（多空，快速、无需组合回测）"""
    C, trad = dat["C"], dat["trad"]
    fwd = C.shift(-horizon) / C - 1
    specs = {
        "momentum": ("mom_20", 1),
        "reversal": ("rev_10", 1),
        "lowvol": ("vol_20", 1),
        "liquidity": ("turn_20", -1),
        "quality_trend": ("mdd_60", 1),
    }
    out, meta = {}, {}
    for key, (fname, sign) in specs.items():
        if fname not in dat["fac"]:
            continue
        f = dat["fac"][fname]
        z = f.sub(f.mean(axis=1), axis=0).div(f.std(axis=1).where(lambda s: s > 1e-9), axis=0)
        z = z.fillna(0.0) * sign
        rets = []
        for dt in idx[:-horizon]:
            zi = z.loc[dt]
            ok = zi.notna() & trad.loc[dt].fillna(True)
            if int(ok.sum()) < 12:
                rets.append(0.0)
                continue
            zz = zi[ok]
            k = max(1, int(len(zz) * 0.3))
            hi, lo = zz.nlargest(k).index, zz.nsmallest(k).index
            r1, r0 = fwd.loc[dt, hi].mean(), fwd.loc[dt, lo].mean()
            rets.append(float(r1 - r0) if not (pd.isna(r1) or pd.isna(r0)) else 0.0)
        out[key] = pd.Series(rets, index=idx[:-horizon], dtype=float)
        meta[key] = fname
    return out, meta


def strategy_alloc(codes=None, days=600, horizon=5):
    """多策略资金配置：等权 / 风险平价 / 最小方差 / 最大夏普"""
    dat = _panel(codes, need=max(days + 320, 900))
    idx = dat["C"].index[-int(days):]
    strat, meta = _factor_strategies(dat, idx, horizon)
    if len(strat) < 3:
        raise RuntimeError("可用子策略不足 3 条")
    R = pd.DataFrame(strat).dropna()
    cols = list(R.columns)
    mu = R.mean().to_numpy() * TRADING_DAYS
    Sig = R.cov().to_numpy() * TRADING_DAYS
    Sig = Sig + np.eye(len(cols)) * 1e-6

    def perf(w):
        ret = R.to_numpy() @ w
        ann = float(np.mean(ret) * TRADING_DAYS * 100)
        vol = float(np.std(ret, ddof=1) * math.sqrt(TRADING_DAYS) * 100)
        sharpe = ann / vol if vol > 0 else 0.0
        nav = np.cumprod(1 + ret)
        mdd = float((nav / np.maximum.accumulate(nav) - 1).min() * 100)
        return dict(ann_ret=round(ann, 2), ann_vol=round(vol, 2),
                    sharpe=round(sharpe, 2), mdd=round(mdd, 2))

    n = len(cols)
    methods = {}
    methods["equal"] = dict(name="等权", weights=np.ones(n) / n)
    iv = 1.0 / np.sqrt(np.diag(Sig))
    methods["risk"] = dict(name="风险平价(逆波动)", weights=iv / iv.sum())

    # 最小方差：解析解 w ∝ Σ⁻¹1
    try:
        inv = np.linalg.pinv(Sig)
        w = inv @ np.ones(n)
        w = np.clip(w, 0, None)
        methods["minvar"] = dict(name="最小方差", weights=w / w.sum())
    except Exception:  # noqa: BLE001
        pass

    # 最大夏普：投影梯度（长仓）
    w = np.ones(n) / n
    for _ in range(2000):
        w = np.clip(w + 0.05 * (mu - (mu @ w) / max(w @ Sig @ w, 1e-12) * (Sig @ w)), 0, None)
        s = w.sum()
        if s <= 1e-12:
            w = np.ones(n) / n
        else:
            w = w / s
    methods["maxsharpe"] = dict(name="最大夏普", weights=w)

    rows = []
    for key, m in methods.items():
        wv = np.asarray(m["weights"], dtype=float)
        p = perf(wv)
        rows.append(dict(key=key, name=m["name"],
                         weights={c: round(float(wv[i]) * 100, 1) for i, c in enumerate(cols)},
                         **p))
    rows.sort(key=lambda r: -r["sharpe"])

    corr = R.corr().round(3)
    pairs = []
    for i in range(n):
        for j in range(i + 1, n):
            pairs.append(dict(a=cols[i], b=cols[j], corr=round(float(corr.iloc[i, j]), 3)))
    pairs.sort(key=lambda x: x["corr"])

    return dict(
        days=len(R), strategies=[dict(key=c, factor=meta.get(c, ""),
                                      factor_cn=validation._FACTORS.get(meta.get(c, ""), c))
                                 for c in cols],
        methods=rows,
        correlations=pairs,
        correlation_matrix=dict(codes=cols, matrix=corr.values.tolist()),
        note=("子策略均由因子面板直接构造多空组合（前 30% 做多 / 后 30% 做空），"
              "因此是相对收益，不含市场 beta。配置权重基于历史收益与协方差，"
              "最大夏普对输入误差极敏感，仅作参考，不是未来收益预测。"),
    )


# ================================================================== 8. 全景矩阵

def inst_matrix():
    """顶级私募「功能 × 数据支撑 × 个人劣势」全景矩阵"""
    layers = [
        ("数据层", "Point-in-Time 财务（bitemporal，防重述未来函数）", "标配",
         "财报公告日 + 修订版本表", "gap",
         "需付费 PIT 库（Wind/朝阳永续），本环境无；纯量价因子不受影响"),
        ("数据层", "Level-2 逐笔 / 十档委托队列", "高频必备",
         "交易所逐笔成交 + 委托队列快照", "gap", "交易所付费行情，沙箱不可得"),
        ("数据层", "分钟级 / Tick 行情", "中高频必备", "行情商 CTP/Level-2 分笔", "gap",
         "免费分钟源被沙箱网络拦截"),
        ("数据层", "全样本含退市 / 长期停牌", "标配", "含退市代码的历史全库", "gap",
         "股票池仅当前上市标的，幸存者偏差向上（审计页已量化）"),
        ("数据层", "另类数据（舆情/研报/卫星/供应链）", "头部标配",
         "NLP 舆情、卖方研报、卫星影像、海关数据", "gap", "无数据源与授权"),
        ("数据层", "北向 / 龙虎榜 / 资金流", "常用", "东财 datacenter 公开接口", "have",
         "已接入：北向持股、龙虎榜、主力资金"),
        ("数据层", "融资融券 / 大宗交易 / 股东户数", "常用", "交易所公开披露", "partial",
         "可加：交易所日频公开数据，需新增采集器"),
        ("数据层", "日频 OHLCV 全量", "标配", "新浪/东财日 K", "have",
         "已具备：主流标的 1400+ 根，本次已用满 OHLCV"),
        ("数据层", "涨跌停 / 停牌可成交性", "标配", "逐日涨跌停价 + 停牌标记", "have",
         "已实现：一字板 + 停牌过滤"),
        ("因子层", "量价因子库（数百个）", "标配", "自研因子工厂 + 遗传规划", "partial",
         "已 14 个纯量价因子；头部数百个，差距在自动化挖掘管线"),
        ("因子层", "基本面因子库（数百 + 分析师预期）", "标配",
         "三大报表 + 一致预期", "partial", "已有 13 个；缺分析师预期"),
        ("因子层", "行业 / 市值中性化", "标配", "行业分类 + 市值", "have",
         "已实现：逐日 OLS 残差"),
        ("因子层", "因子正交化 / 共线性处理", "标配", "相关矩阵 + 施密特正交", "have",
         "已实现：Spearman>0.55 剔除"),
        ("因子层", "因子衰减曲线 / 半衰期", "常用", "多持有期 IC", "have",
         "本次新增：8 个持有期 IC + 指数拟合半衰期"),
        ("因子层", "横截面 IC / ICIR / t 值", "标配", "逐日横截面秩相关", "have",
         "已具备 + Bonferroni/BH 校正"),
        ("因子层", "Alpha 因子自动挖掘（GP / 遗传规划）", "头部标配",
         "算子库 + GPU 评估集群", "gap", "需算力与工程管线，单机成本过高"),
        ("模型层", "线性多因子合成", "标配", "ICIR 加权 / 打分", "have",
         "已实现：ICIR 加权 + 方向自适应"),
        ("模型层", "机器学习非线性合成（GBDT/NN/LTR）", "头部标配",
         "特征矩阵 + GPU 训练", "have", "本次新增：直方图 GBDT + 排序目标，样本外 IC 对比"),
        ("模型层", "深度学习序列模型（LSTM/Transformer）", "头部标配",
         "高频序列 + GPU", "gap", "无 GPU 集群，日频样本量不足"),
        ("模型层", "集成学习 / 多模型投票", "常用", "多模型 OOF 预测", "partial",
         "已有 21 家 LLM 委员会；量化模型侧仅单 GBDT"),
        ("组合层", "Barra 风格风险模型", "标配", "10 风格 + 行业 + 特质风险", "have",
         "本次新增：暴露估计 + 因子协方差 + 风险分解"),
        ("组合层", "带约束的组合优化", "标配", "凸优化 QP", "have",
         "本次新增：投影梯度 + 单票/行业/风格/换手约束"),
        ("组合层", "风险平价 / 均值-方差 / 最大夏普", "标配", "协方差矩阵", "have",
         "本次新增：多策略配置四种方法对比"),
        ("组合层", "行业权重上限", "标配", "行业分类", "have", "已具备：默认 ≤35%"),
        ("组合层", "做空 / 融资融券 / 股指对冲", "标配", "券源 + 期指", "gap",
         "仅多头；融券券源与期指账户不可得"),
        ("执行层", "算法交易 TWAP/VWAP/POV", "标配", "日内成交量分布", "partial",
         "本次提供成本代理对比；真实执行需券商接口"),
        ("执行层", "冲击成本模型 / TCA", "标配", "成交回报 + 市场冲击回归", "have",
         "本次新增：Almgren-Chriss 代理 + 容量曲线"),
        ("执行层", "容量分析（AUM 衰减曲线）", "头部标配", "ADV + 冲击成本", "have",
         "本次新增：0.5–50 亿 AUM 下的净收益衰减与容量上限"),
        ("执行层", "券商 API 实盘下单", "标配", "券商柜台 / CTP", "gap",
         "需券商授权 + 合规资质"),
        ("验证层", "Walk-forward（含 purge/embargo）", "标配", "时间序列切分", "have",
         "已具备：训练段末 horizon 天截断防泄漏"),
        ("验证层", "Deflated Sharpe Ratio", "头部标配", "试验次数 + 夏普分布", "have",
         "本次新增：N=14 试验的 DSR 校正"),
        ("验证层", "PBO 过拟合概率（CSCV）", "头部标配", "多配置收益矩阵", "have",
         "本次新增：8 分块 70 种切分的 PBO"),
        ("验证层", "多重检验校正（Bonferroni / BH）", "常用", "p 值矩阵", "have",
         "已具备"),
        ("验证层", "概率校准（Brier / 分桶）", "常用", "预测-结果配对", "have",
         "已具备：保序回归 PAV"),
        ("验证层", "Brinson 收益归因", "标配", "配置/选股/交互三效应", "have",
         "已具备"),
        ("验证层", "Paper trading 逐日模拟盘", "标配", "逐日净值 + 交易记录", "have",
         "已具备"),
        ("风控层", "组合风险模型（VaR/ES/Beta）", "标配", "收益分布 + 协方差", "have",
         "已具备：95% VaR、ES、Beta、风险贡献"),
        ("风控层", "风格暴露实时约束", "标配", "Barra 暴露", "have",
         "本次新增：优化器内嵌 |z|≤1 约束 + 越界投影"),
        ("风控层", "回撤熔断 / 自动止损", "标配", "实时净值监控", "partial",
         "有预警，无自动执行（需实盘通道）"),
        ("风控层", "压力测试 / 情景分析", "常用", "历史极端日重放", "partial",
         "可用历史极端区间做情景重放，尚未产品化"),
        ("工程层", "三层存储（热/温/冷）", "头部标配", "Redis + Parquet + 对象存储", "partial",
         "本地 SQLite + 内存缓存；单机规模可用，未做分布式"),
        ("工程层", "并行计算 / 分布式回测", "头部标配", "集群 + 任务队列", "partial",
         "线程池并行取数；无集群"),
        ("工程层", "低延迟交易链路", "高频必备", "FPGA/内核旁路", "gap",
         "本环境不适用"),
    ]
    rows = [dict(layer=l, capability=c, standard=s, data=d, status=st, note=n)
            for l, c, s, d, st, n in layers]
    counts = {"have": 0, "partial": 0, "gap": 0}
    for r in rows:
        counts[r["status"]] += 1

    retail = [
        dict(dim="数据广度", inst="Level-2/Tick、PIT 财务、另类数据、全样本含退市",
             retail="免费日 K + 公开财报快照",
             gap="数据维度相差 3–5 个数量级；因子上限被数据卡死",
             we_have="日频 OHLCV + 北向/龙虎榜/资金流 + 行业/财务快照"),
        dict(dim="因子数量", inst="数百至数千个，自动挖掘管线日产数百",
             retail="手工 3–10 个指标",
             gap="因子越少越容易过拟合单一风格；但因子多也需多重检验校正",
             we_have="14 个量价 + 13 个基本面，全部做 IC/BH 校正"),
        dict(dim="算力", inst="GPU 集群 + 分布式回测，日跑百万组参数",
             retail="单机 CPU", gap="搜索空间小 3–4 个数量级",
             we_have="线程池并行；GBDT 单机可训练"),
        dict(dim="执行成本", inst="算法交易 + 券商返佣，冲击成本压到 3–8bp",
             retail="市价单，冲击 + 滑点 15–40bp",
             gap="每年被成本吃掉 5–15 个百分点",
             we_have="Almgren-Chriss 成本代理 + TWAP/VWAP/POV 对比 + 容量曲线"),
        dict(dim="风险控制", inst="独立风控部门 + Barra 约束 + 自动熔断",
             retail="凭感觉止损，无组合层风险预算",
             gap="极端行情回撤不可控",
             we_have="Barra 风险分解 + 优化器内嵌暴露约束"),
        dict(dim="过拟合防护", inst="DSR + PBO + 多重检验 + 独立样本外",
             retail="几乎无，看到回测好就上",
             gap="个人策略样本外衰减通常 -50% 以上",
             we_have="DSR + PBO + BH-FDR + walk-forward 四道闸"),
        dict(dim="研究团队", inst="数十至数百人，因子/模型/工程/风控分工",
             retail="1 人全包", gap="覆盖深度受限；但个人可聚焦少数高确定性策略",
             we_have="AI 多模型委员会（21 家 LLM）+ 规则引擎辅助"),
        dict(dim="容量与规模", inst="数十亿至千亿，规模本身是约束",
             retail="几十万至几百万，容量不是问题",
             gap="个人在容量上反而有优势（小船好调头）",
             we_have="容量曲线量化「AUM 多大时 alpha 被成本吃光」"),
        dict(dim="合规与通道", inst="券商专线、托管、合规审查",
             retail="普通经纪账户", gap="无法做空/杠杆/期指对冲",
             we_have="无（仅多头，已如实标注 gap）"),
        dict(dim="信息时效", inst="实时行情 + 公告秒级解析",
             retail="收盘后手工复盘", gap="反应速度慢半天至一天",
             we_have="日频收盘后自动跑，30 分钟自主盯盘 + Webhook 推送"),
    ]

    return dict(
        rows=rows, counts=counts, total=len(rows),
        retail=retail,
        conclusion=(
            f"共 {len(rows)} 项能力：已具备 {counts['have']} / 部分 {counts['partial']} / 缺失 {counts['gap']}。"
            "本次把「Barra 风险模型、带约束组合优化、算法交易与容量分析、DSR/PBO 过拟合诊断、"
            "因子衰减半衰期、GBDT 非线性合成、多策略配置」7 组头部私募核心组件落地为可运行代码。"
            "仍缺的集中在两类：一是付费数据与硬件（Level-2、分钟级、PIT 财务、另类数据、GPU），"
            "二是牌照与通道（做空、期指、券商实盘）。这两类不是代码能补齐的。"
        ),
        note=("「数据支撑」列说明每项能力在头部私募实际依赖什么数据；「本站」列如实标注可落地程度。"
              "个人 vs 机构对比中的 we_have 列是本站用免费数据能做到的替代方案。"),
    )
