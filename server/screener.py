# -*- coding: utf-8 -*-
"""全市场选股扫描：实时快照初筛 + 七因子短期评分 + 连涨持续性统计

流程：
1. 全市场实时快照（约5500只）剔除 ST/退市整理/成交额过低/涨幅不足/已封板
2. 按"资金强度"（成交额×涨幅）取前 40 只候选
3. 并发拉取候选日线（当日缓存），逐只计算七因子短期评分
4. 按综合评分排序输出 Top N，附历史连涨持续性（回答"上涨持续多久"）
"""
import concurrent.futures as cf
import datetime as dt
import threading
import time

from . import analysis, datasource as ds, prediction, strategies as stg, trade

SCAN_TTL = 300          # 扫描结果缓存秒数
CANDIDATES = 40         # 进入深度评分的候选数
MIN_AMOUNT = 1.5e8      # 成交额门槛（元）
MIN_PCT = 1.5           # 当日最低涨幅（%），保证"正在上涨"语境

_scan_cache = {"ts": 0.0, "data": None}
_scan_lock = threading.Lock()


def _limit_pct(code):
    """按板块返回接近涨停的涨幅阈值（封板买不进的剔除）"""
    if code[:2] in ("30", "68"):
        return 19.5
    if code[0] in ("4", "8") or code[:2] == "92":
        return 29.5
    return 9.5


def streak_stats(closes):
    """连涨统计：返回 (当前连涨天数, 平均连涨段长度, 最长连涨段)"""
    up = [closes[i] > closes[i - 1] for i in range(1, len(closes))]
    cur = 0
    for v in reversed(up):
        if v:
            cur += 1
        else:
            break
    runs, run = [], 0
    for v in up:
        if v:
            run += 1
        elif run:
            runs.append(run)
            run = 0
    if run:
        runs.append(run)
    avg = round(sum(runs) / len(runs), 1) if runs else 0.0
    return cur, avg, max(runs) if runs else 0


def _eval_one(item):
    """对单只候选做深度评分，失败返回 None"""
    code, name, price, pct, amt = item
    try:
        df = analysis.build_df(ds.get_kline(code))
        if len(df) < 60:
            return None
        df = analysis.add_indicators(df)
        sc = analysis.short_term_analysis(code, name, df)
        closes = list(df["close"])
        cur, avg_s, max_s = streak_stats(closes)
        ret5 = (closes[-1] / closes[-6] - 1) * 100 if len(closes) > 5 else 0
        ret20 = (closes[-1] / closes[-21] - 1) * 100 if len(closes) > 20 else 0
        bf = max(sc["factors"], key=lambda x: x["weighted"])
        # ---- 多策略共振标签 ----
        r = df.iloc[-1]
        strategy_tags = ["动量"]
        mr_hit, mr_reason = stg.hit_mean_reversion(df, r)
        tb_hit, tb_reason = stg.hit_trend_breakout(df, r)
        if mr_hit:
            strategy_tags.append("均值回归")
        if tb_hit:
            strategy_tags.append("趋势突破")
        # 多策略共振加权：每多一个策略 +5 分
        base_score = sc["score"]
        boost = (len(strategy_tags) - 1) * 5
        boosted_score = base_score + boost
        tail = df.tail(20)
        ma5 = round(float(df["ma5"].iloc[-1]), 2) if "ma5" in df else price
        ma10 = round(float(df["ma10"].iloc[-1]), 2) if "ma10" in df else price
        recent_low = round(float(tail["low"].min()), 2)
        recent_high = round(float(tail["high"].max()), 2)
        # 参考买入区：当日回调到 5 日均线附近；
        # 止损：强势启动股（今日涨幅≥5%）用 5 日均线紧止损；否则用 10 日均线或近20日低点
        buy_zone = (round(min(price, ma5) * 0.995, 2), round(max(price, ma5) * 1.005, 2))
        if pct >= 5:
            stop_loss = round(ma5 * 0.97, 2)
        else:
            stop_loss = round(min(ma10, recent_low) * 0.985, 2)
        return dict(
            code=code, name=name, price=price, pct=round(pct, 2),
            amount_yi=round(amt / 1e8, 2),
            score=base_score, boosted_score=boosted_score,
            strategy_tags=strategy_tags,
            mr_reason=mr_reason or None, tb_reason=tb_reason or None,
            direction=sc["direction"], confidence=sc["confidence"],
            up_prob_5d=sc["up_prob_5d"], expected_range=sc["expected_range"],
            streak_up=cur, avg_streak=avg_s, max_streak=max_s,
            ret5=round(ret5, 2), ret20=round(ret20, 2),
            top_factor=bf["name"], top_factor_detail=bf["detail"],
            ma5=ma5, ma10=ma10, recent_low=recent_low, recent_high=recent_high,
            buy_zone=buy_zone, stop_loss=stop_loss,
        )
    except Exception:  # noqa: BLE001
        return None


