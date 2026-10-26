# -*- coding: utf-8 -*-
"""私募经典交易模型扫描引擎

在行情数据（量价+PE/PB/换手率）基础上实现两类私募常用策略：
1. 均值回归（超跌反弹）—— 找超卖/触及布林下轨/短期急跌后企稳的标的
2. 趋势突破 —— 找放量突破20日平台 + 均线多头排列的标的

两类引擎与动量选股(screener)共享实时快照，但评分逻辑独立。
screener 候选会回查本模块的命中条件，打上 strategy_tags，实现多策略共振加权。
"""
import concurrent.futures as cf
import datetime as dt
import threading
import time

import numpy as np

from . import analysis, datasource as ds

MR_TTL = 300      # 均值回归扫描缓存秒数
TB_TTL = 300      # 趋势突破扫描缓存秒数
CANDIDATES = 40   # 深度评分候选数
MIN_AMOUNT_MR = 1.0e8   # 均值回归成交额门槛（超跌股可能放量不大，放宽到1亿）
MIN_AMOUNT_TB = 2.0e8   # 趋势突破成交额门槛

_mr_cache = {"ts": 0.0, "data": None}
_tb_cache = {"ts": 0.0, "data": None}
_mr_lock = threading.Lock()
_tb_lock = threading.Lock()


# ---------------------------------------------------------------- 复用：构建候选池

def _build_pool(min_amount, min_pct=None, max_pct=None):
    """从全市场快照构建初筛池：剔除ST/退市/低成交额，可选涨跌幅区间"""
    snap = ds.get_realtime_snapshot()
    if not snap:
        raise RuntimeError("全市场实时快照尚未就绪，请稍后重试")
    pool = []
    for code, q in snap.items():
        name = q.get("name") or ""
        price, prev = q.get("price") or 0, q.get("prev_close") or 0
        if price <= 0 or prev <= 0 or len(code) != 6 or not code.isdigit():
            continue
        if "ST" in name.upper() or "退" in name:
            continue
        amt = q.get("amount") or 0
        if amt < min_amount:
            continue
        pct = (price / prev - 1) * 100
        if min_pct is not None and pct < min_pct:
            continue
        if max_pct is not None and pct > max_pct:
            continue
        pool.append((code, name, price, pct, amt))
    return snap, pool


def _fundamentals(code):
    """从 stocks 表取 PE/PB/换手率/市值，失败返回 None"""
    try:
        with ds._db() as conn:
            row = conn.execute(
                "SELECT pe,pb,turnover,mktcap FROM stocks WHERE code=?", (code,)
            ).fetchone()
        if not row:
            return None
        pe, pb, turnover, mktcap = row
        return dict(
            pe=float(pe) if pe else None,
            pb=float(pb) if pb else None,
            turnover=float(turnover) if turnover else None,
            mktcap=float(mktcap) if mktcap else None,
        )
    except Exception:
        return None


# ---------------------------------------------------------------- 策略命中判定（供 screener 打标签复用）

def hit_mean_reversion(df, r):
    """给定带指标的 df 与最后一行 r，判定是否符合均值回归（超跌反弹）条件。
    返回 (是否命中, 原因描述)"""
    close = float(r["close"])
    rsi6 = float(r["rsi6"]) if not np.isnan(r["rsi6"]) else 50
    boll_low = float(r["boll_low"]) if not np.isnan(r["boll_low"]) else close
    boll_mid = float(r["boll_mid"]) if not np.isnan(r["boll_mid"]) else close
    c20 = float(df["close"].iloc[-21]) if len(df) > 21 else close
    ret20 = (close / c20 - 1) * 100
    reasons = []
    if rsi6 < 35:
        reasons.append(f"RSI6={rsi6:.0f} 超卖")
    if close <= boll_low * 1.03:
        reasons.append("触及布林下轨")
    if ret20 < -12:
        reasons.append(f"近20日下跌 {ret20:.1f}%")
    if not reasons:
        return False, ""
    # 缩量企稳加分（量比<1.3）
    vol = float(r["volume"])
    vol_ma5 = float(r["vol_ma5"]) if not np.isnan(r["vol_ma5"]) else vol
    volr = vol / vol_ma5 if vol_ma5 > 0 else 1
    steady = "缩量企稳" if volr < 1.3 else "仍在放量"
    return True, "、".join(reasons) + f"（{steady}）"


