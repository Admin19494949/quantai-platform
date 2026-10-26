# -*- coding: utf-8 -*-
"""多股票组合回测：行业约束 + 仓位管理 + 周期再平衡

与单股事件驱动回测（backtest.py）的区别：
- 同时持有多只股票（默认最多 10 只），按目标权重分配资金
- 每次再平衡从候选池重新选股，受行业权重上限约束（默认单一行业 ≤ 35%）
- 支持等权 / 评分加权 / 风险平价（逆波动率）三种仓位方案
- 记录每日净值、持仓数量、行业暴露、换手率与交易成本

策略：
- mr       均值回归（超跌反弹），信号来自 strategies.hit_mean_reversion
- tb       趋势突破，信号来自 strategies.hit_trend_breakout
- momentum 动量跟踪，复用 backtest.SIGNAL_FUNCS
- factor   多因子打分（质量/成长/价值/动量），全市场分位来自 market_data

默认股票池从全市场财务快照中按行业分散抽样（每行业取质量+成长最高的 2 只），
确保行业约束有实际意义，而非集中在少数板块。
"""
import concurrent.futures as cf
import threading
import time

import numpy as np
import pandas as pd

from . import analysis, backtest as bt, datasource as ds, market_data

INIT_CAPITAL = bt.INIT_CAPITAL
SLIPPAGE = bt.SLIPPAGE
COMMISSION_RATE = bt.COMMISSION_RATE
COMMISSION_MIN = bt.COMMISSION_MIN
STAMP_TAX = bt.STAMP_TAX

DEFAULT_UNIVERSE_SIZE = 40
WARMUP = 130               # 指标预热交易日
CACHE_TTL = 600

_cache = {}
_load_cache = {}
_cache_lock = threading.Lock()


# ---------------------------------------------------------------- 股票池

def _is_a_share(code):
    if not code or len(code) != 6 or not code.isdigit():
        return False
    if code.startswith("900") or code.startswith("200"):
        return False
    return code[0] in "03684" or code.startswith("92")


def default_universe(size=DEFAULT_UNIVERSE_SIZE):
    """按行业分散抽样：每个行业取 ROE+净利增速综合最高的 2 只

    筛选条件（避免选中微盘/一次性收益标的，保证回测可交易性）：
    - 市值 ≥ 50 亿（stocks.mktcap 单位为万元）
    - PE 在 (0, 100) 区间（盈利且非极端高估）
    - ROE 在 (0, 50)%、净利增速在 (-50, 100)% 区间
    """
    snaps = market_data.get_all_snapshots()
    ind_map = market_data.get_industry_map()
    if not snaps or not ind_map:
        return []
    try:
        vrows = ds._db().execute("SELECT code,pe,mktcap FROM stocks").fetchall()
        vals = {r[0]: (r[1], r[2]) for r in vrows}
    except Exception:  # noqa: BLE001
        vals = {}
    rows = []
    for s in snaps:
        code = s.get("code")
        if not _is_a_share(code) or code not in ind_map:
            continue
        pe, mktcap = vals.get(code, (None, None))
        if not mktcap or mktcap < 500_000 or not pe or pe <= 0 or pe > 100:
            continue
        roe, pg = s.get("roe"), s.get("profit_growth")
        if roe is None or pg is None:
            continue
        if not (0 < roe < 50) or not (-50 < pg < 100):
            continue
        score = roe * 0.6 + pg * 0.4
        rows.append((score, code, s.get("name") or "", ind_map[code]))
    by_ind = {}
    for r in rows:
        by_ind.setdefault(r[3], []).append(r)
    picked = []
    for lst in by_ind.values():
        lst.sort(reverse=True)
        picked.extend(lst[:2])
    picked.sort(reverse=True)
    return [dict(code=c, name=n, industry=i) for _, c, n, i in picked[:size]]


def _resolve_universe(codes):
    """codes 为空时使用默认行业分散池"""
    if not codes:
        return default_universe()
    ind_map = market_data.get_industry_map()
    out = []
    for c in codes:
        code = ds.norm_code(c)
        if not _is_a_share(code):
            continue
        out.append(dict(code=code, name=ds.get_stock_name(code) or code,
                        industry=ind_map.get(code) or "其他"))
    return out


# ---------------------------------------------------------------- 行情 / 信号