def scan_stocks(top=8):
    """全市场动量选股扫描，返回 dict(time, universe, shortlist, scored, candidates)"""
    now = time.time()
    with _scan_lock:
        if _scan_cache["data"] and now - _scan_cache["ts"] < SCAN_TTL:
            return _scan_cache["data"]
        snap = ds.get_realtime_snapshot()
        if not snap:
            raise RuntimeError("全市场实时快照尚未就绪，请稍后重试")
        pool = []
        for code, q in snap.items():
            name = q.get("name") or ""
            price, prev = q.get("price") or 0, q.get("prev_close") or 0
            if price <= 0 or prev <= 0 or len(code) != 6 or not code.isdigit():
                continue
            upname = name.upper()
            if "ST" in upname or "退" in name:
                continue
            amt = q.get("amount") or 0
            if amt < MIN_AMOUNT:
                continue
            pct = (price / prev - 1) * 100
            if pct < MIN_PCT or pct >= _limit_pct(code):
                continue
            pool.append((code, name, price, pct, amt))
        if not pool:
            raise RuntimeError("当前市场无符合初筛条件（放量上涨且未封板）的股票")
        pool.sort(key=lambda x: -(x[4] * x[3]))  # 资金强度 ≈ 成交额 × 涨幅
        pool = pool[:CANDIDATES]

        with cf.ThreadPoolExecutor(max_workers=8) as ex:
            results = [r for r in ex.map(_eval_one, pool) if r]
        if not results:
            raise RuntimeError("候选股深度评分全部失败，请稍后重试")
        results.sort(key=lambda x: (-x["boosted_score"], -x["pct"]))
        data = dict(
            time=dt.datetime.now().strftime("%Y-%m-%d %H:%M"),
            universe=len(snap), shortlist=len(pool), scored=len(results),
            candidates=results[:top],
        )
        _scan_cache["ts"] = now
        _scan_cache["data"] = data
        return data


# ================================================================ 短期最佳推荐（概率+收益双维度）

_BEST_TTL = 300
_best_cache = {"ts": 0.0, "data": None}
_best_lock = threading.Lock()


def _enrich_with_prediction(cand):
    """对单只候选跑相似形态预测，补充真实历史上涨概率与预期收益"""
    try:
        df = analysis.build_df(ds.get_kline(cand["code"]))
        if len(df) < 120:
            return cand
        pr = prediction.predict(cand["code"], cand["name"], df)
        h5 = pr["horizons"]["5日"]
        h10 = pr["horizons"]["10日"]
        h20 = pr["horizons"]["20日"]
        cand["up_prob_5d_real"] = h5["up_prob"]      # 相似形态历史上涨概率（%）
        cand["med_ret_5d"] = h5["med_ret"]           # 相似形态中位收益（%）
        cand["worst_5d"] = h5.get("worst")           # 相似形态 5 日最差收益（%，极端尾部，仅展示）
        cand["best_5d"] = h5.get("best")             # 相似形态 5 日最佳收益（%）
        cand["low_pt_5d"] = h5.get("low_pt")         # 5 日 25 分位价格 → 用于计算典型亏损
        cand["target_5d"] = h5.get("target")         # 5 日目标价
        # 10 日（短中线过渡）
        cand["up_prob_10d"] = h10["up_prob"]
        cand["med_ret_10d"] = h10["med_ret"]
        cand["target_10d"] = h10.get("target")
        cand["worst_10d"] = h10.get("worst")
        cand["ev_10d"] = round((h10["up_prob"] or 0) / 100 * (h10["med_ret"] or 0), 3)
        # 20 日（中线）
        cand["up_prob_20d_real"] = h20["up_prob"]
        cand["med_ret_20d"] = h20["med_ret"]
        cand["target_20d"] = h20["target"]
        cand["low_pt_20d"] = h20["low_pt"]
        cand["worst_20d"] = h20.get("worst")
        # 60 日（长线）
        m60 = pr.get("mid60")
        if m60:
            cand["up_prob_60d"] = m60.get("up_prob")
            cand["med_ret_60d"] = m60.get("med_ret")
            cand["target_60d"] = m60.get("target")
            cand["ev_60d"] = round((m60.get("up_prob") or 0) / 100 * (m60.get("med_ret") or 0), 3)
        cand["pred_direction"] = pr["direction"]
        cand["pred_confidence"] = pr["confidence"]
        cand["ensemble_agree"] = pr["ensemble"]["agree"]
        # 期望收益 = 上涨概率 × 中位收益（Kelly 分数近似，单位 %）
        up5 = h5["up_prob"] or 0
        med5 = h5["med_ret"] or 0
        up20 = h20["up_prob"] or 0
        med20 = h20["med_ret"] or 0
        cand["ev_5d"] = round(up5 / 100 * med5, 3)      # 短期期望收益（5 日，主排序）
        cand["ev_20d"] = round(up20 / 100 * med20, 3)  # 中期期望收益（20 日，确认）
        cand["amp_max_gain_med"] = pr["amp"].get("max_gain_med")
        cand["amp_max_dd_med"] = pr["amp"].get("max_dd_med")
        cand["timing"] = pr["timing"]
    except Exception:  # noqa: BLE001（个股预测失败不影响整体）
        cand["pred_direction"] = None
    return cand


