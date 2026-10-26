# -*- coding: utf-8 -*-
"""妖股研判引擎：按个股量价数据 + 近期上涨因素评估妖股潜力与生命周期位置

妖股六因子：短期爆发力 / 涨停密度 / 连板结构 / 量能异动 / 巨幅震荡 / 趋势突破
评级：妖股(>=75) / 准妖股(>=55) / 妖股胚胎(>=40) / 无妖性
"""
import concurrent.futures as cf
import datetime as dt
import threading
import time

import numpy as np

from . import datasource as ds
from .limitup import limit_price as _limit_price

_cache = {"ts": 0.0, "data": None}
_lock = threading.Lock()
SCAN_TTL = 300
CANDIDATES = 150

DEMON_LEVELS = ((75, "妖股（已成妖）"), (55, "准妖股（妖性十足）"),
                (40, "妖股胚胎（有潜质待启动）"), (0, "无妖性"))


def _norm(v, full_at):
    v = 0 if v is None else v
    return min(1.0, max(0.0, v / full_at)) if full_at else 0.0


def _r(x, n=2):
    if x is None:
        return None
    try:
        x = float(x)
    except (TypeError, ValueError):
        return None
    if np.isnan(x) or np.isinf(x):
        return None
    return round(x, n)


def _pct_hist(code, days, closes):
    """历史每日是否收盘涨停（布尔数组，与 closes 对齐）"""
    out = [False] * len(closes)
    for i in range(1, len(closes)):
        prev = closes[i - 1]
        if prev and prev > 0:
            lp = _limit_price(prev, code, days[i])
            if closes[i] >= lp - 1e-9:
                out[i] = True
    return out


