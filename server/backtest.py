# -*- coding: utf-8 -*-
"""事件驱动回测引擎

支持的策略信号（复用 strategies.py 的判定逻辑）：
- mr: 均值回归（超跌反弹）
- tb: 趋势突破
- momentum: 动量（短期涨跌评分看多）

回测假设：
- 初始资金 100 万
- 全仓进出（单只股票）
- 滑点 0.1%（买卖各扣）
- 手续费：佣金 0.025%（双向，最低5元）+ 印花税 0.05%（卖出）
"""
import datetime as dt

import numpy as np
import pandas as pd

from . import analysis, datasource as ds, strategies as st

INIT_CAPITAL = 1_000_000.0
SLIPPAGE = 0.001          # 0.1% 滑点
COMMISSION_RATE = 0.00025 # 佣金 0.025%
COMMISSION_MIN = 5.0      # 最低佣金5元
STAMP_TAX = 0.0005        # 印花税 0.05%（卖出）


def _signals_mr(df):
    """均值回归信号：命中条件日收盘买入，布林中轨或止损卖出"""
    df = analysis.add_indicators(df.copy())
    signals = pd.Series(0, index=df.index)  # 1=买入, -1=卖出
    for i in range(1, len(df)):
        r = df.iloc[i]
        hit, _ = st.hit_mean_reversion(df.iloc[: i + 1], r)
        if hit:
            signals.iloc[i] = 1  # 买入信号
        else:
            # 卖出条件：触及布林中轨 或 跌破近20日低点*0.97
            close = float(r["close"])
            boll_mid = float(r["boll_mid"]) if not np.isnan(r["boll_mid"]) else close
            low20 = float(df["low"].iloc[max(0, i - 20) : i + 1].min())
            if close >= boll_mid * 0.995 or close <= low20 * 0.97:
                signals.iloc[i] = -1
    return signals


def _signals_tb(df):
    """趋势突破信号：突破日买入，跌破突破位3%卖出"""
    df = analysis.add_indicators(df.copy())
    signals = pd.Series(0, index=df.index)
    for i in range(1, len(df)):
        r = df.iloc[i]
        hit, _ = st.hit_trend_breakout(df.iloc[: i + 1], r)
        if hit:
            signals.iloc[i] = 1
        else:
            close = float(r["close"])
            high20 = float(df["high"].iloc[max(0, i - 21) : i].max()) if i > 21 else close
            if close <= high20 * 0.97:
                signals.iloc[i] = -1
    return signals


def _signals_momentum(df):
    """动量信号：短期评分>=30买入，<=-30卖出"""
    df = analysis.add_indicators(df.copy())
    signals = pd.Series(0, index=df.index)
    for i in range(60, len(df)):
        # 用当日的7因子简化评分
        sub = df.iloc[: i + 1]
        r = sub.iloc[-1]
        close = float(r["close"])
        # 简化动量评分
        ret5 = (close / sub["close"].iloc[-6] - 1) * 100 if len(sub) > 5 else 0
        ma_ok = close > r["ma5"] > r["ma10"] if not np.isnan(r["ma10"]) else False
        macd_ok = r["dif"] > r["dea"]
        score = 0
        score += min(40, ret5 * 4)
        score += 20 if ma_ok else -20
        score += 15 if macd_ok else -15
        if score >= 30:
            signals.iloc[i] = 1
        elif score <= -30:
            signals.iloc[i] = -1
    return signals


SIGNAL_FUNCS = {
    "mr": _signals_mr,
    "tb": _signals_tb,
    "momentum": _signals_momentum,
}

STRATEGY_NAMES = {
    "mr": "均值回归（超跌反弹）",
    "tb": "趋势突破",
    "momentum": "动量跟踪",
}


def _buy_cost(amount, price):
    """买入成本（含滑点+佣金）"""
    slip_price = price * (1 + SLIPPAGE)
    shares = int(amount / slip_price / 100) * 100  # 整手
    if shares <= 0:
        return 0, 0, 0
    cost = shares * slip_price
    commission = max(cost * COMMISSION_RATE, COMMISSION_MIN)
    total = cost + commission
    return shares, slip_price, total


def _sell_proceeds(shares, price):
    """卖出收入（扣滑点+佣金+印花税）"""
    slip_price = price * (1 - SLIPPAGE)
    revenue = shares * slip_price
    commission = max(revenue * COMMISSION_RATE, COMMISSION_MIN)
    stamp = revenue * STAMP_TAX
    net = revenue - commission - stamp
    return slip_price, net