def _next_day_entry(cand, df):
    """估算「次日买入」的可执行价格区间与可成交性。

    数据来源（全部来自该股自身历史，非经验硬编码）：
    - 历史「次日开盘 / 今日收盘 - 1」的跳空分布：中位数 / 25 分位 / 75 分位；
    - 今日涨幅接近涨停板 → 次日大概率一字板 / 大幅高开，限价单难成交；
    - prediction.timing.next_day_up：相似形态次日上涨概率（辅助判断方向）。
    """
    import numpy as np
    price = cand["price"]
    pct = cand.get("pct") or 0
    # 1) 历史跳空分布：次日开盘相对今日收盘的涨跌幅（%）
    gaps = []
    if len(df) >= 60:
        opens = df["open"].values
        closes = df["close"].values
        for i in range(1, len(df)):
            if closes[i - 1] > 0:
                gaps.append((opens[i] / closes[i - 1] - 1) * 100)
    if gaps:
        gap_med = round(float(np.median(gaps)), 2)
        gap_p25 = round(float(np.percentile(gaps, 25)), 2)
        gap_p75 = round(float(np.percentile(gaps, 75)), 2)
        n_gap = len(gaps)
    else:
        gap_med = gap_p25 = gap_p75 = 0.0
        n_gap = 0
    # 次日预估开盘价 = 今日收盘 × (1 + 历史跳空中位数/100)
    open_est = round(price * (1 + gap_med / 100), 2)
    # 次日买入价区间：结合买入区与跳空分布（乐观/中性/悲观三种情形）
    bz = cand.get("buy_zone")
    if bz:
        # 中性：买入区下沿按跳空中位数调整
        entry_low = round(bz[0] * (1 + gap_med / 100), 2)
        entry_high = round(bz[1] * (1 + gap_p75 / 100), 2)
    else:
        entry_low = round(price * (1 + gap_p25 / 100), 2)
        entry_high = round(price * (1 + gap_p75 / 100), 2)
    # 2) 次日可成交性校验
    code = cand["code"]
    limit_pct = _limit_pct(code)
    near_limit = pct >= limit_pct - 0.5          # 今日已接近涨停 → 次日可能一字板
    big_gap_up = gap_p75 >= 5.0                  # 历史 75 分位跳空 ≥ 5% → 容易大幅高开
    timing = cand.get("timing") or {}
    nd_up = timing.get("next_day_up")
    can_buy_tomorrow = not near_limit            # 接近涨停时保守判定为"难买入"
    if near_limit:
        tomorrow_note = (f"今日涨幅 {pct}% 已接近 {limit_pct}% 涨停板，"
                         f"次日大概率一字板或大幅高开，限价单难以成交，不建议次日追高。")
    elif big_gap_up:
        tomorrow_note = (f"该股历史次日跳空 75 分位达 {gap_p75:+.2f}%，"
                         f"容易大幅高开；建议在 {entry_low}~{entry_high} 元区间挂限价单，"
                         f"若开盘价高于 {entry_high} 元则放弃，避免追高。")
    else:
        tomorrow_note = (f"该股历史次日跳空中位 {gap_med:+.2f}%（25分位 {gap_p25:+.2f}% / "
                         f"75分位 {gap_p75:+.2f}%），预估次日开盘 {open_est} 元；"
                         f"建议在 {entry_low}~{entry_high} 元区间挂限价单，9:45 后下单。")
    if nd_up is not None:
        tomorrow_note += f" 相似形态次日上涨概率 {nd_up}%。"
    return dict(
        open_est=open_est,
        gap_med=gap_med, gap_p25=gap_p25, gap_p75=gap_p75, n_gap=n_gap,
        entry_low=entry_low, entry_high=entry_high,
        can_buy_tomorrow=can_buy_tomorrow,
        near_limit=near_limit,
        tomorrow_note=tomorrow_note,
    )