def demon_analysis(code, name, df):
    """对单只股票做妖股六因子评分与生命周期定位"""
    closes = df["close"].tolist()
    opens = df["open"].tolist()
    highs = df["high"].tolist()
    lows = df["low"].tolist()
    amts = (df["close"] * df["volume"]).tolist() if "volume" in df else [0.0] * len(closes)
    days = df["day"].tolist()
    n = len(closes)
    cur = closes[-1]
    prev = closes[-2] if n > 1 else cur
    w = min(20, n - 1)

    def ret(k):
        return (cur / closes[-(k + 1)] - 1) * 100 if n > k else 0.0

    ret5, ret10, ret20 = ret(5), ret(10), ret(20)

    lim = _pct_hist(code, days, closes)[-w:]  # 近20日涨停布尔
    limit_count_20 = sum(lim)
    cur_streak = 0
    for v in reversed(lim):
        if v:
            cur_streak += 1
        else:
            break
    max_streak = run = 0
    for v in lim:
        run = run + 1 if v else 0
        max_streak = max(max_streak, run)
    broke_recent = max_streak >= 3 and cur_streak == 0  # 近期连板后已断板

    amt5 = sum(amts[-5:]) / min(5, n)
    amt20 = sum(amts[-w:]) / max(1, w)
    vol_ratio = amt5 / amt20 if amt20 > 0 else 1.0

    prev_closes = closes[-w:] if n > w else closes
    amp = [abs(highs[-w + i] - lows[-w + i]) / prev_closes[i] * 100 for i in range(len(prev_closes))]
    avg_amp = float(np.mean(amp)) if amp else 0.0

    hist_high = max(closes)
    is_new_high = cur >= hist_high * 0.999
    ma250 = float(np.mean(closes[-250:])) if n >= 250 else None
    dist_ma250 = (cur / ma250 - 1) * 100 if ma250 else None
    ma5 = float(np.mean(closes[-5:])) if n >= 5 else cur
    ma10 = float(np.mean(closes[-10:])) if n >= 10 else cur
    bias5 = (cur / ma5 - 1) * 100

    # ---------------- 六因子打分 ----------------
    factors = []
    s1 = _norm(max(ret20, 0), 100) * 25
    factors.append(dict(name="短期爆发力", got=round(s1, 1), full=25,
                        detail=f"20日涨幅 +{_r(ret20)}%（妖股起步通常30%+，翻倍满分）"))
    s2 = _norm(limit_count_20, 8) * 20
    factors.append(dict(name="涨停密度", got=round(s2, 1), full=20,
                        detail=f"近20日收盘涨停 {limit_count_20} 次（8次满分）"))
    s3 = _norm(max_streak, 5) * 15
    factors.append(dict(name="连板结构", got=round(s3, 1), full=15,
                        detail=f"近20日最长{max_streak}连板，当前{'已断板' if cur_streak == 0 else f'{cur_streak}连板中'}（5连板满分）"))
    s4 = _norm(vol_ratio - 1, 2) * 15
    factors.append(dict(name="量能异动", got=round(s4, 1), full=15,
                        detail=f"5日均额为20日的 {vol_ratio:.2f} 倍（3倍满分）"))
    s5 = _norm(avg_amp - 3, 6) * 10
    factors.append(dict(name="巨幅震荡", got=round(s5, 1), full=10,
                        detail=f"20日日均振幅 {avg_amp:.1f}%（妖股特征：日均振幅9%+）"))
    if dist_ma250 is not None:
        s6 = _norm(dist_ma250, 100) * 15
        detail6 = f"现价偏离年线 +{_r(dist_ma250)}%（偏离100%满分），{'已创历史新高' if is_new_high else '未创历史新高'}"
    else:
        s6 = _norm(is_new_high, 1) * 15
        detail6 = "上市未满一年，以是否创历史新高评估" + ("（创新高满分）" if is_new_high else "（未创新高）")
    factors.append(dict(name="趋势突破", got=round(s6, 1), full=15, detail=detail6))

    score = round(sum(f["got"] for f in factors))
    level = next(t for th, t in DEMON_LEVELS if score >= th)

    # ---------------- 生命周期定位 ----------------
    if cur_streak >= 1:
        if cur_streak >= 6 or ret20 >= 120:
            stage, stage_detail = "高潮期（妖股尾段）", "已处加速末端，随时断板见顶，只宜观察不宜追高"
        elif cur_streak >= 3:
            stage, stage_detail = "加速期（主升浪）", "量价齐升的主升段，妖股空间兑现期，持股可让利润奔跑、破5日线止盈"
        else:
            stage, stage_detail = "启动期（妖股初段）", "刚露出妖相，若后续放量晋级则确认成妖，可轻仓试错"
    else:
        if broke_recent and ret20 >= 80:
            stage, stage_detail = "退潮期（断板分歧）", "连板已断，妖股进入出逃阶段，反抽不创新高即离场信号"
        elif ret20 >= 80:
            stage, stage_detail = "高位震荡（成妖后休整）", "巨大涨幅后横盘消化，二次启动需再度放量涨停"
        elif ret20 >= 40:
            stage, stage_detail = "分歧中继期", "已有一定妖性，回调缩量+再板确认则升级，放量滞涨则退潮"
        else:
            stage, stage_detail = "孕育期（未启动）", "尚无妖股特征，纳入观察池等待首板或放量突破"

    # ---------------- 见顶信号 ----------------
    signals = []
    if vol_ratio >= 2 and ret5 < 3:
        signals.append("巨量滞涨（近5日均额翻倍但涨幅<3%）")
    if broke_recent:
        signals.append(f"高位断板（近20日曾有{max_streak}连板，当前已断）")
    if bias5 > 12:
        signals.append(f"乖离过大（距5日线 +{_r(bias5)}%，短线过热）")
    if n >= 2:
        upper = (highs[-1] - max(opens[-1], closes[-1])) / prev * 100
        if upper > 3 and closes[-1] < opens[-1]:
            signals.append(f"长上影阴线（上影 {_r(upper)}%，尾盘抛压重）")
    if vol_ratio >= 2 and (cur / prev - 1) * 100 < -3:
        signals.append("天量下跌（放量且跌超3%，资金出逃）")
    if is_new_high and limit_count_20 >= 3 and ret20 >= 100:
        signals.append("涨幅透支（20日翻倍+创新高，兑现盘密集）")

    # ---------------- 剩余空间（历史妖股统计基准）----------------
    space_map = {
        "启动期（妖股初段）": "参考历史妖股路径，仍有 60%~150% 潜在空间，行情通常还能走 5~10 个交易日",
        "加速期（主升浪）": "主升段剩余空间约 30%~80%，行情通常还能走 3~6 个交易日",
        "高潮期（妖股尾段）": "剩余空间 0%~20%，随时见顶断板，严禁追高",
        "高位震荡（成妖后休整）": "若无二次启动，剩余空间有限；再度放量涨停可看 20%~50% 续涨",
        "分歧中继期": "再板确认则有 20%~60% 空间；放量滞涨则回归平淡",
        "退潮期（断板分歧）": "妖股寿命基本走完，反抽不创新高即逃命波，勿接飞刀",
        "孕育期（未启动）": "暂无妖股空间可言，等待首板/放量突破再评估",
    }
    trading_days_run = min(20, sum(1 for i in range(max(1, n - 20), n)))
    hist_par = (f"A股历史妖股从首板到见顶平均持续 8~15 个交易日、区间涨幅 100%~200%；"
                f"本股近 {trading_days_run} 个交易日已涨 +{_r(ret20)}%")
    today = dt.datetime.now().strftime("%Y-%m-%d %H:%M")
    return dict(code=code, name=name, date=today, price=cur,
                pct=_r((cur / prev - 1) * 100),
                score=score, level=level, factors=factors,
                stage=stage, stage_detail=stage_detail,
                ret5=_r(ret5), ret10=_r(ret10), ret20=_r(ret20),
                limit_count_20=limit_count_20, cur_streak=cur_streak,
                max_streak=max_streak, vol_ratio=_r(vol_ratio),
                avg_amp=_r(avg_amp), is_new_high=is_new_high,
                dist_ma250=_r(dist_ma250), bias5=_r(bias5),
                top_signals=signals, potential=space_map.get(stage, ""),
                hist_par=hist_par)


