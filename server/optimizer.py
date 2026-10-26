# -*- coding: utf-8 -*-
"""多策略组合优化 + 参数网格寻优（样本内外验证）

1. optimize_mix：把多个策略的净值曲线按 等权 / 逆波动率(风险平价) / 动量加权 合成，
   给出合成组合的绩效、各策略贡献与相关性。
2. grid_search：对再平衡周期、最大持仓数、仓位方案做网格搜索，
   以样本内（前 train_ratio）夏普选参，样本外（后段）验证，披露过拟合风险。

诚实声明：样本内/样本外来自同一段历史数据，样本外仍非真正的未来数据，
只能降低过拟合概率，不能保证未来有效。
"""
import itertools

import numpy as np
import pandas as pd

from . import portfolio

TRADING_DAYS = 244


def _perf(values):
    """由净值序列计算绩效（序列首值视为基准）"""
    if values is None or len(values) < 2:
        return dict(total_return=0.0, cagr=0.0, annual_volatility=0.0,
                    sharpe=0.0, max_drawdown=0.0, calmar=0.0)
    nav = pd.Series(values, dtype="float64")
    rets = nav.pct_change().dropna()
    total = (nav.iloc[-1] / nav.iloc[0] - 1) * 100
    years = max(len(nav) / TRADING_DAYS, 1e-9)
    cagr = ((nav.iloc[-1] / nav.iloc[0]) ** (1 / years) - 1) * 100
    vol = float(rets.std() * np.sqrt(TRADING_DAYS) * 100) if len(rets) > 1 else 0.0
    sharpe = cagr / vol if vol > 0 else 0.0
    dd = (nav / nav.cummax() - 1) * 100
    mdd = float(dd.min())
    calmar = cagr / abs(mdd) if mdd < 0 else 0.0
    return dict(total_return=round(float(total), 2), cagr=round(float(cagr), 2),
                annual_volatility=round(vol, 2), sharpe=round(float(sharpe), 2),
                max_drawdown=round(mdd, 2), calmar=round(float(calmar), 2))


def optimize_mix(strategies=("factor", "momentum", "tb", "mr"), days=500,
                 rebalance=20, max_holdings=10, max_industry_weight=0.35,
                 lookback=60, universe_size=40):
    """多策略组合优化：等权 / 逆波动率 / 动量加权"""
    strategies = [s for s in strategies if s]
    if len(strategies) < 2:
        return dict(error="至少需要 2 个策略才能做组合优化")

    individual, curves, errors = {}, {}, {}
    for s in strategies:
        bt = portfolio.run_portfolio_backtest(
            strategy=s, days=days, rebalance=rebalance, max_holdings=max_holdings,
            max_industry_weight=max_industry_weight, universe_size=universe_size)
        if bt.get("error"):
            errors[s] = bt["error"]
            continue
        individual[s] = dict(
            strategy_name=bt["strategy_name"],
            total_return=bt["metrics"]["total_return"],
            sharpe=bt["metrics"]["sharpe"],
            max_drawdown=bt["metrics"]["max_drawdown"],
            annual_volatility=bt["metrics"]["annual_volatility"],
            trade_count=bt["metrics"]["trade_count"],
        )
        curves[s] = pd.Series({x["date"]: x["nav"] for x in bt["nav_curve"]})
    if len(curves) < 2:
        return dict(error="可用策略不足 2 个，无法合成组合", details=errors)

    df = pd.DataFrame(curves).ffill().dropna()
    if len(df) < 60:
        return dict(error="策略净值对齐后不足 60 天")
    rets = df.pct_change().dropna()
    cols = list(df.columns)

    methods = {}
    # 1) 等权
    methods["equal"] = dict(weights={c: round(1 / len(cols) * 100, 1) for c in cols})
    # 2) 逆波动率（风险平价近似）
    inv_vol = 1.0 / rets.std()
    w_risk = inv_vol / inv_vol.sum()
    methods["risk"] = dict(weights={c: round(float(w_risk[c]) * 100, 1) for c in cols})
    # 3) 动量加权（近 lookback 日收益，仅取正收益；全为负则退化为等权）
    lb = min(max(5, lookback), len(df) - 1)
    mom = df.iloc[-1] / df.iloc[-1 - lb] - 1
    pos = mom.clip(lower=0)
    if pos.sum() <= 0:
        w_mom = pd.Series(1 / len(cols), index=cols)
    else:
        w_mom = pos / pos.sum()
        # 单策略上限 60%，超出部分留作现金，避免动量加权过度集中
        w_mom = w_mom.clip(upper=0.6)
    methods["momentum"] = dict(weights={c: round(float(w_mom[c]) * 100, 1) for c in cols})

    names = {"equal": "等权", "risk": "逆波动率(风险平价)", "momentum": f"动量加权({lb}日)"}
    for key, m in methods.items():
        w = np.array([m["weights"][c] / 100 for c in cols])
        combined = (1 + rets.values @ w).cumprod()
        nav = np.concatenate([[1.0], combined]) * portfolio.INIT_CAPITAL
        m["metrics"] = _perf(nav)
        m["cash_weight"] = round(100 - sum(m["weights"].values()), 1)
        m["nav"] = [round(float(v), 2) for v in nav]
        # 各策略对合成收益的贡献（权重 × 期间收益）
        contrib = []
        for i, c in enumerate(cols):
            r = float(df[c].iloc[-1] / df[c].iloc[0] - 1) * 100
            contrib.append(dict(strategy=c, name=individual.get(c, {}).get("strategy_name", c),
                                weight=round(float(w[i]) * 100, 1),
                                strategy_return=round(r, 2),
                                contribution=round(float(w[i]) * r, 2)))
        m["contributions"] = sorted(contrib, key=lambda x: -x["contribution"])
        m["name"] = names[key]

    corr = rets.corr().round(3)
    corr_pairs = []
    for i in range(len(cols)):
        for j in range(i + 1, len(cols)):
            corr_pairs.append(dict(a=cols[i], b=cols[j], corr=float(corr.iloc[i, j])))
    corr_pairs.sort(key=lambda x: x["corr"])

    dates = [str(d) for d in df.index]
    return dict(
        period=dict(start=dates[0], end=dates[-1], days=len(dates)),
        dates=dates,
        strategies=cols,
        individual=individual,
        errors=errors,
        methods={k: dict(name=v["name"], weights=v["weights"], metrics=v["metrics"],
                         nav=v["nav"], contributions=v["contributions"],
                         cash_weight=v.get("cash_weight", 0.0))
                 for k, v in methods.items()},
        correlations=corr_pairs,
        correlation_matrix=dict(codes=cols, matrix=corr.values.tolist()),
        disclaimer=("策略权重基于历史净值估计；逆波动率与动量加权均为启发式方法，"
                    "不是收益预测。样本外表现可能显著低于样本内。"),
    )