def _load_one(item, strategy, need):
    """加载单只股票并计算信号（在预热窗口上算指标，再截取回测区间）"""
    code = item["code"]
    try:
        rows = ds.get_kline(code)
        if not rows or len(rows) < 80:
            return None
        df = analysis.build_df(rows).tail(need).reset_index(drop=True)
        if len(df) < 80:
            return None
        if strategy in bt.SIGNAL_FUNCS:
            sig = bt.SIGNAL_FUNCS[strategy](df)
        else:                      # factor 策略由打分选股，不需要技术信号
            sig = pd.Series(0, index=df.index)
        return dict(
            code=code, name=item["name"], industry=item["industry"],
            day=df["day"].astype(str), close=df["close"].astype(float),
            signal=sig.astype(int),
        )
    except Exception:  # noqa: BLE001
        return None


def _load_all(items, strategy, need, workers=8):
    key = (tuple(sorted(x["code"] for x in items)), strategy, int(need))
    now = time.time()
    with _cache_lock:
        hit = _load_cache.get(key)
        if hit and now - hit["ts"] < CACHE_TTL:
            return hit["data"]
    out = []
    with cf.ThreadPoolExecutor(max_workers=workers) as ex:
        futs = [ex.submit(_load_one, it, strategy, need) for it in items]
        for f in cf.as_completed(futs):
            r = f.result()
            if r:
                out.append(r)
    # 网络抖动导致失败的标的串行补拉一次
    got = {d["code"] for d in out}
    for it in [x for x in items if x["code"] not in got]:
        r = _load_one(it, strategy, need)
        if r:
            out.append(r)
    if out:
        with _cache_lock:
            _load_cache[key] = dict(ts=now, data=out)
            if len(_load_cache) > 16:
                oldest = min(_load_cache, key=lambda k: _load_cache[k]["ts"])
                _load_cache.pop(oldest, None)
    return out


# ---------------------------------------------------------------- 估值 / 因子打分

def _valuation_map(codes):
    """从 stocks 表取 PE/PB（用于价值因子打分）"""
    if not codes:
        return {}
    try:
        q = ",".join("?" * len(codes))
        rows = ds._db().execute(
            f"SELECT code,pe,pb FROM stocks WHERE code IN ({q})", list(codes)).fetchall()
        return {r[0]: dict(pe=r[1], pb=r[2]) for r in rows}
    except Exception:  # noqa: BLE001
        return {}


def _pct(sorted_arr, v):
    return market_data.percentile_rank(sorted_arr, v)


def _factor_score(code, ret60, dist, snaps, vals):
    """多因子打分（0-100）：质量30% + 成长20% + 价值20% + 动量30%"""
    s = snaps.get(code) or {}
    roe = s.get("roe")
    pg = s.get("profit_growth")
    pe = (vals.get(code) or {}).get("pe")
    parts = []
    q = _pct(dist.get("roe") or [], roe)
    if q is not None:
        parts.append(("quality", q, 0.30))
    g = _pct(dist.get("profit_growth") or [], pg)
    if g is not None:
        parts.append(("growth", g, 0.20))
    if pe and pe > 0:
        ep = 1.0 / float(pe)
        v = _pct(dist.get("ep") or [], ep) if dist.get("ep") else None
        if v is not None:
            parts.append(("value", v, 0.20))
    if ret60 is not None:
        m = _pct(dist.get("ret60") or [], ret60)
        if m is not None:
            parts.append(("momentum", m, 0.30))
    if not parts:
        return None
    wsum = sum(w for _, _, w in parts)
    return round(sum(v * w for _, v, w in parts) / wsum, 2)


def _build_dist(data, snaps, vals):
    """本次回测窗口内的横截面分布（动量/价值），叠加全市场财务分布"""
    dist = market_data.get_distributions()
    ret60 = []
    for d in data:
        c = d["close"]
        if len(c) > 61:
            ret60.append(float(c.iloc[-1] / c.iloc[-61] - 1) * 100)
    # 动量分布用候选池横截面（回测内可比），不用全市场
    dist = dict(dist)
    dist["ret60"] = sorted(ret60)
    eps = []
    for code in {d["code"] for d in data}:
        pe = (vals.get(code) or {}).get("pe")
        if pe and pe > 0:
            eps.append(1.0 / float(pe))
    dist["ep"] = sorted(eps)
    return dist


