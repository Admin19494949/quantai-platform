# -*- coding: utf-8 -*-
"""全球数据 → A 股模型的融合层

把 world_data 接入的 10 大类全球资产，转成 6 个可回归的全球因子，并落到
现有模型的四个环节：

  1. 因子层  world_alpha    —— 新增 3 个全球横截面因子：
                              全球Beta(gb_beta)、美元敏感度(usd_beta)、
                              行业全球动量(sec_gmom)，与原有 14 个量价因子
                              一起做样本外 IC 对比（线性 ICIR 加权）
  2. 风险层  world_beta     —— 组合对 6 个全球因子的回归 beta / t / R²，
                              给出用 ES、SPY 等标的对冲的等值名义金额
  3. 择时层  regime         —— 全球股票趋势 × 美元趋势 四象限，统计各状态
                              下 A 股未来 5 日收益与胜率，给仓位系数
  4. 数据层  dashboard      —— 全球因子收益、相关矩阵、与 A 股同期/滞后相关

时间对齐（防止未来函数）：所有全球因子一律**滞后 1 个交易日**接入 A 股，
即用 t-1 收盘（美股/欧股/期货）预测 t 日 A 股；leadlag 单独给出 0~5 日
超前滞后结构，用于验证这个假设。
"""
import math

import numpy as np
import pandas as pd
from scipy import stats as sps

from . import institutional as inst, validation, world_data as wd

TRADING_DAYS = 244

# ================================================================ 全球因子定义

FACTORS = {
    "G_EQ": dict(name="全球股票风险偏好", unit="",
                 pairs=[("gf", "ES"), ("gf", "NQ"), ("gf", "NK"), ("gf", "HSI"),
                        ("us", "EWJ"), ("us", "FXI"), ("us", "EWG"), ("us", "EWU"),
                        ("us", "EWY"), ("us", "INDA"), ("us", "EWZ")],
                 live=[("idx", "int_sp500"), ("idx", "b_NKY"), ("idx", "b_HSI")]),
    "G_USD": dict(name="美元强度", unit="",
                  pairs=[("fx", "fx_susdcny"), ("fx", "fx_susdjpy"), ("fx", "fx_susdchf"),
                         ("fx", "fx_susdcad"), ("fx", "fx_eurusd", -1),
                         ("fx", "fx_sgbpusd", -1), ("fx", "fx_saudusd", -1)],
                  live=[("fx", "fx_susdindex")]),
    "G_CMD": dict(name="全球商品", unit="",
                  pairs=[("cmd", "CL"), ("cmd", "GC"), ("cmd", "HG"), ("cmd", "NG")],
                  live=[("cmd", "CL"), ("cmd", "GC")]),
    "G_RATE": dict(name="利率/久期（债券价格）", unit="",
                   pairs=[("us", "TLT"), ("us", "IEF")],
                   live=[("us", "TLT")]),
    "G_CRYPTO": dict(name="加密风险偏好", unit="",
                     pairs=[("crypto", "BTC")],
                     live=[("crypto", "BTC")]),
    "G_CN": dict(name="中国离岸资产", unit="",
                 pairs=[("gf", "HSI"), ("us", "FXI"), ("us", "KWEB"),
                        ("fx", "fx_susdcny", -1)],
                 live=[("idx", "b_HSI")]),
}

KEY = "sh000300"          # A 股基准：沪深300
_A_CACHE = {}
_A_TTL = 900.0


# ================================================================ 基础序列

def a_returns(days=800):
    """沪深300 日收益（升序）"""
    import time
    now = time.time()
    hit = _A_CACHE.get(days)
    if hit and now - hit[0] < _A_TTL:
        return hit[1]
    rows = wd.kline("cnidx", KEY)
    # 统一用字符串日期做索引：与 validation._wide 的 A 股面板口径一致（"YYYY-MM-DD"）
    s = pd.Series([r["close"] for r in rows], index=pd.Index([r["day"] for r in rows]))
    s = s.sort_index()
    s = s[~s.index.duplicated(keep="last")].tail(int(days) + 5)
    ret = s.pct_change()
    _A_CACHE[days] = (now, ret)
    return ret