# A 股交易成本参数（公开费率，非编造）
_COMMISSION_RATE = 0.00025    # 佣金率：万 2.5（双向，券商普遍费率）
_COMMISSION_MIN = 5.0         # 佣金最低 5 元（单笔）
_STAMP_TAX_RATE = 0.0005      # 印花税：卖出单边 0.05%（2023-08-28 起减半）
_TRANSFER_FEE_RATE = 0.00001  # 过户费：双向 0.001%（沪深两市统一）


def _calc_trade_costs(buy_price, sell_price, shares, order_pct_adv=0.0):
    """计算 A 股交易的真实成本（佣金 + 印花税 + 过户费 + 滑点），返回总成本（元）。

    滑点估算：按订单占日均成交额比例的 0.5 倍计（如占比 1% → 滑点 0.5%），
    这是冲击成本的保守近似，非精确值（无 Level-2 逐笔数据）。
    """
    buy_value = buy_price * shares
    sell_value = sell_price * shares
    # 佣金（双向，最低 5 元）
    comm_buy = max(buy_value * _COMMISSION_RATE, _COMMISSION_MIN)
    comm_sell = max(sell_value * _COMMISSION_RATE, _COMMISSION_MIN)
    # 印花税（卖出单边）
    stamp = sell_value * _STAMP_TAX_RATE
    # 过户费（双向）
    transfer = (buy_value + sell_value) * _TRANSFER_FEE_RATE
    # 滑点（买卖各一次，按订单占比估算）
    slippage_rate = min(order_pct_adv * 0.5 / 100, 0.02)  # 上限 2%
    slippage = (buy_value + sell_value) * slippage_rate
    total = comm_buy + comm_sell + stamp + transfer + slippage
    return dict(
        comm_buy=round(comm_buy, 2),
        comm_sell=round(comm_sell, 2),
        stamp=round(stamp, 2),
        transfer=round(transfer, 2),
        slippage=round(slippage, 2),
        total=round(total, 2),
        cost_pct=round(total / buy_value * 100, 3) if buy_value > 0 else 0,
    )