# ---------------------------------------------------------------- 当前时点选股

def rank_now(strategy="factor", max_holdings=10, max_industry_weight=0.35,
             universe_size=DEFAULT_UNIVERSE_SIZE):
    """按最新交易日数据给出目标组合（供实盘信号/盯盘使用，不做历史回测）

    返回 dict(targets=[...], scored=[...], date=..., universe_count=...)
    """
    if strategy not in list(bt.SIGNAL_FUNCS) + ["factor"]:
        return dict(error=f"未知策略: {strategy}")
    items = default_universe(max(10, min(80, int(universe_size))))
    if len(items) < 2:
        return dict(error="默认股票池为空，请等待财务/行业数据同步")
    data = _load_all(items, strategy, 260)
    if len(data) < 2:
        return dict(error="行情数据不足，请稍后重试")

    snaps = {s["code"]: s for s in market_data.get_all_snapshots()}
    vals = _valuation_map([d["code"] for d in data])
    dist = _build_dist(data, snaps, vals)

    scored = []
    for d in data:
        close = d["close"]
        if len(close) < 61:
            continue
        ret20 = float(close.iloc[-1] / close.iloc[-21] - 1) * 100
        ret60 = float(close.iloc[-1] / close.iloc[-61] - 1) * 100
        sig = int(d["signal"].iloc[-1])
        if strategy == "factor":
            s = _factor_score(d["code"], ret60, dist, snaps, vals)
            if s is None:
                continue
        else:
            if sig != 1:
                continue
            s = round(ret20, 2)
        scored.append(dict(code=d["code"], name=d["name"], industry=d["industry"],
                           score=s, signal=sig, price=round(float(close.iloc[-1]), 2),
                           ret20=round(ret20, 2), ret60=round(ret60, 2),
                           date=str(d["day"].iloc[-1])))
    scored.sort(key=lambda x: -x["score"])

    selected, ind_w = [], {}
    for s in scored:
        if len(selected) >= max_holdings:
            break
        w = 1.0 / max_holdings
        ind = s["industry"] or "其他"
        if ind_w.get(ind, 0.0) + w > max_industry_weight + 1e-9:
            continue
        selected.append(dict(s, target_weight=round(w * 100, 1)))
        ind_w[ind] = ind_w.get(ind, 0.0) + w
    return dict(date=scored[0]["date"] if scored else None,
                strategy=strategy, universe_count=len(data),
                holdings_count=len(selected), targets=selected,
                scored=scored[:30], industry_count=len(ind_w))


# ---------------------------------------------------------------- 交易成本

def _buy(budget, price):
    """按预算买入，返回 (股数, 成交价, 总支出)"""
    slip = price * (1 + SLIPPAGE)
    shares = int(budget / (slip * (1 + COMMISSION_RATE)) / 100) * 100
    if shares <= 0:
        return 0, 0.0, 0.0
    cost = shares * slip
    fee = max(cost * COMMISSION_RATE, COMMISSION_MIN)
    return shares, slip, cost + fee


def _sell(shares, price):
    """卖出，返回 (成交价, 净收入, 费用)"""
    slip = price * (1 - SLIPPAGE)
    gross = shares * slip
    fee = max(gross * COMMISSION_RATE, COMMISSION_MIN) + gross * STAMP_TAX
    return slip, gross - fee, fee


# ---------------------------------------------------------------- 主流程