def grid_search(strategy="factor", days=500, train_ratio=0.7,
                rebalance_grid=(10, 20, 40), holdings_grid=(5, 10),
                weight_grid=("equal", "risk"), max_industry_weight=0.35,
                universe_size=40):
    """参数网格寻优 + 样本内外验证"""
    train_ratio = min(0.9, max(0.5, float(train_ratio)))
    combos = list(itertools.product(rebalance_grid, holdings_grid, weight_grid))
    rows = []
    for rb, mh, wt in combos:
        bt = portfolio.run_portfolio_backtest(
            strategy=strategy, days=days, rebalance=rb, max_holdings=mh,
            max_industry_weight=max_industry_weight, weighting=wt,
            universe_size=universe_size)
        if bt.get("error"):
            continue
        nav = [x["nav"] for x in bt["nav_curve"]]
        split = max(30, int(len(nav) * train_ratio))
        if len(nav) - split < 20:
            continue
        is_perf = _perf(nav[:split])
        os_perf = _perf(nav[split - 1:])   # 用切分日净值做基准，避免跨段跳空
        rows.append(dict(
            params=dict(rebalance=rb, max_holdings=mh, weighting=wt),
            in_sample=is_perf, out_sample=os_perf,
            full=_perf(nav),
            split_date=str(bt["nav_curve"][split]["date"]),
        ))
    if not rows:
        return dict(error="没有产生有效结果，请检查参数或等待行情数据就绪")

    # 以样本内夏普选参
    rows_sorted = sorted(rows, key=lambda x: -x["in_sample"]["sharpe"])
    best = rows_sorted[0]
    os_median = float(np.median([r["out_sample"]["sharpe"] for r in rows]))
    is_vals = [r["in_sample"]["sharpe"] for r in rows]
    os_vals = [r["out_sample"]["sharpe"] for r in rows]
    rank_corr = None
    if len(rows) >= 3 and len(set(is_vals)) > 1 and len(set(os_vals)) > 1:
        rank_corr = round(float(pd.Series(is_vals).corr(pd.Series(os_vals), method="spearman")), 3)

    # 过拟合警示
    warnings = []
    if best["out_sample"]["sharpe"] <= 0:
        warnings.append("样本内最优参数在样本外夏普为负，存在明显过拟合风险")
    if best["out_sample"]["max_drawdown"] < best["in_sample"]["max_drawdown"] * 1.8:
        warnings.append("样本外最大回撤显著大于样本内，参数稳健性不足")
    if rank_corr is not None and rank_corr < 0.3:
        warnings.append("样本内外夏普排名相关性低（<0.3），参数选择容易被噪声主导")

    return dict(
        strategy=strategy, days=days, train_ratio=train_ratio,
        combo_count=len(rows),
        in_sample_sharpe_median=round(float(np.median(is_vals)), 2),
        out_sample_sharpe_median=round(os_median, 2),
        rank_correlation=rank_corr,
        best=best,
        best_params=best["params"],
        rows=sorted(rows, key=lambda x: -x["in_sample"]["sharpe"]),
        warnings=warnings,
        disclaimer=("样本内/样本外来自同一段历史，样本外并非真正未来数据；"
                    "网格搜索容易过拟合，请结合样本外表现与回撤综合判断，不构成投资建议。"),
    )