def _member_returns(pairs):
    """单个因子里各标的的日收益（各自交易日），返回对齐后的 DataFrame"""
    out = {}
    for p in pairs:
        cat, sym = p[0], p[1]
        sign = p[2] if len(p) > 2 else 1
        try:
            rows = wd.kline(cat, sym)
        except Exception:  # noqa: BLE001
            continue
        if len(rows) < 40:
            continue
        s = pd.Series([r["close"] for r in rows], index=pd.Index([r["day"] for r in rows]))
        s = s.sort_index()
        if s.index.has_duplicates:
            s = s[~s.index.duplicated(keep="last")]
        out[f"{cat}:{sym}"] = s.pct_change() * sign
    return pd.DataFrame(out).sort_index() if out else pd.DataFrame()


def factor_returns(days=800, lag=0):
    """6 个全球因子日收益；lag=1 表示滞后一日（接入 A 股时的默认口径）"""
    a = a_returns(days)
    cal = a.index
    out = {}
    for k, meta in FACTORS.items():
        m = _member_returns(meta["pairs"])
        if m.empty:
            continue
        f = m.mean(axis=1, skipna=True)
        f = f.reindex(f.index.union(cal)).ffill().reindex(cal)
        out[k] = f.shift(lag) if lag else f
    df = pd.DataFrame(out)
    return df.tail(days)


# ================================================================ 1. 数据层看板

def dashboard(days=600):
    """全球因子收益/波动/相关矩阵 + 与 A 股同期及滞后相关"""
    fr = factor_returns(days, lag=0)
    a = a_returns(days).reindex(fr.index)
    rows = []
    for k in fr.columns:
        s = fr[k].dropna()
        if len(s) < 30:
            continue
        cum = lambda n: float((1 + s.tail(n)).prod() - 1) * 100
        vol = float(s.tail(60).std() * math.sqrt(TRADING_DAYS) * 100)
        c0 = _corr(s.reindex(a.index), a)
        c1 = _corr(s.shift(1).reindex(a.index), a)
        rows.append(dict(
            key=k, name=FACTORS[k]["name"],
            d1=round(cum(1), 2), d5=round(cum(5), 2), d20=round(cum(20), 2),
            d60=round(cum(60), 2), ann_vol=round(vol, 2),
            corr_a_same=round(c0[0], 3) if c0 else None,
            corr_a_lag1=round(c1[0], 3) if c1 else None,
            n=int(len(s))))
    cm = fr.corr().round(3)
    return dict(
        days=int(len(fr)), rows=rows,
        corr_codes=list(cm.columns),
        corr_matrix=[[None if pd.isna(v) else float(v) for v in r] for r in cm.values],
        a_key=KEY,
        note=("全球因子由各成员标的日收益等权合成（标的方向已按因子含义调整符号）。"
              "corr_a_same 为与沪深300「同日」相关（美股同日收盘晚于 A 股，含前视，仅作参照）；"
              "corr_a_lag1 为「全球 t-1 对 A 股 t」，这才是可交易的口径。"))


def _corr(x, y):
    d = pd.concat([x, y], axis=1).dropna()
    if len(d) < 30:
        return None
    r = float(d.iloc[:, 0].corr(d.iloc[:, 1]))
    if not np.isfinite(r):
        return None
    n = len(d)
    t = r * math.sqrt(n - 2) / math.sqrt(max(1e-12, 1 - r * r))
    return r, t, n


# ================================================================ 2. 领先滞后

def leadlag(days=500, max_lag=5):
    """A 股 vs 各全球因子的超前/滞后相关结构"""
    fr = factor_returns(days, lag=0)
    a = a_returns(days).reindex(fr.index)
    rows = []
    for k in fr.columns:
        s = fr[k]
        rec = dict(key=k, name=FACTORS[k]["name"], curves=[], best_lag=None,
                   best_corr=None, best_t=None, n=None)
        for lag in range(0, max_lag + 1):
            c = _corr(s.shift(lag).reindex(a.index), a)
            rec["curves"].append(round(c[0], 4) if c else None)
            if c and (rec["best_corr"] is None or abs(c[0]) > abs(rec["best_corr"])):
                rec["best_lag"], rec["best_corr"], rec["best_t"], rec["n"] = lag, round(c[0], 4), round(c[1], 2), c[2]
        if rec["best_lag"] is not None:
            rec["verdict"] = ("同日相关最强（含交易时段重叠与前瞻偏差，不可直接交易）"
                              if rec["best_lag"] == 0 else
                              "隔夜领先（可用 t-1 预测 t，可交易）" if rec["best_lag"] == 1 else
                              f"{rec['best_lag']} 日滞后传导")
        rows.append(rec)
    rows.sort(key=lambda r: -abs(r["best_corr"] or 0))
    return dict(days=int(len(a)), max_lag=max_lag, rows=rows,
                note="lag=0 表示同日；lag=1 表示全球 t-1 对 A 股 t。相关为 Pearson，t 值按 n-2 自由度计算。")