def run_portfolio_backtest(codes=None, strategy="momentum", days=500,
                           rebalance=20, max_holdings=10,
                           max_industry_weight=0.35, weighting="equal",
                           stop_loss=None, universe_size=DEFAULT_UNIVERSE_SIZE):
    """组合回测入口

    codes: 指定股票代码列表；为空则使用默认行业分散池
    strategy: mr / tb / momentum / factor
    rebalance: 再平衡周期（交易日）
    max_industry_weight: 单一行业市值占比上限（0-1）
    weighting: equal 等权 / score 评分加权 / risk 逆波动率
    stop_loss: 单票止损比例（如 0.1 表示 -10% 止损），None 关闭
    """
    if strategy not in list(bt.SIGNAL_FUNCS) + ["factor"]:
        return dict(error=f"未知策略: {strategy}")
    days = max(60, min(int(days), 1500))
    rebalance = max(5, min(int(rebalance), 120))
    max_holdings = max(1, min(int(max_holdings), 30))
    max_industry_weight = min(1.0, max(0.05, float(max_industry_weight)))
    if weighting not in ("equal", "score", "risk"):
        weighting = "equal"

    items = _resolve_universe(codes)
    if len(items) < 2:
        return dict(error="股票池不足（至少需要2只），请检查代码或等待财务/行业数据同步")

    key = (tuple(sorted(i["code"] for i in items)), strategy, days, rebalance,
           max_holdings, round(max_industry_weight, 3), weighting,
           round(stop_loss, 3) if stop_loss else None)
    now = time.time()
    with _cache_lock:
        hit = _cache.get(key)
        if hit and now - hit["ts"] < CACHE_TTL:
            return hit["data"]

    need = days + WARMUP
    data = _load_all(items, strategy, need)
    if len(data) < 2:
        return dict(error="可用行情数据不足，请稍后重试（股票列表可能仍在后台加载）")

    # 对齐交易日：以覆盖最广的交易日为日历，前向填充缺失
    price = pd.DataFrame({d["code"]: pd.Series(d["close"].values, index=d["day"])
                          for d in data}).sort_index()
    coverage = price.notna().sum(axis=1)
    calendar = price.index[coverage >= max(2, int(len(data) * 0.6))]
    calendar = calendar[-days:]
    if len(calendar) < 60:
        return dict(error="对齐后的交易日不足 60 天，请减少窗口或调整股票池")
    price = price.reindex(calendar).ffill()
    price = price.dropna(axis=1, how="any")   # 剔除窗口内无数据的标的
    if price.shape[1] < 2:
        return dict(error="窗口内有效标的不足")

    meta = {d["code"]: d for d in data}
    names = {d["code"]: d["name"] for d in data}
    inds = {d["code"]: d["industry"] for d in data}

    sig = pd.DataFrame({d["code"]: pd.Series(d["signal"].values, index=d["day"])
                        for d in data}).reindex(calendar).fillna(0).astype(int)

    snaps = {s["code"]: s for s in market_data.get_all_snapshots()}
    vals = _valuation_map(list(price.columns))
    dist = _build_dist(data, snaps, vals)

    # 60 日已实现波动率（用于风险平价与止损参考）
    rets = price.pct_change()
    vol60 = rets.rolling(60).std() * np.sqrt(244)

    cash = INIT_CAPITAL
    positions = {}     # code -> dict(shares, cost, entry_price, entry_date)
    nav_curve = []
    trades = []
    total_cost = 0.0
    turnover_amount = 0.0
    holdings_hist = []
    rebal_count = 0

    def sig_at(code, day):
        try:
            return int(sig.at[day, code])
        except Exception:  # noqa: BLE001
            return 0

    for i, day in enumerate(calendar):
        px = price.loc[day]

        # ---- 每日：止损检查
        if stop_loss:
            for code in list(positions.keys()):
                p = positions[code]
                cur = px.get(code)
                if cur is None or np.isnan(cur) or p["entry_price"] <= 0:
                    continue
                if cur / p["entry_price"] - 1 <= -abs(stop_loss):
                    sp, net, fee = _sell(p["shares"], float(cur))
                    pnl = net - p["cost"]
                    trades.append(dict(date=day, code=code, name=names.get(code, code),
                                       side="sell", reason="止损", price=round(sp, 2),
                                       shares=p["shares"], amount=round(net, 2),
                                       pnl=round(pnl, 2),
                                       pnl_pct=round((sp / p["entry_price"] - 1) * 100, 2)))
                    cash += net
                    total_cost += fee
                    turnover_amount += p["shares"] * sp
                    del positions[code]

        # ---- 再平衡
        is_rebal = i % rebalance == 0
        if is_rebal:
            rebal_count += 1
            scores = {}
            if strategy == "factor":
                for code in price.columns:
                    c = price[code].iloc[: i + 1]
                    ret60 = float(c.iloc[-1] / c.iloc[-61] - 1) * 100 if len(c) > 61 else None
                    s = _factor_score(code, ret60, dist, snaps, vals)
                    if s is not None:
                        scores[code] = s
                ranked = sorted(scores, key=lambda x: scores[x], reverse=True)
            else:
                cands = [c for c in price.columns if sig_at(c, day) == 1]
                if not cands:
                    # 无买入信号时不强制清仓，仅维持现有持仓
                    ranked = [c for c in positions if c in price.columns]
                else:
                    def _mom(c):
                        s = price[c].iloc[: i + 1]
                        n = min(21, len(s) - 1)
                        return float(s.iloc[-1] / s.iloc[-1 - n] - 1) * 100 if n > 0 else 0.0
                    ranked = sorted(cands, key=_mom, reverse=True)
                    for c in ranked:
                        scores[c] = round(_mom(c), 2)

            # 保留缓冲：现有持仓排名在前 max_holdings*1.5 内可继续持有，降低换手
            keep_buffer = int(max_holdings * 1.5)
            rank_pos = {c: k for k, c in enumerate(ranked)}
            selected = []
            ind_w = {}
            # 现有持仓优先（若仍在候选高位）
            ordered = [c for c in ranked if c in positions]
            ordered += [c for c in ranked if c not in positions]
            for code in ordered:
                if len(selected) >= max_holdings:
                    break
                if code in positions and rank_pos.get(code, 10 ** 9) >= keep_buffer:
                    continue
                ind = inds.get(code) or "其他"
                w = 1.0 / max_holdings
                if ind_w.get(ind, 0.0) + w > max_industry_weight + 1e-9:
                    continue
                selected.append(code)
                ind_w[ind] = ind_w.get(ind, 0.0) + w

            # 卖出不在目标中的持仓（或出现卖出信号）
            for code in list(positions.keys()):
                if code in selected and sig_at(code, day) != -1:
                    continue
                p = positions[code]
                cur = px.get(code)
                if cur is None or np.isnan(cur) or cur <= 0:
                    continue
                sp, net, fee = _sell(p["shares"], float(cur))
                pnl = net - p["cost"]
                trades.append(dict(date=day, code=code, name=names.get(code, code),
                                   side="sell", reason="调仓" if code not in selected else "信号",
                                   price=round(sp, 2), shares=p["shares"],
                                   amount=round(net, 2), pnl=round(pnl, 2),
                                   pnl_pct=round((sp / p["entry_price"] - 1) * 100, 2)))
                cash += net
                total_cost += fee
                turnover_amount += p["shares"] * sp
                del positions[code]

            # 计算目标权重
            nav_now = cash + sum(p["shares"] * float(px.get(c, p["entry_price"]))
                                 for c, p in positions.items())
            if selected:
                # 目标总仓位 = 实际选中数 / 最大持仓数，候选不足时保留现金，避免过度集中
                total_target = min(1.0, len(selected) / float(max_holdings))
                if weighting == "score" and scores:
                    raw = np.array([max(scores.get(c, 1.0), 1.0) for c in selected])
                elif weighting == "risk":
                    raw = np.array([1.0 / max(float(vol60.at[day, c]) if not np.isnan(vol60.at[day, c]) else 0.4, 0.05)
                                    for c in selected])
                else:
                    raw = np.ones(len(selected))
                raw = raw / raw.sum() * total_target
                cap = 1.0 / max_holdings
                raw = np.minimum(raw, cap)
                raw = raw / raw.sum() * total_target   # 截断后重新归一到目标总仓位
                raw = np.minimum(raw, cap)
                target_w = dict(zip(selected, raw))
            else:
                target_w = {}

            # 买入 / 调仓到目标权重
            equity_for_pos = nav_now * 0.98   # 预留 2% 现金缓冲
            for code, w in target_w.items():
                cur_px = px.get(code)
                if cur_px is None or np.isnan(cur_px) or cur_px <= 0:
                    continue
                target_val = equity_for_pos * float(w)
                held_val = positions[code]["shares"] * float(cur_px) if code in positions else 0.0
                diff = target_val - held_val
                if diff <= nav_now * 0.02:      # 偏离不足 2% 不交易
                    continue
                if diff > 0:
                    budget = min(diff, cash)
                    shares, bp, total = _buy(budget, float(cur_px))
                    if shares <= 0:
                        continue
                    cash -= total
                    total_cost += total - shares * bp
                    turnover_amount += shares * bp
                    if code in positions:
                        p = positions[code]
                        p["cost"] += total
                        p["shares"] += shares
                        p["entry_price"] = (p["entry_price"] * (p["shares"] - shares) + bp * shares) / p["shares"]
                    else:
                        positions[code] = dict(shares=shares, cost=total, entry_price=bp,
                                               entry_date=day, industry=inds.get(code) or "其他")
                    trades.append(dict(date=day, code=code, name=names.get(code, code),
                                       side="buy", reason="建仓" if held_val == 0 else "加仓",
                                       price=round(bp, 2), shares=shares,
                                       amount=round(total, 2), pnl=None, pnl_pct=None))
                else:
                    p = positions[code]
                    sell_n = min(p["shares"], int(-diff / float(cur_px) / 100) * 100)
                    if sell_n <= 0:
                        continue
                    sp, net, fee = _sell(sell_n, float(cur_px))
                    cost_part = p["cost"] * (sell_n / p["shares"])
                    pnl = net - cost_part
                    trades.append(dict(date=day, code=code, name=names.get(code, code),
                                       side="sell", reason="减仓", price=round(sp, 2),
                                       shares=sell_n, amount=round(net, 2), pnl=round(pnl, 2),
                                       pnl_pct=round((sp / p["entry_price"] - 1) * 100, 2)))
                    cash += net
                    total_cost += fee
                    turnover_amount += sell_n * sp
                    p["shares"] -= sell_n
                    p["cost"] -= cost_part
                    if p["shares"] <= 0:
                        del positions[code]

        # ---- 每日净值
        hold_val = 0.0
        for code, p in positions.items():
            cur = px.get(code)
            if cur is not None and not np.isnan(cur) and cur > 0:
                hold_val += p["shares"] * float(cur)
        nav_curve.append(dict(date=day, nav=round(cash + hold_val, 2),
                              cash=round(cash, 2), holdings_value=round(hold_val, 2),
                              n_holdings=len(positions)))
        holdings_hist.append(len(positions))

    # ---- 期末按收盘价清算（仅用于计算指标，不计交易成本）
    last_day = calendar[-1]
    last_px = price.loc[last_day]
    final_nav = cash + sum(p["shares"] * float(last_px.get(c, p["entry_price"]))
                           for c, p in positions.items())

    result = _metrics(
        nav_curve=nav_curve, trades=trades, final_nav=final_nav,
        price=price, strategy=strategy, days=len(calendar),
        total_cost=total_cost, turnover_amount=turnover_amount,
        positions=positions, names=names, inds=inds, last_px=last_px,
        rebal_count=rebal_count, holdings_hist=holdings_hist,
        universe=[dict(code=c, name=names.get(c, c), industry=inds.get(c) or "其他")
                  for c in price.columns],
        params=dict(days=days, rebalance=rebalance, max_holdings=max_holdings,
                    max_industry_weight=max_industry_weight, weighting=weighting,
                    stop_loss=stop_loss, universe_size=universe_size),
    )
    with _cache_lock:
        _cache[key] = dict(ts=now, data=result)
        if len(_cache) > 24:
            oldest = min(_cache, key=lambda k: _cache[k]["ts"])
            _cache.pop(oldest, None)
    return result