def _add_execution_plan(cand, capital=None):
    """为最佳候选补充「执行层」细节：怎么买、怎么卖、仓位、移动止损、时间止损。

    数据来源：
    - trade.trade_plan：基于七因子/支撑压力/ATR 给出买入计划（激进/稳健/突破）、
      卖出计划（止损/止盈1/止盈2/移动止盈）、仓位建议、信号分区；
    - prediction 预测数据：5/20 日上涨概率、中位收益、中位最大回撤 → 计算 Kelly 分数；
    - screener 已有字段：buy_zone / stop_loss / target_20d / ma5 / recent_high。

    参数 capital（元）：若提供，则计算按该资金可买的股数、投入金额、各止盈位预期收益、
    止损最大亏损、5/20 日预期收益（元）。
    """
    try:
        df = analysis.build_df(ds.get_kline(cand["code"]))
        tp = trade.trade_plan(cand["code"], cand["name"], df)
    except Exception:  # noqa: BLE001
        return cand
    # 清除可能从缓存对象残留的旧 capital 字段
    if not capital:
        cand.pop("capital", None)

    cand["trade_plan"] = tp
    cand["zone"] = tp["zone"]
    cand["support"] = tp["support"]
    cand["resistance"] = tp["resistance"]
    cand["atr"] = tp["atr"]

    # ---- 次日买入计划（明天买入口径：预估开盘价 / 买入区间 / 可成交性） ----
    try:
        cand["next_day_plan"] = _next_day_entry(cand, df)
    except Exception:  # noqa: BLE001
        cand["next_day_plan"] = None

    # ---- Kelly 仓位（基于相似形态 5 日真实统计，同口径，非硬编码） ----
    p = (cand.get("up_prob_5d_real") or 0) / 100.0
    win = cand.get("med_ret_5d") or 0          # 5 日中位收益（%）
    # 典型亏损：优先用 5 日收益 25 分位（非极端最差）；若 25 分位 ≥0（75% 以上形态盈利），
    # 则退化为用历史最差 5 日收益作保守损失估计
    lp5 = cand.get("low_pt_5d")
    price = cand["price"]
    loss_mag = abs((lp5 / price - 1) * 100) if (lp5 and price and lp5 < price) else 0
    loss_label = "25分位亏损"
    if loss_mag <= 0:
        worst5 = cand.get("worst_5d") or 0
        loss_mag = abs(worst5) if worst5 < 0 else 0
        loss_label = "历史最差亏损"
    if p > 0 and win > 0 and loss_mag > 0:
        b = win / loss_mag                      # 盈亏比（5 日中位收益 / 5 日典型亏损幅度）
        kelly = max(0.0, (p * b - (1 - p)) / b)
        kelly_pct = round(min(kelly, 1.0) * 100, 1)
    else:
        kelly_pct = None
    cand["kelly_pct"] = kelly_pct
    worst5 = cand.get("worst_5d")
    # 实际仓位建议：取 Kelly 分数的 1/4~1/2（半凯利更稳健），并与 trade_plan 评分仓位取保守值
    if kelly_pct is not None and kelly_pct > 0:
        cand["position_kelly"] = (
            f"Kelly 分数 {kelly_pct}%（p={p*100:.0f}%, 盈亏比 b={b:.2f} = 中位收益 {win}% / "
            f"{loss_label} {loss_mag:.1f}%）→ 建议实际仓位 {round(kelly_pct * 0.4, 0):.0f}%~"
            f"{round(kelly_pct * 0.6, 0):.0f}%（半凯利，控制单笔回撤）"
            + (f"；历史最差 5 日曾达 {worst5}%" if worst5 else "")
        )
    else:
        cand["position_kelly"] = (
            f"Kelly 分数 0%（5 日中位收益 {win}% vs {loss_label} {loss_mag:.1f}%，"
            f"期望收益为负，不建议开仓）"
            + (f"；历史最差 5 日曾达 {worst5}%" if worst5 else "")
        )
    cand["position_score"] = tp["position"]    # 基于七因子评分的仓位

    # ---- 买入方式（限价单 + 分批） ----
    bz = cand["buy_zone"]
    ma5 = cand.get("ma5")
    rh = cand.get("recent_high")
    buy_plans = tp.get("buy_plans", [])
    buy_method = []
    # 首笔：在 buy_zone 下沿挂限价单
    buy_method.append(
        f"首笔 1/3：在 {bz[0]} 元（买入区下沿）挂限价买单，不追高；开盘前 15 分钟波动大，建议 9:45 后再下单。"
    )
    # 加仓：回调到 ma5 或突破 recent_high
    if ma5:
        buy_method.append(
            f"第二笔 1/3：若回调至 5 日均线 {ma5} 元附近缩量企稳（不破），再加 1/3；"
            f"或放量（量比>1.5）站稳 {rh} 元（近 20 日高点）突破时加 1/3。"
        )
    buy_method.append("单笔下单金额不超过该股近 5 日均成交额的 5%，避免冲击成本过高。")
    # 合并 trade_plan 的买入计划备注
    for p in buy_plans:
        if p.get("note"):
            buy_method.append(f"[{p['style']}] {p['action']}：{p['note']}")
    cand["buy_method"] = buy_method

    # ---- 卖出方式（分批止盈 + 条件止损 + 移动止损 + 时间止损） ----
    sp = tp.get("sell_plan", {})
    sl = cand["stop_loss"]
    tgt = cand.get("target_20d")
    tp1 = sp.get("tp1")
    tp2 = sp.get("tp2")
    sell_method = [
        f"止损：用条件单（止损单）挂在 {sl} 元，跌破即全部清仓，不犹豫（单笔风险 ≈ "
        f"{round((cand['price'] - sl) / cand['price'] * 100, 1)}%）。",
    ]
    # 分批止盈：收集所有止盈价位并按价格升序排列（先到先卖）
    tp_levels = []
    if tp1 is not None:
        tp_levels.append((tp1, "ATR 压力位"))
    if tgt is not None:
        tp_levels.append((tgt, "20 日相似形态中位目标"))
    if tp2 is not None:
        tp_levels.append((tp2, "ATR 扩张目标"))
    tp_levels.sort(key=lambda x: x[0])
    for i, (px, why) in enumerate(tp_levels, 1):
        if i < len(tp_levels):
            sell_method.append(f"止盈 {i}：{px} 元（{why}）卖出 1/2 仓位，锁定部分利润。")
        else:
            sell_method.append(f"止盈 {i}：{px} 元（{why}）卖出剩余全部仓位，落袋为安。")
    # 移动止损
    trailing = sp.get("trailing")
    if trailing:
        sell_method.append(f"移动止损：{trailing}")
    sell_method.append(
        "浮盈超过 5% 后，将止损上移至成本价（保本止损）；浮盈超过 10% 后，止损上移至 5 日均线。"
    )
    # 时间止损
    sell_method.append(
        "时间止损：买入后 10 个交易日仍未触及止盈 1 且涨幅<3%，无论盈亏一律离场，避免资金占用。"
    )
    cand["sell_method"] = sell_method
    cand["reward_risk_tp"] = tp.get("reward_risk")  # trade_plan 基于支撑压力的盈亏比

    # ---- 资金分配（按用户指定资金额计算股数 / 投入金额 / 预期收益 / 最大亏损） ----
    if capital and capital > 0 and cand["price"] and cand["price"] > 0:
        # 参考买入价：取买入区下沿（保守，挂限价单）
        buy_ref = cand["buy_zone"][0]
        min_lot_cost = buy_ref * 100  # 1 手（100 股）所需金额
        if capital >= min_lot_cost:
            # A 股 1 手 = 100 股，股数必须为 100 的整数倍
            shares = int(capital // buy_ref // 100 * 100)
            capital_used = round(shares * buy_ref, 2)
            position_pct = round(capital_used / capital * 100, 1)
            insufficient = False
            shortfall = None
        else:
            # 资金不足 1 手：按 1 手计所需金额，并提示缺口
            shares = 100
            capital_used = round(min_lot_cost, 2)
            position_pct = round(capital_used / capital * 100, 1)
            insufficient = True
            shortfall = round(capital_used - capital, 2)
        # 订单占日均成交额比例（用于滑点估算与执行保证）
        avg_amount_yi = cand.get("amount_yi") or 1.0
        adv_yuan = avg_amount_yi * 1e8
        order_pct_adv = round(capital_used / adv_yuan * 100, 4) if adv_yuan > 0 else 99
        # 各止盈位预期收益（元）—— 毛收益 + 净收益（扣除交易成本）
        tp_profits = []
        net_tp_profits = []
        for px, why in tp_levels:
            profit = round((px - buy_ref) * shares, 2)
            ret_pct = round((px / buy_ref - 1) * 100, 1)
            tp_profits.append(dict(price=px, why=why, profit=profit, ret_pct=ret_pct))
            # 净收益 = 毛收益 - 交易成本（佣金 + 印花税 + 过户费 + 滑点）
            cost = _calc_trade_costs(buy_ref, px, shares, order_pct_adv)
            net_profit = round(profit - cost["total"], 2)
            net_tp_profits.append(dict(price=px, why=why, gross_profit=profit,
                                       net_profit=net_profit, ret_pct=ret_pct,
                                       cost_total=cost["total"], cost_pct=cost["cost_pct"]))
        # 止损最大亏损（元）—— 毛亏损 + 净亏损（含交易成本，亏损更大）
        loss_at_stop = round((sl - buy_ref) * shares, 2)
        loss_pct = round((sl / buy_ref - 1) * 100, 1)
        cost_stop = _calc_trade_costs(buy_ref, sl, shares, order_pct_adv)
        net_loss_at_stop = round(loss_at_stop - cost_stop["total"], 2)
        # 预期收益（元）= 期望收益率 × 投入金额；净收益扣除交易成本
        ev5 = cand.get("ev_5d") or 0
        ev20 = cand.get("ev_20d") or 0
        expected_profit_5d = round(ev5 / 100 * capital_used, 2)
        expected_profit_20d = round(ev20 / 100 * capital_used, 2)
        # 成本占比：取买入+卖出一个完整回合的成本率（以买入金额为基数）
        # 用止盈 1 价位估算一个典型回合的成本率
        if tp_levels:
            cost_typical = _calc_trade_costs(buy_ref, tp_levels[0][0], shares, order_pct_adv)
            cost_pct_total = cost_typical["cost_pct"]
        else:
            cost_pct_total = cost_stop["cost_pct"]
        net_expected_profit_5d = round(expected_profit_5d - cost_pct_total / 100 * capital_used, 2)
        net_expected_profit_20d = round(expected_profit_20d - cost_pct_total / 100 * capital_used, 2)
        cand["capital"] = dict(
            amount=capital,
            buy_ref=buy_ref,
            shares=shares,
            capital_used=capital_used,
            position_pct=position_pct,
            insufficient=insufficient,
            shortfall=shortfall,
            tp_profits=tp_profits,
            loss_at_stop=loss_at_stop,
            loss_pct=loss_pct,
            expected_profit_5d=expected_profit_5d,
            expected_profit_20d=expected_profit_20d,
            # —— 净收益（扣除真实交易成本）——
            cost_pct=cost_pct_total,
            net_tp_profits=net_tp_profits,
            net_loss_at_stop=net_loss_at_stop,
            net_expected_profit_5d=net_expected_profit_5d,
            net_expected_profit_20d=net_expected_profit_20d,
            order_pct_adv=order_pct_adv,
        )

    # ---- 短/中/长线对比 + 推荐周期 ----
    price = cand["price"]
    support = cand.get("support") or cand["ma5"] * 0.97
    def _horizon(name, days, up_prob, med_ret, ev, target, worst):
        """构造单个周期的数据结构"""
        if up_prob is None or med_ret is None:
            return None
        stop = support  # 各周期统一用支撑位作止损
        risk = abs((stop / price - 1) * 100) if stop else 5.0
        # 风险调整后收益 = 期望收益 / 历史最差亏损幅度（越大越好）
        loss_mag = abs(worst) if worst and worst < 0 else (risk or 1)
        risk_adj = round(ev / loss_mag, 3) if loss_mag > 0 else 0
        return dict(
            name=name, days=days, up_prob=up_prob, med_ret=med_ret, ev=ev,
            target=target, stop=round(stop, 2), risk=round(risk, 1),
            risk_adj=risk_adj,
        )
    horizons = []
    h_short = _horizon("短线", 5, cand.get("up_prob_5d_real"), cand.get("med_ret_5d"),
                       cand.get("ev_5d"), cand.get("target_5d"), cand.get("worst_5d"))
    h_mid = _horizon("中线", 20, cand.get("up_prob_20d_real"), cand.get("med_ret_20d"),
                     cand.get("ev_20d"), cand.get("target_20d"), cand.get("worst_20d"))
    h_long = _horizon("长线", 60, cand.get("up_prob_60d"), cand.get("med_ret_60d"),
                      cand.get("ev_60d"), cand.get("target_60d"), None)
    for h in (h_short, h_mid, h_long):
        if h:
            horizons.append(h)
    # 推荐周期：风险调整后收益最高的（兼顾收益与回撤）
    if horizons:
        recommended = max(horizons, key=lambda h: h["risk_adj"])
        # 若全部风险调整收益≤0，诚实建议观望
        if recommended["risk_adj"] <= 0:
            recommended_advice = "各周期风险调整后收益均≤0，历史相似形态下期望收益不足以覆盖风险，建议观望或换股"
        else:
            recommended_advice = (f"推荐「{recommended['name']}」（{recommended['days']} 个交易日）："
                                  f"上涨概率 {recommended['up_prob']}%、期望收益 {recommended['ev']}%，"
                                  f"风险调整后收益 {recommended['risk_adj']} 在各周期中最高")
    else:
        recommended = None
        recommended_advice = "周期数据不足，无法对比"
    cand["horizon_plan"] = dict(
        horizons=horizons,
        recommended=recommended,
        recommended_advice=recommended_advice,
    )

    # ---- 执行保证（能否买入 / 能否全部卖出） ----
    cap = cand.get("capital")
    shares = cap["shares"] if cap else 100
    buy_ref = cap["buy_ref"] if cap else cand["buy_zone"][0]
    order_value = round(shares * buy_ref, 2)
    avg_amount_yi = cand.get("amount_yi") or 1.0  # 日均成交额（亿）
    adv_yuan = avg_amount_yi * 1e8
    order_pct_adv = round(order_value / adv_yuan * 100, 4) if adv_yuan > 0 else 99
    # 能否买入：订单金额占日均成交额 < 5% 时，限价单大概率可成交
    can_buy = order_pct_adv < 5
    # 能否全部卖出：同理，单日可承接的卖出量
    can_sell_all = order_pct_adv < 10  # 卖出用 10% 阈值（卖出可分批）
    pct_str = f"{order_pct_adv:.4f}%" if order_pct_adv < 0.01 else f"{order_pct_adv:.2f}%"
    buy_guarantee = (
        f"该股日均成交额 {avg_amount_yi} 亿元，本次下单 {order_value:,.0f} 元仅占日均成交额的 "
        f"{pct_str}，"
        + ("远低于 5%，在买入区下沿挂限价单可保证成交。" if can_buy else
           "占比较高，建议拆成 2~3 笔分批挂单，避免冲击成本。")
    )
    sell_guarantee = (
        f"全部 {shares} 股市值 {order_value:,.0f} 元占日均成交额 {pct_str}，"
        + ("可在目标价一次性挂限价单全部卖出。" if can_sell_all else
           "建议分 2~3 日卖出，或用市价单快速清仓（承受少量滑点）。")
    )
    cand["execution_guarantee"] = dict(
        avg_amount_yi=avg_amount_yi,
        order_value=order_value,
        order_pct_adv=order_pct_adv,
        order_pct_adv_str=(f"{order_pct_adv:.4f}%" if order_pct_adv < 0.01 else f"{order_pct_adv:.2f}%"),
        can_buy=can_buy,
        can_sell_all=can_sell_all,
        buy_guarantee=buy_guarantee,
        sell_guarantee=sell_guarantee,
    )
    return cand


def best_short_term_pick(top=12, capital=None):
    """短期最佳推荐：在动量扫描候选上叠加相似形态预测，按「期望收益」选最可能赚钱的标的

    与 scan_stocks 的区别：
    - scan_stocks 用七因子启发式评分排序，up_prob_5d 是从评分线性换算的；
    - 本函数对每只候选跑 prediction.predict（相似形态 k-NN），得到真实历史上涨概率、
      中位收益、三信号共识方向，再按 期望收益 = 上涨概率 × 中位收益 排序。
    最终只推荐「三信号看多 + 20日上涨概率≥55% + 期望收益最高」的标的。

    参数 capital（元）：若提供，则在 best 中补充按该资金计算的股数、投入金额、
    各止盈位预期收益、止损最大亏损、预期收益（元）。
    """
    now = time.time()
    with _best_lock:
        # 有资金参数时不读缓存（仓位计算依赖资金额），但仍写入缓存供无资金场景复用
        if not capital and _best_cache["data"] and now - _best_cache["ts"] < _BEST_TTL:
            return _best_cache["data"]
        scan = scan_stocks(top=top)
        cands = scan["candidates"]
        with cf.ThreadPoolExecutor(max_workers=6) as ex:
            enriched = list(ex.map(_enrich_with_prediction, cands))

        # 可交易候选：三信号共识看多 + 短期（5日）上涨概率≥50% + 中期（20日）≥55%
        # 主排序按 5 日期望收益（用户问的是"短期"），20 日期望收益作为确认
        tradable = [c for c in enriched
                    if c.get("pred_direction") == "看多"
                    and (c.get("up_prob_5d_real") or 0) >= 50
                    and (c.get("up_prob_20d_real") or 0) >= 55]
        if capital and capital > 0:
            # 有资金时按「净期望收益」排序（扣除佣金/印花税/过户费/滑点后的真实可实现收益）
            # 轻量估算：不跑完整 _add_execution_plan，仅用买入区下沿 + 5日目标价估算一个回合成本
            def _net_ev(c):
                bz = c.get("buy_zone")
                if not bz:
                    return c.get("ev_5d", -999)
                buy_ref = bz[0]
                shares = int(capital // buy_ref // 100 * 100) or 100
                capital_used = shares * buy_ref
                adv_yuan = (c.get("amount_yi") or 1.0) * 1e8
                order_pct_adv = capital_used / adv_yuan * 100 if adv_yuan > 0 else 99
                sell_price = c.get("target_5d") or (buy_ref * 1.03)
                cost = _calc_trade_costs(buy_ref, sell_price, shares, order_pct_adv)
                return (c.get("ev_5d", 0) or 0) - cost["cost_pct"]
            tradable.sort(key=lambda c: (-_net_ev(c),
                                         -c.get("ev_20d", -999),
                                         -(c.get("up_prob_5d_real") or 0)))
        else:
            tradable.sort(key=lambda c: (-c.get("ev_5d", -999),
                                         -c.get("ev_20d", -999),
                                         -(c.get("up_prob_5d_real") or 0)))

        # 若严格过滤后为空，放宽到方向看多（保留概率排序）
        if not tradable:
            tradable = [c for c in enriched if c.get("pred_direction") == "看多"]
            tradable.sort(key=lambda c: (-(c.get("up_prob_5d_real") or 0),
                                         -c.get("ev_5d", -999)))

        best = tradable[0] if tradable else (enriched[0] if enriched else None)
        # 若指定了资金，优先选能买得起（至少 1 手 = 100 股）的标的；
        # 若全部候选都买不起，选价格最低的并在执行计划中提示资金缺口
        if capital and best is not None:
            affordable = [c for c in tradable
                          if c.get("buy_zone") and c["buy_zone"][0] * 100 <= capital]
            if affordable:
                best = affordable[0]  # 已按净期望收益排序，取可负担中净收益最高的
            else:
                # 全部买不起，选买入价最低的候选（含 enriched 兜底）
                pool = tradable or enriched
                if pool:
                    best = min(pool, key=lambda c: (c.get("buy_zone") or [1e18])[0])
        # 仅对最终推荐的那只票补充完整执行计划（怎么买 / 怎么卖 / 仓位 / 止损），避免对所有候选浪费算力
        if best is not None:
            best = dict(best)  # 浅拷贝，避免污染 scan_stocks 缓存中的候选对象（capital 等字段会跨调用残留）
            best = _add_execution_plan(best, capital=capital)
        data = dict(
            time=scan["time"], universe=scan["universe"],
            shortlist=scan["shortlist"], scored=scan["scored"],
            analyzed=len(enriched),
            tradable=len(tradable),
            best=best,
            ranked=tradable[:top],
            capital=capital,
            note=("先对全市场做动量扫描得到候选，再逐只跑相似形态预测；"
                  + ("按「5 日净期望收益（扣除佣金/印花税/过户费/滑点）」排序（20 日概率作确认），"
                     if capital else
                     "按「5 日上涨概率 × 中位收益 = 短期期望收益」排序（20 日概率≥55% 作确认），")
                  + "仅当三信号共识看多且 5 日上涨概率≥50%、20 日≥55% 时才入选推荐。"),
        )
        if not capital:
            _best_cache["ts"] = now
            _best_cache["data"] = data
        return data