def run_backtest(strategy, code, days=500):
    """执行回测，返回绩效指标 + 交易明细 + 净值曲线"""
    code = ds.norm_code(code)
    name = ds.get_stock_name(code)
    rows = ds.get_kline(code)
    df = analysis.build_df(rows)
    if len(df) < 60:
        return dict(error="历史数据不足，至少需要60个交易日")
    df = df.tail(days).reset_index(drop=True)

    sig_func = SIGNAL_FUNCS.get(strategy)
    if not sig_func:
        return dict(error=f"未知策略: {strategy}")
    signals = sig_func(df)

    capital = INIT_CAPITAL
    shares = 0
    entry_price = 0.0
    trades = []
    nav_curve = []  # 每日净值
    hold_days = 0

    for i in range(len(df)):
        r = df.iloc[i]
        close = float(r["close"])
        sig = int(signals.iloc[i])

        # 持仓中
        if shares > 0:
            hold_days += 1
            if sig == -1:
                # 卖出
                sell_price, net = _sell_proceeds(shares, close)
                pnl = net - (shares * entry_price)
                pnl_pct = (sell_price / entry_price - 1) * 100
                trades.append(dict(
                    buy_date=str(df["day"].iloc[trades[-1]["buy_idx"]]) if trades else "",
                    sell_date=str(r["day"]),
                    buy_price=round(entry_price, 2),
                    sell_price=round(sell_price, 2),
                    shares=shares,
                    pnl=round(pnl, 2),
                    pnl_pct=round(pnl_pct, 2),
                    hold_days=hold_days,
                ))
                capital = net
                shares = 0
                hold_days = 0
        # 空仓中
        elif sig == 1 and capital > 0:
            s, buy_price, total = _buy_cost(capital, close)
            if s > 0:
                shares = s
                entry_price = buy_price
                capital -= total
                trades.append(dict(buy_idx=i, buy_date=str(r["day"]), buy_price=round(buy_price, 2)))

        # 当日净值
        nav = capital + shares * close
        nav_curve.append(dict(date=str(r["day"]), nav=round(nav, 2), price=round(close, 2)))

    # 最后一天如果还持仓，按收盘价计算净值（不实际卖出）
    final_nav = capital + shares * float(df["close"].iloc[-1])

    return _calc_metrics(trades, nav_curve, final_nav, df, strategy, code, name)


def _calc_metrics(trades, nav_curve, final_nav, df, strategy, code, name):
    """计算绩效指标"""
    closed_trades = [t for t in trades if "sell_date" in t]
    total_return = (final_nav / INIT_CAPITAL - 1) * 100

    nav_series = pd.Series([x["nav"] for x in nav_curve])
    daily_ret = nav_series.pct_change().dropna()

    n_days = len(nav_curve)
    years = n_days / 244.0
    cagr = ((final_nav / INIT_CAPITAL) ** (1 / years) - 1) * 100 if years > 0 else 0
    annual_vol = float(daily_ret.std() * np.sqrt(244) * 100) if len(daily_ret) > 1 else 0
    sharpe = (cagr / annual_vol) if annual_vol > 0 else 0
    downside = daily_ret[daily_ret < 0]
    sortino = (cagr / (downside.std() * np.sqrt(244) * 100)) if len(downside) > 1 and downside.std() > 0 else 0

    # 最大回撤
    cummax = nav_series.cummax()
    dd = (nav_series / cummax - 1) * 100
    max_dd = float(dd.min())
    max_dd_idx = int(dd.idxmin()) if len(dd) else 0
    # 回撤持续期
    dd_series = dd < 0
    mdd_duration = 0
    cur = 0
    for v in dd_series:
        if v:
            cur += 1
            mdd_duration = max(mdd_duration, cur)
        else:
            cur = 0

    calmar = cagr / abs(max_dd) if max_dd < 0 else 0

    # 交易统计
    wins = [t for t in closed_trades if t["pnl"] > 0]
    losses = [t for t in closed_trades if t["pnl"] <= 0]
    win_rate = len(wins) / len(closed_trades) * 100 if closed_trades else 0
    avg_win = np.mean([t["pnl_pct"] for t in wins]) if wins else 0
    avg_loss = abs(np.mean([t["pnl_pct"] for t in losses])) if losses else 0
    profit_factor = (sum(t["pnl"] for t in wins) / abs(sum(t["pnl"] for t in losses))) if losses and sum(t["pnl"] for t in losses) != 0 else float("inf")
    avg_hold = np.mean([t["hold_days"] for t in closed_trades]) if closed_trades else 0

    # 基准收益（买入持有）
    bench_return = (float(df["close"].iloc[-1]) / float(df["close"].iloc[0]) - 1) * 100

    return dict(
        code=code, name=name, strategy=strategy, strategy_name=STRATEGY_NAMES.get(strategy, strategy),
        period=dict(start=str(df["day"].iloc[0]), end=str(df["day"].iloc[-1]), days=n_days),
        metrics=dict(
            total_return=round(total_return, 2),
            cagr=round(cagr, 2),
            annual_volatility=round(annual_vol, 2),
            sharpe=round(sharpe, 2),
            sortino=round(sortino, 2),
            calmar=round(calmar, 2),
            max_drawdown=round(max_dd, 2),
            max_dd_duration_days=mdd_duration,
            win_rate=round(win_rate, 1),
            profit_factor=round(profit_factor, 2) if profit_factor != float("inf") else None,
            avg_win_pct=round(avg_win, 2),
            avg_loss_pct=round(avg_loss, 2),
            avg_hold_days=round(avg_hold, 1),
            trade_count=len(closed_trades),
            initial_capital=INIT_CAPITAL,
            final_nav=round(final_nav, 2),
            benchmark_return=round(bench_return, 2),
            excess_return=round(total_return - bench_return, 2),
        ),
        trades=closed_trades[-20:],  # 最近20笔交易
        nav_curve=nav_curve,
        params=dict(slippage=SLIPPAGE, commission=COMMISSION_RATE, stamp_tax=STAMP_TAX, min_commission=COMMISSION_MIN),
        disclaimer="回测基于历史数据，不构成投资建议。滑点手续费为A股标准费率近似。",
    )