def _metrics(nav_curve, trades, final_nav, price, strategy, days, total_cost,
             turnover_amount, positions, names, inds, last_px, rebal_count,
             holdings_hist, universe, params):
    nav = pd.Series([x["nav"] for x in nav_curve])
    dr = nav.pct_change().dropna()
    total_return = (final_nav / INIT_CAPITAL - 1) * 100
    years = max(days / 244.0, 1e-9)
    cagr = ((final_nav / INIT_CAPITAL) ** (1 / years) - 1) * 100 if final_nav > 0 else -100
    ann_vol = float(dr.std() * np.sqrt(244) * 100) if len(dr) > 1 else 0.0
    sharpe = cagr / ann_vol if ann_vol > 0 else 0.0
    downside = dr[dr < 0]
    sortino = cagr / (float(downside.std()) * np.sqrt(244) * 100) if len(downside) > 1 and downside.std() > 0 else 0.0
    cummax = nav.cummax()
    dd = (nav / cummax - 1) * 100
    max_dd = float(dd.min()) if len(dd) else 0.0
    mdd_duration, cur = 0, 0
    for v in (dd < 0):
        cur = cur + 1 if v else 0
        mdd_duration = max(mdd_duration, cur)
    calmar = cagr / abs(max_dd) if max_dd < 0 else 0.0

    sells = [t for t in trades if t["side"] == "sell" and t.get("pnl") is not None]
    wins = [t for t in sells if t["pnl"] > 0]
    losses = [t for t in sells if t["pnl"] <= 0]
    win_rate = len(wins) / len(sells) * 100 if sells else 0.0
    avg_win = float(np.mean([t["pnl_pct"] for t in wins])) if wins else 0.0
    avg_loss = abs(float(np.mean([t["pnl_pct"] for t in losses]))) if losses else 0.0
    gross_win = sum(t["pnl"] for t in wins)
    gross_loss = abs(sum(t["pnl"] for t in losses))
    profit_factor = round(gross_win / gross_loss, 2) if gross_loss > 0 else None
    turnover_annual = (turnover_amount / INIT_CAPITAL / 2) / years if years > 0 else 0

    # 基准：等权买入持有（组合内全部标的）
    rets = price.pct_change().dropna(how="all")
    bench_nav = (1 + rets.mean(axis=1)).cumprod()
    bench_return = (float(bench_nav.iloc[-1]) - 1) * 100 if len(bench_nav) else 0.0
    bench_curve = [dict(date=nav_curve[0]["date"], nav=INIT_CAPITAL)]
    for d, v in bench_nav.items():
        bench_curve.append(dict(date=str(d), nav=round(float(v) * INIT_CAPITAL, 2)))

    # 期末行业暴露
    ind_exp = {}
    for c, p in positions.items():
        ind = inds.get(c) or "其他"
        val = p["shares"] * float(last_px.get(c, p["entry_price"]))
        ind_exp[ind] = ind_exp.get(ind, 0.0) + (val / final_nav * 100 if final_nav else 0)
    ind_exp = {k: round(v, 1) for k, v in sorted(ind_exp.items(), key=lambda x: -x[1])}

    holdings = []
    for c, p in positions.items():
        cur = float(last_px.get(c, p["entry_price"]))
        holdings.append(dict(
            code=c, name=names.get(c, c), industry=inds.get(c) or "其他",
            shares=p["shares"], cost=round(p["entry_price"], 2), last=round(cur, 2),
            market_value=round(p["shares"] * cur, 2),
            pnl_pct=round((cur / p["entry_price"] - 1) * 100, 2) if p["entry_price"] else 0,
            weight=round(p["shares"] * cur / final_nav * 100, 1) if final_nav else 0,
            entry_date=p.get("entry_date"),
        ))
    holdings.sort(key=lambda x: -x["weight"])

    return dict(
        strategy=strategy,
        strategy_name=bt.STRATEGY_NAMES.get(strategy, "多因子打分"),
        period=dict(start=nav_curve[0]["date"], end=nav_curve[-1]["date"], days=days),
        metrics=dict(
            total_return=round(total_return, 2),
            cagr=round(cagr, 2),
            annual_volatility=round(ann_vol, 2),
            sharpe=round(sharpe, 2),
            sortino=round(sortino, 2),
            calmar=round(calmar, 2),
            max_drawdown=round(max_dd, 2),
            max_dd_duration_days=mdd_duration,
            win_rate=round(win_rate, 1),
            profit_factor=profit_factor,
            avg_win_pct=round(avg_win, 2),
            avg_loss_pct=round(avg_loss, 2),
            trade_count=len(sells),
            buy_count=len([t for t in trades if t["side"] == "buy"]),
            rebalance_count=rebal_count,
            avg_holdings=round(float(np.mean(holdings_hist)), 1) if holdings_hist else 0,
            turnover_annual=round(turnover_annual, 2),
            total_cost=round(total_cost, 2),
            cost_ratio=round(total_cost / INIT_CAPITAL * 100, 2),
            initial_capital=INIT_CAPITAL,
            final_nav=round(final_nav, 2),
            benchmark_return=round(bench_return, 2),
            excess_return=round(total_return - bench_return, 2),
        ),
        nav_curve=nav_curve,
        bench_curve=bench_curve,
        trades=trades[-40:],
        holdings=holdings,
        industry_exposure=ind_exp,
        universe=universe,
        params=params,
        disclaimer=("组合回测基于历史数据，不构成投资建议。全仓买入持有同期等权组合作为基准；"
                    "交易成本按 A 股标准费率近似（佣金0.025%最低5元、印花税0.05%、滑点0.1%）。"),
    )