def hit_trend_breakout(df, r):
    """判定是否符合趋势突破条件。返回 (是否命中, 原因描述)"""
    close = float(r["close"])
    ma5 = float(r["ma5"]) if not np.isnan(r["ma5"]) else close
    ma10 = float(r["ma10"]) if not np.isnan(r["ma10"]) else close
    ma20 = float(r["ma20"]) if not np.isnan(r["ma20"]) else close
    # 突破近20日平台（不含当日）
    high20 = float(df["high"].iloc[-21:-1].max()) if len(df) > 21 else close
    vol = float(r["volume"])
    vol_ma5 = float(r["vol_ma5"]) if not np.isnan(r["vol_ma5"]) else vol
    volr = vol / vol_ma5 if vol_ma5 > 0 else 1
    reasons = []
    if close > high20 * 0.995:
        reasons.append(f"突破近20日平台高点 {high20:.2f}")
    if volr > 1.5:
        reasons.append(f"放量（量比 {volr:.1f}）")
    if ma5 > ma10 > ma20:
        reasons.append("均线多头排列")
    # 至少需突破+放量 两项，或突破+多头
    core = 0
    if close > high20 * 0.995:
        core += 1
    if volr > 1.5:
        core += 1
    if ma5 > ma10 > ma20:
        core += 1
    if core < 2:
        return False, ""
    return True, "、".join(reasons)


# ---------------------------------------------------------------- 均值回归扫描

def _eval_mr(item):
    code, name, price, pct, amt = item
    try:
        df = analysis.build_df(ds.get_kline(code))
        if len(df) < 60:
            return None
        df = analysis.add_indicators(df)
        r = df.iloc[-1]
        hit, reason = hit_mean_reversion(df, r)
        if not hit:
            return None
        close = float(r["close"])
        rsi6 = float(r["rsi6"]) if not np.isnan(r["rsi6"]) else 50
        boll_low = float(r["boll_low"]) if not np.isnan(r["boll_low"]) else close
        boll_mid = float(r["boll_mid"]) if not np.isnan(r["boll_mid"]) else close
        c20 = float(df["close"].iloc[-21])
        ret20 = (close / c20 - 1) * 100
        recent_low = float(df["low"].tail(20).min())
        recent_high = float(df["high"].tail(20).max())
        vol = float(r["volume"])
        vol_ma5 = float(r["vol_ma5"]) if not np.isnan(r["vol_ma5"]) else vol
        volr = round(vol / vol_ma5, 2) if vol_ma5 > 0 else 1.0
        # 价值面
        fund = _fundamentals(code)
        pe = fund["pe"] if fund else None
        pb = fund["pb"] if fund else None
        # 评分：超跌越深分越高 + 缩量企稳加分 + 价值加分
        score = 0
        score += max(0, min(40, -ret20 * 2))          # 超跌程度
        score += max(0, (35 - rsi6) * 0.8)            # 超卖程度
        if volr < 1.3:
            score += 20                                 # 缩量企稳
        if pe and 0 < pe < 50:
            score += 10                                 # 估值合理
        if pb and 0 < pb < 5:
            score += 5
        score = int(round(score))
        # 买入区：现价附近；止损：近20日低点×0.97；目标：布林中轨
        buy_zone = (round(close * 0.99, 2), round(close * 1.01, 2))
        stop_loss = round(recent_low * 0.97, 2)
        target = round(boll_mid, 2)
        risk = round((close - stop_loss) / close * 100, 1)
        reward = round((target - close) / close * 100, 1)
        rr = round(reward / risk, 2) if risk > 0 else None
        return dict(
            code=code, name=name, price=round(price, 2), pct=round(pct, 2),
            amount_yi=round(amt / 1e8, 2),
            score=score, reason=reason,
            rsi6=round(rsi6, 1), ret20=round(ret20, 2), vol_ratio=volr,
            boll_low=round(boll_low, 2), boll_mid=round(boll_mid, 2),
            recent_low=round(recent_low, 2),
            pe=round(pe, 1) if pe else None, pb=round(pb, 1) if pb else None,
            buy_zone=buy_zone, stop_loss=stop_loss, target=target,
            risk=risk, reward=reward, rr=rr,
        )
    except Exception:
        return None