# ================================================================ 3. 全球 Beta

def _portfolio_returns(codes=None, days=500):
    """A 股组合等权日收益（默认使用当前股票池）"""
    O, H, L, C, V, ind, mktcap = validation._wide(codes, need=max(days + 60, 400))
    if codes:
        C = C[[c for c in C.columns if c in set(codes)]]
    ret = C.pct_change()
    return ret.mean(axis=1, skipna=True).tail(days + 5), C, ret


def world_beta(codes=None, days=500, portfolio_value=1e8):
    """组合对全球因子的回归：beta / t / R² + 对冲名义金额"""
    y_all, C, stock_ret = _portfolio_returns(codes, days)
    fr = factor_returns(days, lag=1)
    d = pd.concat([y_all.rename("a"), fr], axis=1).dropna()
    if len(d) < 60:
        raise RuntimeError("全球因子与 A 股重叠样本不足 60 日，无法回归")
    y = d["a"].to_numpy(float)
    X = d[list(fr.columns)].to_numpy(float)
    X1 = np.hstack([np.ones((len(X), 1)), X])
    beta, *_ = np.linalg.lstsq(X1, y, rcond=None)
    resid = y - X1 @ beta
    dof = max(1, len(y) - X1.shape[1])
    s2 = float(resid @ resid) / dof
    cov = s2 * np.linalg.pinv(X1.T @ X1)
    se = np.sqrt(np.maximum(np.diag(cov), 1e-20))
    tvals = beta / se
    ss_tot = float(((y - y.mean()) ** 2).sum())
    r2 = 1 - float(resid @ resid) / ss_tot if ss_tot > 0 else 0.0
    rows = []
    for i, k in enumerate(fr.columns):
        rows.append(dict(
            key=k, name=FACTORS[k]["name"],
            beta=round(float(beta[i + 1]), 4),
            t=round(float(tvals[i + 1]), 2),
            sig=("显著" if abs(float(tvals[i + 1])) >= 2 else "不显著"),
            hedge_notional_yi=round(float(-beta[i + 1]) * portfolio_value / 1e8, 2),
            hedge_hint={"G_EQ": "ES/SPY 期货或 ETF", "G_USD": "UUP / 离岸人民币",
                        "G_CMD": "原油/黄金 ETF", "G_RATE": "TLT/IEF",
                        "G_CRYPTO": "IBIT/BTC", "G_CN": "恒指期货 HSI"}.get(k, "—")))
    rows.sort(key=lambda r: -abs(r["beta"]))

    # 个股层面：对 G_EQ 的 beta 分布（找风险敞口最集中的票）
    eq = fr.get("G_EQ")
    per_stock = []
    if eq is not None:
        g = eq.dropna()
        for c in stock_ret.columns[:200]:
            r = stock_ret[c].reindex(g.index)
            dd = pd.concat([r, g], axis=1).dropna()
            if len(dd) < 60 or float(dd.iloc[:, 1].var()) <= 0:
                continue
            b = float(dd.cov().iloc[0, 1] / dd.iloc[:, 1].var())
            per_stock.append((c, round(b, 3)))
        per_stock.sort(key=lambda x: -abs(x[1]))

    return dict(days=int(len(d)), universe=int(C.shape[1]), intercept=round(float(beta[0]), 6),
                r2=round(float(r2), 4), rows=rows,
                stock_beta_top=per_stock[:10],
                stock_beta_mean=round(float(np.mean([b for _, b in per_stock])), 3) if per_stock else None,
                stock_beta_std=round(float(np.std([b for _, b in per_stock])), 3) if per_stock else None,
                note=("回归式为 组合收益_t = α + Σβ_k·全球因子_k,t-1 + ε，OLS + 解析标准误。"
                      "hedge_notional_yi 为「按当前 beta 完全对冲该因子」所需的反向名义金额（1 亿组合）。"
                      "beta 只是统计敞口，不等于可交易的对冲效率（基差、汇率、交易时段差异均未计）。"))


# ================================================================ 4. 全球状态择时

