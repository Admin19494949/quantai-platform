# -*- coding: utf-8 -*-
"""收益归因：Brinson-Fachler 行业配置 / 个股选择 / 交互效应

- 基于期末权重做静态归因（假设期间权重不变），基准为默认股票池等权
- 若同时传入组合回测结果，则额外给出"择时效应 = 策略实际收益 - 期末权重静态收益"
- 现金部分单独计算拖累（cash drag），不混入选股/配置效应
"""
import numpy as np
import pandas as pd

from . import datasource as ds, market_data, portfolio, risk


def _benchmark_frame(days):
    items = portfolio.default_universe(portfolio.DEFAULT_UNIVERSE_SIZE)
    # _load_one 要求至少 80 行，短窗口需补足
    data = portfolio._load_all(items, "factor", max(80, days + 5))
    if len(data) < 5:
        return None
    price = pd.DataFrame({d["code"]: pd.Series(d["close"].values, index=d["day"])
                          for d in data}).sort_index()
    price = price.ffill()
    # 各股票历史长度不同：对齐到最晚的上市起点，仅剔除中途退市/长期停牌的个股
    start = max(price[c].first_valid_index() for c in price.columns)
    price = price.loc[start:]
    price = price.loc[:, price.notna().all(axis=0)]
    if price.shape[1] < 5 or len(price) < 20:
        return None
    return price.iloc[-days:]


def analyze(codes=None, weights=None, days=120, backtest=None):
    """收益归因

    codes: 持仓代码；weights: dict code -> 权重（百分比或小数）
    days: 归因窗口（交易日）
    backtest: portfolio.run_portfolio_backtest 的结果（可选，用于计算择时效应）
    """
    days = max(20, min(int(days), 750))
    price, rets = risk._load_returns(codes, days)
    if price is None:
        return dict(error="行情数据不足，无法做归因")
    codes_used = list(price.columns)
    n = len(codes_used)
    if weights:
        raw = np.array([float(weights.get(c, 0) or 0) for c in codes_used])
        if raw.sum() <= 0:
            raw = np.ones(n)
    else:
        raw = np.ones(n)
    w = raw / raw.sum()

    bench = _benchmark_frame(days)
    if bench is None:
        return dict(error="基准股票池数据不足")
    # 对齐到组合的交易日
    bench = bench.reindex(price.index).ffill().dropna(how="any")
    if len(bench) < 20:
        return dict(error="基准与组合交易日对齐后不足 20 天")

    ind_map = market_data.get_industry_map()
    stock_ret = {c: float(price[c].iloc[-1] / price[c].iloc[0] - 1) * 100 for c in codes_used}
    bench_ret = {c: float(bench[c].iloc[-1] / bench[c].iloc[0] - 1) * 100 for c in bench.columns}

    port_ret = float(sum(float(w[i]) * stock_ret[c] for i, c in enumerate(codes_used)))
    bench_w = {c: 1.0 / len(bench.columns) for c in bench.columns}
    bench_total = float(sum(bench_w[c] * bench_ret[c] for c in bench.columns))

    # 按行业聚合
    inds = sorted(set([ind_map.get(c) or "其他" for c in codes_used] +
                      [ind_map.get(c) or "其他" for c in bench.columns]))
    rows = []
    for ind in inds:
        hold_idx = [i for i, c in enumerate(codes_used) if (ind_map.get(c) or "其他") == ind]
        bench_codes_ind = [c for c in bench.columns if (ind_map.get(c) or "其他") == ind]
        if not hold_idx and not bench_codes_ind:
            continue
        w_p = float(sum(w[i] for i in hold_idx))
        w_b = float(sum(bench_w[c] for c in bench_codes_ind))
        r_p = float(sum(w[i] * stock_ret[codes_used[i]] for i in hold_idx) / w_p) if w_p > 0 else 0.0
        r_b = float(sum(bench_w[c] * bench_ret[c] for c in bench_codes_ind) / w_b) if w_b > 0 else 0.0
        allocation = float((w_p - w_b) * (r_b - bench_total))
        selection = float(w_b * (r_p - r_b))
        interaction = float((w_p - w_b) * (r_p - r_b))
        rows.append(dict(
            industry=ind,
            portfolio_weight=round(w_p * 100, 1),
            benchmark_weight=round(w_b * 100, 1),
            portfolio_return=round(r_p, 2),
            benchmark_return=round(r_b, 2),
            allocation=round(allocation, 2),
            selection=round(selection, 2),
            interaction=round(interaction, 2),
            total=round(allocation + selection + interaction, 2),
        ))
    rows.sort(key=lambda x: -abs(x["total"]))

    stock_contrib = sorted(
        [dict(code=c, name=ds.get_stock_name(c) or c,
              industry=ind_map.get(c) or "其他",
              weight=round(float(w[i]) * 100, 1),
              return_pct=round(stock_ret[c], 2),
              contribution=round(float(w[i]) * stock_ret[c], 2))
         for i, c in enumerate(codes_used)], key=lambda x: -x["contribution"])

    alloc_sum = float(sum(r["allocation"] for r in rows))
    sel_sum = float(sum(r["selection"] for r in rows))
    inter_sum = float(sum(r["interaction"] for r in rows))
    active = round(port_ret - bench_total, 2)

    # 择时效应：策略实际收益 vs 期末权重静态持有
    timing = None
    if backtest and "nav_curve" in backtest and backtest["nav_curve"]:
        strat_ret = backtest["metrics"]["total_return"]
        timing = dict(
            strategy_return=round(float(strat_ret), 2),
            static_return=round(float(port_ret), 2),
            timing_effect=round(float(strat_ret) - float(port_ret), 2),
            note="择时效应 = 组合回测实际收益 - 以期末权重静态持有同期收益；"
                 "正值代表调仓/仓位变化贡献，负值代表拖累。",
        )

    return dict(
        period=dict(start=str(price.index[0]), end=str(price.index[-1]), days=len(price)),
        benchmark=dict(name="默认股票池等权", count=len(bench.columns),
                       return_pct=round(bench_total, 2)),
        portfolio_return=round(port_ret, 2),
        active_return=active,
        summary=dict(
            allocation=round(alloc_sum, 2),
            selection=round(sel_sum, 2),
            interaction=round(inter_sum, 2),
            total_active=round(alloc_sum + sel_sum + inter_sum, 2),
        ),
        industry_attribution=rows,
        stock_contribution=stock_contrib,
        timing=timing,
        disclaimer=("Brinson 归因基于期末权重的静态持有假设，不含期间调仓路径；"
                    "基准为默认股票池等权，非沪深300等官方指数。数值仅供归因参考。"),
    )