def mean_reversion_scan(top=8):
    """均值回归（超跌反弹）扫描"""
    now = time.time()
    with _mr_lock:
        if _mr_cache["data"] and now - _mr_cache["ts"] < MR_TTL:
            return _mr_cache["data"]
        snap, pool = _build_pool(MIN_AMOUNT_MR)
        # 按跌幅排序优先评估超跌股
        pool.sort(key=lambda x: x[3])
        pool = pool[:CANDIDATES]
        with cf.ThreadPoolExecutor(max_workers=8) as ex:
            results = [r for r in ex.map(_eval_mr, pool) if r]
        results.sort(key=lambda x: -x["score"])
        data = dict(
            strategy="均值回归（超跌反弹）",
            time=dt.datetime.now().strftime("%Y-%m-%d %H:%M"),
            universe=len(snap), shortlist=len(pool), scored=len(results),
            candidates=results[:top],
        )
        _mr_cache["ts"] = now
        _mr_cache["data"] = data
        return data


# ---------------------------------------------------------------- 趋势突破扫描

def _eval_tb(item):
    code, name, price, pct, amt = item
    try:
        df = analysis.build_df(ds.get_kline(code))
        if len(df) < 60:
            return None
        df = analysis.add_indicators(df)
        r = df.iloc[-1]
        hit, reason = hit_trend_breakout(df, r)
        if not hit:
            return None
        close = float(r["close"])
        ma5 = float(r["ma5"])
        ma10 = float(r["ma10"])
        ma20 = float(r["ma20"])
        high20 = float(df["high"].iloc[-21:-1].max()) if len(df) > 21 else close
        vol = float(r["volume"])
        vol_ma5 = float(r["vol_ma5"]) if not np.isnan(r["vol_ma5"]) else vol
        volr = round(vol / vol_ma5, 2) if vol_ma5 > 0 else 1.0
        recent_high = float(df["high"].tail(60).max())
        # 距60日新高空间（越大说明刚启动）
        room60 = round((recent_high / close - 1) * 100, 1) if close > 0 else 0
        fund = _fundamentals(code)
        turnover = fund["turnover"] if fund else None
        # 评分：突破强度 + 放量 + 多头排列 + 距前高空间 + 换手适中
        score = 0
        score += min(30, (close / high20 - 1) * 100 * 5) if high20 > 0 else 0
        score += min(25, (volr - 1) * 10)
        if ma5 > ma10 > ma20:
            score += 20
        if room60 > 5:
            score += 15
        if turnover and 2 < turnover < 15:
            score += 10
        score = int(round(score))
        # 买入区：突破位附近；止损：跌回突破位下方3%；目标：前高+ATR
        buy_zone = (round(high20 * 0.995, 2), round(close * 1.01, 2))
        stop_loss = round(high20 * 0.97, 2)
        atr_val = float(r["atr14"]) if not np.isnan(r["atr14"]) else close * 0.03
        atr = atr_val
        target = round(recent_high + atr, 2)
        risk = round((close - stop_loss) / close * 100, 1)
        reward = round((target - close) / close * 100, 1)
        rr = round(reward / risk, 2) if risk > 0 else None
        return dict(
            code=code, name=name, price=round(price, 2), pct=round(pct, 2),
            amount_yi=round(amt / 1e8, 2),
            score=score, reason=reason,
            high20=round(high20, 2), vol_ratio=volr,
            ma_align="多头" if ma5 > ma10 > ma20 else "非多头",
            room60=room60, turnover=round(turnover, 1) if turnover else None,
            buy_zone=buy_zone, stop_loss=stop_loss, target=target,
            risk=risk, reward=reward, rr=rr,
        )
    except Exception:
        return None


def trend_breakout_scan(top=8):
    """趋势突破扫描"""
    now = time.time()
    with _tb_lock:
        if _tb_cache["data"] and now - _tb_cache["ts"] < TB_TTL:
            return _tb_cache["data"]
        snap, pool = _build_pool(MIN_AMOUNT_TB, min_pct=1.0, max_pct=9.0)
        # 按资金强度排序
        pool.sort(key=lambda x: -(x[4] * x[3]))
        pool = pool[:CANDIDATES]
        with cf.ThreadPoolExecutor(max_workers=8) as ex:
            results = [r for r in ex.map(_eval_tb, pool) if r]
        results.sort(key=lambda x: -x["score"])
        data = dict(
            strategy="趋势突破",
            time=dt.datetime.now().strftime("%Y-%m-%d %H:%M"),
            universe=len(snap), shortlist=len(pool), scored=len(results),
            candidates=results[:top],
        )
        _tb_cache["ts"] = now
        _tb_cache["data"] = data
        return data
