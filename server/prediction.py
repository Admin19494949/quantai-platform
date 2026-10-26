# -*- coding: utf-8 -*-
"""盘后预测引擎：相似形态统计法

从个股全历史中找出与当前技术状态最相似的 K 个时点，
用它们此后的真实走势统计：上涨概率、预期涨跌幅度、到顶/见底时点、目标价与风险低点。
"""
import datetime as dt

import numpy as np
import pandas as pd

from . import analysis, datasource as ds


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


FEATURES = ("bias5", "bias20", "mom5", "mom20", "rsi6", "kdj_j", "volr", "macd_sig", "pos20")


def _build_features(dfi):
    c, v = dfi["close"], dfi["volume"]
    f = pd.DataFrame(index=dfi.index)
    f["bias5"] = (c / dfi["ma5"] - 1) * 100
    f["bias20"] = (c / dfi["ma20"] - 1) * 100
    f["mom5"] = c.pct_change(5) * 100
    f["mom20"] = c.pct_change(20) * 100
    f["rsi6"] = dfi["rsi6"]
    f["kdj_j"] = dfi["kdj_j"]
    volr = (v / dfi["vol_ma5"] - 1) * 100
    if float(v.abs().sum()) == 0:  # 外汇等无成交量资产：量比恒0（z-score 后无贡献）
        volr = volr.fillna(0.0)
    f["volr"] = volr
    f["macd_sig"] = (dfi["dif"] - dfi["dea"]) / c * 100
    lo20, hi20 = c.rolling(20).min(), c.rolling(20).max()
    f["pos20"] = (c - lo20) / (hi20 - lo20).replace(0, np.nan) * 100
    return f