def regime(days=1000, horizon=5):
    """全球股票趋势 × 美元趋势 四象限，统计 A 股未来收益"""
    fr = factor_returns(days, lag=1)
    a = a_returns(days)
    d = pd.concat([fr[["G_EQ", "G_USD"]], a.rename("a")], axis=1).dropna()
    if len(d) < 120:
        raise RuntimeError("样本不足 120 日")
    # 趋势：过去 60 日累计收益符号（因子已滞后 1 日，无未来信息）
    eq_up = d["G_EQ"].rolling(60).apply(lambda x: float((1 + x).prod() - 1), raw=True) > 0
    usd_up = d["G_USD"].rolling(60).apply(lambda x: float((1 + x).prod() - 1), raw=True) > 0
    fwd = (1 + d["a"]).cumprod().shift(-horizon) / (1 + d["a"]).cumprod() - 1
    state = pd.Series(np.where(eq_up, "risk_on", "risk_off") + "/" +
                      np.where(usd_up, "usd_up", "usd_down"), index=d.index)
    ok = fwd.notna()
    base_mean = float(fwd[ok].mean())
    rows = []
    for st, g in fwd[ok].groupby(state[ok]):
        vals = g.dropna()
        if len(vals) < 10:
            continue
        m = float(vals.mean())
        win = float((vals > 0).mean())
        rows.append(dict(state=st, n=int(len(vals)),
                         mean_fwd_pct=round(m * 100, 3),
                         win_rate=round(win * 100, 1),
                         ann_vol=round(float(vals.std() * math.sqrt(TRADING_DAYS / horizon) * 100), 2),
                         pos_coef=round(float(np.clip(m / base_mean, 0.2, 1.4)), 2) if base_mean > 0 else 1.0))
    rows.sort(key=lambda r: -r["mean_fwd_pct"])
    cur = "—"
    if len(state.dropna()):
        cur = str(state.iloc[-1])
    return dict(days=int(len(d)), horizon=horizon, current=cur,
                base_mean_pct=round(base_mean * 100, 3), rows=rows,
                note=(f"状态由「全球股票(G_EQ) 60日趋势」×「美元(G_USD) 60日趋势」构成，"
                      f"统计该状态下未来 {horizon} 日沪深300收益。pos_coef 为按历史状态均值/全样本均值"
                      "线性映射的仓位系数（0.2~1.4，仅作参考，非投资建议）。状态判定只用到 t-1 数据。"))


# ================================================================ 5. 全球因子接入 Alpha

# A 股行业 → 海外 ETF 映射（按关键词）
_SECTOR_MAP = [
    (("半导体", "电子", "芯片", "元件", "光学"), "SMH"),
    (("软件", "计算机", "通信", "互联网", "传媒", "游戏"), "IGV"),
    (("银行", "保险", "证券", "多元金融"), "XLF"),
    (("医药", "生物", "医疗"), "XLV"),
    (("电力", "公用", "燃气", "水务", "环保"), "XLU"),
    (("煤炭", "石油", "化工", "有色", "钢铁"), "XLE"),
    (("汽车", "机械", "军工", "电力设备", "新能源", "电气"), "XLI"),
    (("食品", "饮料", "家电", "纺织", "商贸", "零售", "旅游", "消费"), "XLY"),
    (("房地产", "建筑", "建材", "装修"), "XLB"),
]


def _sector_etf(ind_name):
    s = str(ind_name or "")
    for kws, etf in _SECTOR_MAP:
        if any(k in s for k in kws):
            return etf
    return "SPY"