def scan_demon(top=8):
    """全市场妖股扫描：今日强势股（涨停或涨幅≥5%、成交额≥5亿）逐只妖股评分"""
    now = time.time()
    with _lock:
        if _cache["data"] and now - _cache["ts"] < SCAN_TTL:
            return _cache["data"]
        snap = ds.get_realtime_snapshot()
        if not snap:
            raise RuntimeError("全市场实时快照尚未就绪，请稍后重试")
        pool = []
        for c, q in snap.items():
            name = q.get("name") or ""
            price, prev = q.get("price") or 0, q.get("prev_close") or 0
            if price <= 0 or prev <= 0 or len(c) != 6 or not c.isdigit():
                continue
            if "ST" in name.upper() or "退" in name:
                continue
            pct = (price / prev - 1) * 100
            amt = q.get("amount") or 0
            if pct >= 5 and amt >= 5e8:
                pool.append((c, name, price, pct))
        if not pool:
            raise RuntimeError("当前无符合条件的强势股")
        pool.sort(key=lambda x: -x[3])
        pool = pool[:CANDIDATES]

        def eval_one(item):
            c, name, price, pct = item
            try:
                df = analysis_build(c, name)
                if df is None or len(df) < 30:
                    return None
                d = demon_analysis(c, name, df)
                d["today_pct"] = round(pct, 2)
                return d
            except Exception:  # noqa: BLE001
                return None

        with cf.ThreadPoolExecutor(max_workers=8) as ex:
            results = [r for r in ex.map(eval_one, pool) if r]
        results.sort(key=lambda x: (-x["score"], -x["ret20"]))
        data = dict(date=dt.datetime.now().strftime("%Y-%m-%d %H:%M"),
                    universe=len(snap), shortlist=len(pool), scored=len(results),
                    candidates=results[:top])
        _cache["ts"] = now
        _cache["data"] = data
        return data


def analysis_build(code, name):
    from . import analysis  # 局部导入避免循环依赖
    return analysis.build_df(ds.get_kline(code))