def predict(code, name, df):
    """相似形态统计：返回未来 5/10/20 日的上涨概率、预期涨跌与目标价"""
    dfi = analysis.add_indicators(df.copy())
    n = len(dfi)
    feats = _build_features(dfi)
    valid = feats.dropna()
    if len(valid) < 120:
        raise RuntimeError("历史数据不足（上市时间太短），暂无法做相似形态预测")

    z = (valid - valid.mean()) / valid.std().replace(0, 1)
    cur = z.iloc[-1]
    # 候选历史时点：排除最近 30 天（避免与当前行情自相关）
    hist = z.iloc[:-30]
    dist = np.sqrt(((hist - cur) ** 2).sum(axis=1))
    # 只保留"未来至少有20个交易日"的时点
    idx_all = list(z.index)
    max_pos = len(idx_all) - 1 - 20
    dist = dist[[i <= max_pos for i in dist.index]]
    k = min(40, len(dist))
    top = dist.nsmallest(k)

    days = dfi["day"]
    vc = dfi.loc[valid.index, "close"].values
    vd = list(valid.index)
    pos_of = {ix: p for p, ix in enumerate(valid.index)}

    horizons = {}
    for lbl, step in (("5日", 5), ("10日", 10), ("20日", 20)):
        rets, dpeak, dtrough = [], [], []
        for ix in top.index:
            p = pos_of[ix]
            if p + step >= len(vc):
                continue
            base = vc[p]
            fut = vc[p + 1: p + step + 1]
            r = (fut[-1] / base - 1) * 100
            rets.append(r)
            if step == 20:  # 时点统计只用20日窗口
                win = fut
                dpeak.append(int(np.argmax(win)) + 1)
                dtrough.append(int(np.argmin(win)) + 1)
        arr = np.array(rets)
        cur_price = float(dfi["close"].iloc[-1])
        horizons[lbl] = dict(
            up_prob=_r(float((arr > 0).mean() * 100), 1),
            med_ret=_r(float(np.median(arr))),
            avg_ret=_r(float(arr.mean())),
            best=_r(float(arr.max())),
            worst=_r(float(arr.min())),
            target=_r(cur_price * (1 + float(np.median(arr)) / 100)),
            low_pt=_r(cur_price * (1 + float(np.percentile(arr, 25)) / 100)),
        )
    h20 = horizons["20日"]
    if h20["up_prob"] is None:
        raise RuntimeError("相似形态统计计算失败，请稍后重试")

    # ---- 三信号共识投票：相似形态 × 七因子评分 × 市场环境 ----
    # 历史回测结论：单一形态信号在上涨概率 44%~56% 区间无预测力（命中率≈随机 50%），
    # 因此仅当多数信号同向时才给出方向，否则诚实输出"中性·建议观望"。
    def _vote_similar():
        p_, m_ = h20["up_prob"], h20["med_ret"] or 0
        if p_ >= 57 and m_ > 1:
            return "看多", f"相似形态20日上涨概率 {p_}%、中位收益 {m_:+.1f}%"
        if p_ <= 42 or m_ < -3:
            return "看空", f"相似形态20日上涨概率仅 {p_}%、中位收益 {m_:+.1f}%"
        return "中性", f"相似形态20日上涨概率 {p_}%，处于无信息区（形态不具区分度）"

    def _vote_factor():
        try:
            st = analysis.short_term_analysis(code, name, dfi)
        except Exception:
            return None, None
        s_ = st["score"]
        if s_ >= 30:
            return "看多", f"七因子评分 {s_:+d}（{st['direction']}，置信度{st['confidence']}）"
        if s_ <= -30:
            return "看空", f"七因子评分 {s_:+d}（{st['direction']}，置信度{st['confidence']}）"
        return "中性", f"七因子评分 {s_:+d}，信号不明确"

    def _vote_market():
        try:
            idxs = ds.get_index_realtime()
            pcts = [x.get("pct") for x in idxs if x.get("pct") is not None]
            if not pcts:
                return None, None
            avg = sum(pcts) / len(pcts)
        except Exception:
            return None, None
        if avg >= 0.4:
            return "看多", f"市场环境偏强（主要指数平均 {avg:+.2f}%）"
        if avg <= -0.4:
            return "看空", f"市场环境偏弱（主要指数平均 {avg:+.2f}%）"
        return "中性", f"市场环境平淡（主要指数平均 {avg:+.2f}%）"

    votes = []
    for nm, fn in (("相似形态", _vote_similar), ("七因子评分", _vote_factor), ("市场环境", _vote_market)):
        try:
            sig, why = fn()
        except Exception:
            sig, why = None, None
        votes.append(dict(name=nm, signal=sig, detail=why))
    valid_votes = [v for v in votes if v["signal"]]
    bull = sum(1 for v in valid_votes if v["signal"] == "看多")
    bear = sum(1 for v in valid_votes if v["signal"] == "看空")
    nv = len(valid_votes)
    if nv >= 2 and bull == nv:
        direction = "看多"
        confidence = "高（三信号共振）" if nv == 3 else "中（双信号一致）"
    elif nv >= 2 and bear == nv:
        direction = "看空"
        confidence = "高（三信号共振）" if nv == 3 else "中（双信号一致）"
    elif bull >= 2:
        direction, confidence = "看多", "中（多数信号看多）"
    elif bear >= 2:
        direction, confidence = "看空", "中（多数信号看空）"
    else:
        direction = "中性"
        why = "信号分歧" if (bull and bear) else "信号不足"
        confidence = f"低（{why}——历史回测显示此状态下方向不可预测，建议观望）"
    ensemble = dict(
        votes=votes,
        agree=("看多" if bull >= 2 else "看空" if bear >= 2 else "无共识"),
        valid_votes=nv,
    )

    similar = []
    for ix in top.index[:8]:
        p = pos_of[ix]
        base = vc[p]

        def fr(stp):
            return _r((vc[p + stp] / base - 1) * 100) if p + stp < len(vc) else None
        similar.append(dict(date=str(days.loc[ix]), price=_r(base),
                            ret5=fr(5), ret10=fr(10), ret20=fr(20)))

    # ---- 四问增强统计：对每个相似时点取其后20个交易日窗口 ----
    nxt, fu2_hit, fu2_days, fd2_hit, fd2_days = [], [], [], [], []
    mg20, md20 = [], []
    r60 = []
    for ix in top.index:
        p = pos_of[ix]
        if p + 20 >= len(vc):
            continue
        base = vc[p]
        win = vc[p + 1: p + 21]
        nxt.append((win[0] / base - 1) * 100)  # 次日涨跌
        # 窗口内逐日涨跌（相对前收盘）
        prev = np.concatenate([[base], win[:-1]])
        daychg = (win - prev) / prev * 100
        up_idx = np.where(daychg >= 2)[0]
        dn_idx = np.where(daychg <= -2)[0]
        fu2_hit.append(1 if len(up_idx) else 0)
        if len(up_idx):
            fu2_days.append(int(up_idx[0]) + 1)
        fd2_hit.append(1 if len(dn_idx) else 0)
        if len(dn_idx):
            fd2_days.append(int(dn_idx[0]) + 1)
        mg20.append((win.max() / base - 1) * 100)  # 20日内最高价涨幅
        md20.append((win.min() / base - 1) * 100)  # 20日内最低价跌幅
        if p + 60 < len(vc):
            r60.append((vc[p + 60] / base - 1) * 100)

    cur_price = float(dfi["close"].iloc[-1])
    timing = dict(
        next_day_up=_r(float((np.array(nxt) > 0).mean() * 100), 1) if nxt else None,
        next_day_med=_r(float(np.median(nxt))) if nxt else None,
        up2_hit=_r(float(np.mean(fu2_hit) * 100), 1) if fu2_hit else None,
        up2_days_med=_r(float(np.median(fu2_days)), 1) if fu2_days else None,
        dn2_hit=_r(float(np.mean(fd2_hit) * 100), 1) if fd2_hit else None,
        dn2_days_med=_r(float(np.median(fd2_days)), 1) if fd2_days else None,
    )
    amp = dict(
        max_gain_med=_r(float(np.median(mg20))) if mg20 else None,
        max_gain_p75=_r(float(np.percentile(mg20, 75))) if mg20 else None,
        max_dd_med=_r(float(np.median(md20))) if md20 else None,
        max_dd_p25=_r(float(np.percentile(md20, 25))) if md20 else None,
    )
    mid60 = None
    if len(r60) >= 15:
        arr60 = np.array(r60)
        mid60 = dict(up_prob=_r(float((arr60 > 0).mean() * 100), 1),
                     med_ret=_r(float(np.median(arr60))),
                     target=_r(cur_price * (1 + float(np.median(arr60)) / 100)))

    today = dt.datetime.now().strftime("%Y-%m-%d %H:%M")
    return dict(code=code, name=name, date=today, price=_r(cur_price),
                direction=direction, confidence=confidence, ensemble=ensemble,
                similar_count=k, scan_days=len(valid),
                horizons=horizons, similar=similar, timing=timing, amp=amp, mid60=mid60,
                avg_days_peak20=_r(float(np.mean(dpeak)), 1) if dpeak else None,
                avg_days_trough20=_r(float(np.mean(dtrough)), 1) if dtrough else None)
