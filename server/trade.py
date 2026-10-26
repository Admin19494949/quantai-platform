# -*- coding: utf-8 -*-
"""买卖点研判：基于七因子评分 + 支撑压力 + ATR 波动，输出明确的买入/卖出计划

输出结构：
- 信号分区：买入区 / 持有区 / 观望区 / 卖出区
- 买入计划：现价买（近支撑）/ 回踩买（支撑区间）/ 突破买（放量过压力）
- 卖出计划：止损价、第一/第二止盈价、移动止盈规则
- 触发条件清单：当前已满足 ✓ / 未满足 ✗
- 盈亏比与仓位建议
"""
import numpy as np

from . import analysis


def _r(x, n=2):
    if x is None or (isinstance(x, float) and (np.isnan(x) or np.isinf(x))):
        return None
    return round(float(x), n)


def trade_plan(code, name, df):
    df = analysis.add_indicators(df.copy())
    r = df.iloc[-1]
    prev = df.iloc[-2]
    close = float(r["close"])
    sc = analysis.short_term_analysis(code, name, df)
    score = sc["score"]

    # 支撑压力
    low20 = float(df["low"].tail(20).min())
    high20 = float(df["high"].tail(20).max())
    support = min(low20, float(r["boll_low"])) if not np.isnan(r["boll_low"]) else low20
    resistance = max(high20, float(r["boll_up"])) if not np.isnan(r["boll_up"]) else high20
    atr = float(r["atr14"]) if not np.isnan(r["atr14"]) else close * 0.03

    # 信号分区
    dead_cross = r["dif"] < r["dea"] and prev["dif"] >= prev["dea"]
    golden_cross = r["dif"] > r["dea"] and prev["dif"] <= prev["dea"]
    above_ma20 = close > r["ma20"] if not np.isnan(r["ma20"]) else False
    below_ma10 = close < r["ma10"] if not np.isnan(r["ma10"]) else True
    rsi6 = float(r["rsi6"]) if not np.isnan(r["rsi6"]) else 50
    vol_ratio = float(r["volume"] / r["vol_ma5"]) if r["vol_ma5"] and r["vol_ma5"] > 0 else 1.0
    pct = float(r["pct"]) if r["pct"] is not None and not np.isnan(r["pct"]) else 0.0
    near_support = (close - support) / close < 0.04
    near_resistance = close >= resistance * 0.97

    if score >= 40 and above_ma20:
        zone = "买入区"
    elif score <= -35 or (below_ma10 and dead_cross):
        zone = "卖出区"
    elif score >= 15:
        zone = "持有区"
    else:
        zone = "观望区"

    # 买入触发条件清单
    buy_checks = [
        ("七因子评分 ≥ 40（当前 %+d）" % score, score >= 40),
        ("股价站上20日均线（MA20 %.2f）" % r["ma20"] if not np.isnan(r["ma20"]) else "20日均线数据不足", above_ma20),
        ("MACD 多头（DIF %.3f %s DEA %.3f）" % (r["dif"], "＞" if r["dif"] > r["dea"] else "＜", r["dea"]),
         r["dif"] > r["dea"]),
        ("RSI6 未超买（当前 %.0f，需 <80）" % rsi6, rsi6 < 80),
        ("处于支撑附近或突破确认（现价距支撑 %.1f%%）" % ((close - support) / close * 100),
         near_support or (close > resistance)),
    ]
    # 卖出触发条件清单
    sell_checks = [
        ("MACD 死叉（刚发生 %s）" % ("✓" if dead_cross else "—"), dead_cross),
        ("跌破 10 日均线（MA10 %.2f）" % r["ma10"] if not np.isnan(r["ma10"]) else "10日线数据不足", below_ma10),
        ("RSI6 超买（当前 %.0f，需 >85）" % rsi6, rsi6 > 85),
        ("触及压力区（压力 %.2f，现价 %.2f）" % (resistance, close), near_resistance),
        ("放量下跌（量比 %.2f 且今日 %+.1f%%）" % (vol_ratio, pct), vol_ratio > 1.4 and pct < 0),
    ]

    # 买入价位计划
    plans = []
    if near_support or score >= 60:
        plans.append(dict(style="激进", action="现价分批买入", price=[_r(close * 0.995), _r(close)],
                          note="七因子共振且贴近支撑，可直接分 2 批建仓"))
    plans.append(dict(style="稳健", action="回踩支撑买入", price=[_r(support), _r(support * 1.03)],
                      note=f"回踩支撑 {support:.2f} 附近缩量企稳（不破）再介入，跌破 {support*0.97:.2f} 放弃计划"))
    plans.append(dict(style="突破", action="放量突破压力买入", price=[_r(resistance * 1.005), _r(resistance * 1.02)],
                      note=f"放量（量比>1.5）站稳 {resistance:.2f} 上方可追，缩量触及勿追"))

    # 卖出价位计划
    stop = min(support * 0.98, close - 1.5 * atr)
    tp1 = resistance if resistance > close else close + 1.2 * atr
    tp2 = max(close + 2.5 * atr, tp1 * 1.06)
    # 盈亏比（按稳健买点中值计）
    buy_mid = (support * 1.015 + support) / 2
    rr = (tp1 - buy_mid) / max(buy_mid - stop, 1e-6)

    sell_plan = dict(
        stop=_r(stop), stop_pct=_r((stop / close - 1) * 100),
        tp1=_r(tp1), tp1_pct=_r((tp1 / close - 1) * 100),
        tp2=_r(tp2), tp2_pct=_r((tp2 / close - 1) * 100),
        trailing="浮盈超 10% 后改用 5 日线移动止盈：收盘跌破 MA5 无条件离场",
    )

    # 时点结论
    if zone == "买入区":
        if near_support:
            timing = f"当前即处于买点区域：现价 {close:.2f} 贴近支撑 {support:.2f}，可现价分批建仓，止损 {_r(stop)}"
        else:
            timing = f"不宜追高：距支撑已有 {((close-support)/close*100):.1f}%，等待回踩 {support:.2f}~{support*1.03:.2f} 区间再买入，或放量过 {resistance:.2f} 突破追入"
    elif zone == "持有区":
        timing = f"趋势向好但买点已过：持仓者继续持有（跌破 MA10 减仓），空仓者等回踩 {support:.2f} 附近再买"
    elif zone == "卖出区":
        timing = f"当前应卖出/减仓：现价 {close:.2f}，反弹至 {min(resistance, close*1.03):.2f} 附近逢高减仓；已跌破止损 {_r(stop)} 则清仓"
    else:
        timing = f"方向未明，观望为主：激进者可在 {support:.2f} 附近轻仓试错，确认 {resistance:.2f} 突破后再加仓"

    # 仓位建议
    if score >= 60 and zone == "买入区":
        pos = "30%~50%（多头共振可适度重仓，但单票勿超半仓）"
    elif score >= 40:
        pos = "15%~30%（首次建仓轻仓，确认趋势后加）"
    elif score >= 15:
        pos = "0%~15%（持有者不加仓，新资金观望）"
    else:
        pos = "0%（信号偏空，空仓等待）"

    return dict(
        code=code, name=name, date=str(r["day"]),
        close=_r(close), score=score, direction=sc["direction"], confidence=sc["confidence"],
        zone=zone, support=_r(support), resistance=_r(resistance), atr=_r(atr, 3),
        buy_checks=buy_checks, sell_checks=sell_checks,
        buy_plans=plans, sell_plan=sell_plan,
        reward_risk=_r(rr), position=pos, timing=timing,
        factors=sc["factors"],
    )