def _rolling_beta(df, g, win=60):
    """逐列对 g 的滚动 beta（只用截止当日的数据）"""
    g = g.reindex(df.index)
    cov = df.rolling(win, min_periods=max(20, win // 2)).cov(g)
    var = g.rolling(win, min_periods=max(20, win // 2)).var()
    return cov.div(var, axis=0)


def world_alpha(codes=None, days=600, horizon=5, train_ratio=0.6):
    """原有 14 因子 vs 追加 3 个全球横截面因子：样本外 IC 对比"""
    dat = inst._panel(codes, need=max(days + 320, 800))
    C, fac, trad = dat["C"], dat["fac"], dat["trad"]
    idx = C.index[-int(days):]
    ind = dat["ind"]
    mcap = pd.Series([math.log1p(float(v)) if v else np.nan for v in dat["mktcap"]],
                     index=C.columns)
    ind_d, _ = inst._ind_dummies(ind, C.index)
    neu = inst._neutralize_many(fac, ind_d, mcap, idx)
    fwd = C.shift(-horizon) / C - 1

    split = int(len(idx) * train_ratio)
    train, test = idx[:split], idx[split:]

    # ---- 全球因子（滞后 1 日）----
    G = factor_returns(max(days + 100, 400), lag=1)
    ret = C.pct_change()
    glob = {}
    for key, name, col in [("G_EQ", "全球Beta", "gb_beta"),
                           ("G_USD", "美元敏感度", "usd_beta")]:
        if key not in G:
            continue
        try:
            b = _rolling_beta(ret, G[key])
            glob[col] = b.reindex(idx)
        except Exception:  # noqa: BLE001
            continue
    # 行业全球动量：A 股行业对应 ETF 的 20 日收益（滞后 1 日）
    try:
        etfs = sorted({_sector_etf(v) for v in ind.dropna().unique()})
        mom = {}
        for e in etfs:
            try:
                rows = wd.kline("us", e.lower())
            except Exception:  # noqa: BLE001
                continue
            s = pd.Series([r["close"] for r in rows],
                          index=pd.Index([r["day"] for r in rows]))
            s = s.sort_index()
            if s.index.has_duplicates:
                s = s[~s.index.duplicated(keep="last")]
            mom[e] = (s / s.shift(20) - 1).shift(1)
        gm = pd.DataFrame(np.nan, index=idx, columns=C.columns)
        for c in C.columns:
            e = _sector_etf(ind.get(c))
            if e in mom:
                gm[c] = mom[e].reindex(idx, method="ffill")
        if gm.notna().sum().sum() > 0:
            glob["sec_gmom"] = gm
    except Exception:  # noqa: BLE001
        pass

    glob = {k: v.loc[idx] for k, v in glob.items()}

    def _stat(fs, seg):
        s = inst._ic_fast(fs.loc[seg[:-horizon]], fwd.loc[seg[:-horizon]], trad).to_numpy(float)
        if len(s) < 20:
            return None
        ic = float(np.mean(s))
        sd = float(np.std(s, ddof=1)) if len(s) > 1 else 0.0
        return dict(ic=round(ic, 4), icir=round(ic / sd, 3) if sd > 0 else 0.0,
                    t=round(ic / sd * math.sqrt(len(s)), 2) if sd > 0 else 0.0,
                    win=round(float((s > 0).mean() * 100), 1), n=int(len(s)))

    base_rows, glob_rows = [], []
    for name, f in neu.items():
        st = _stat(f, train)
        if not st or abs(st["icir"]) < 0.05:
            continue
        base_rows.append(dict(factor=name, cn=validation._FACTORS.get(name, name), **st,
                              sign=1 if st["ic"] >= 0 else -1,
                              weight=round(st["icir"], 3)))
    for name, f in glob.items():
        st = _stat(f.fillna(0.0), train)
        if not st:
            continue
        glob_rows.append(dict(factor=name, cn={"gb_beta": "全球Beta(60日,对G_EQ)",
                                               "usd_beta": "美元敏感度(60日,对G_USD)",
                                               "sec_gmom": "行业全球动量(对应ETF 20日)"}.get(name, name),
                              **st, sign=1 if st["ic"] >= 0 else -1, weight=round(st["icir"], 3)))
    base_rows.sort(key=lambda r: -abs(r["icir"]))
    glob_rows.sort(key=lambda r: -abs(r["icir"]))

    base_pick = [r for r in base_rows if abs(r["icir"]) >= 0.15][:8]
    glob_pick = [r for r in glob_rows if abs(r["icir"]) >= 0.1]

    def _composite(picks, seg):
        z = None
        for r in picks:
            src = neu.get(r["factor"]) if r["factor"] in neu else glob.get(r["factor"])
            if src is None:
                continue
            f = inst._zscore_cs(src.loc[seg])
            term = f * (r["sign"] * abs(r["weight"]))
            z = term if z is None else z + term
        return z

    def _oos(picks):
        if not picks:
            return None
        comp = _composite(picks, idx)
        return _stat(comp.fillna(0.0), test)

    base_oos = _oos(base_pick)
    both_oos = _oos(base_pick + glob_pick)

    # 状态条件 IC：全球风险偏好上行 vs 下行
    cond = []
    try:
        g = G.get("G_EQ")
        if g is not None:
            # reindex 会把 bool 序列降级为 float 并引入 NaN，必须先 fillna 再 astype(bool)，
            # 否则 ~up 会对 float 取反抛 TypeError（曾被 except 静默吞掉）
            up = (g.rolling(60).apply(lambda x: float((1 + x).prod() - 1), raw=True) > 0)
            up = up.reindex(idx).fillna(False).astype(bool)
            comp = _composite(base_pick + glob_pick, idx).fillna(0.0)
            # fwd 用全量索引，布尔索引器必须与对象索引对齐，先切到 idx
            fwd_seg = fwd.reindex(idx)
            for lab, mask in [("全球上行", up), ("全球下行", ~up)]:
                mm = mask.astype(bool)
                s = inst._ic_fast(comp.loc[mm], fwd_seg.loc[mm], trad.reindex_like(comp.loc[mm]))
                cond.append(dict(state=lab, n=int(len(s)),
                                 ic=round(float(s.mean()), 4) if len(s) else None,
                                 win=round(float((s > 0).mean() * 100), 1) if len(s) else None))
    except Exception:  # noqa: BLE001
        pass

    imp = None
    if base_oos and both_oos:
        imp = round(both_oos["ic"] - base_oos["ic"], 4)
    return dict(
        days=int(len(idx)), train_days=int(len(train)), test_days=int(len(test)),
        horizon=horizon, universe=int(C.shape[1]),
        base_factors=base_rows, global_factors=glob_rows,
        selected_base=[r["factor"] for r in base_pick],
        selected_global=[r["factor"] for r in glob_pick],
        base_oos=base_oos, extended_oos=both_oos, improve_ic=imp,
        verdict=("全球因子有正贡献（+%.4f）" % imp if imp and imp > 0 else
                 "全球因子暂无正贡献（%.4f），已如实保留不启用" % (imp or 0)),
        regime_ic=cond,
        note=("复合 alpha = Σ sign(IC)·|ICIR|·z(因子)，训练段选因子、样本外段评估。"
              "全球Beta/美元敏感度由个股收益对滞后 1 日的全球因子做 60 日滚动回归得到；"
              "行业全球动量用 A 股行业对应的海外 ETF（半导体→SMH、软件→IGV、银行→XLF…）"
              "20 日收益，按行业广播到个股。全部滞后 1 日，无未来函数。"))


# ================================================================ 6. 融合说明矩阵

def integration_matrix():
    """全球数据在模型中的 6 个接入点及状态"""
    rows = [
        dict(layer="数据层", item="全球 10 大类 233 个标的统一接入 + 本地缓存",
             how="world_data.kline / quotes / panel",
             data="新浪美股/期货/外汇 + ECB 交叉盘 + A股指数",
             status="have", note="首次拉取后写入 SQLite，日内复用"),
        dict(layer="因子层", item="全球Beta、美元敏感度、行业全球动量 3 个新因子",
             how="world_alpha：与原有 14 因子同口径中性化 + IC/ICIR 对比",
             data="美股/全球期货/FX/行业ETF 日收益，滞后 1 日",
             status="have", note="样本外 IC 增量在接口中给出，无正贡献则如实不启用"),
        dict(layer="风险层", item="组合对 6 个全球因子的 beta / R² / 对冲名义",
             how="world_beta：OLS + 解析标准误",
             data="全球因子收益 + A 股组合收益",
             status="have", note="仅统计敞口，不含基差/汇率/时段差异"),
        dict(layer="择时层", item="全球股票×美元 四象限 → 仓位系数",
             how="regime：状态条件下未来 5 日收益统计",
             data="G_EQ / G_USD 60 日趋势 + 沪深300",
             status="partial", note="样本内统计，未做样本外滚动验证"),
        dict(layer="组合层", item="全球 beta 约束 / 跨境对冲组合",
             how="暂未写入优化器约束",
             data="需跨境交易通道与汇率对冲工具",
             status="gap", note="A 股账户无法直接做空 ES/HSI，仅能给出对冲建议"),
        dict(layer="执行层", item="跨市场时段与汇率结算",
             how="未实现",
             data="海外券商/期货通道、实时汇率",
             status="gap", note="免费源给不出可成交报价与跨境成本"),
    ]
    cnt = {"have": sum(1 for r in rows if r["status"] == "have"),
           "partial": sum(1 for r in rows if r["status"] == "partial"),
           "gap": sum(1 for r in rows if r["status"] == "gap")}
    return dict(rows=rows, counts=cnt, total=len(rows),
                conclusion=("全球数据已经进入「数据层 / 因子层 / 风险层」，择时层为部分接入；"
                            "组合约束与跨境执行仍是缺口，原因是没有跨境交易通道，不是代码问题。"))


def live_quotes(limit_per_class=4):
    """各资产类别代表性实时行情"""
    pairs = []
    for cat in wd._CAT_NAME:
        got = [(c, s) for c, s, *_ in wd.CATALOG if c == cat]
        pairs.extend(got[:limit_per_class])
    return wd.quotes(pairs)
