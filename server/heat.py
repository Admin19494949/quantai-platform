# -*- coding: utf-8 -*-
"""市场热度：三大指数 + 市场广度 + 涨停温度 + 量能 → 热度评分"""
import datetime as dt

from . import datasource as ds


def _limit_threshold(code):
    """按板块返回涨停幅度阈值（%）：主板10 / 创业板·科创板20 / 北交所30"""
    if code.startswith(("30", "68")):
        return 19.8
    if code.startswith(("4", "8", "92")):
        return 29.8
    return 9.8


def compute_heat():
    indices = ds.get_index_realtime()
    snap = ds.get_realtime_snapshot()
    if not snap:
        raise RuntimeError("全市场行情快照为空，股票列表可能仍在加载")

    up = down = flat = limit_up = limit_down = 0
    total_amount = 0.0
    gainers, losers = [], []
    for code, q in snap.items():
        price, prev = q["price"], q["prev_close"]
        if price <= 0 or prev <= 0:
            continue
        pct = (price / prev - 1) * 100
        total_amount += q["amount"]
        if pct > 0.001:
            up += 1
        elif pct < -0.001:
            down += 1
        else:
            flat += 1
        thr = _limit_threshold(code)
        if pct >= thr:
            limit_up += 1
        elif pct <= -thr:
            limit_down += 1
        item = dict(code=code, name=q["name"], pct=round(pct, 2), price=price)
        if pct > 0:
            gainers.append(item)
        elif pct < 0:
            losers.append(item)
    gainers.sort(key=lambda x: -x["pct"])
    losers.sort(key=lambda x: x["pct"])

    # 分量评分（0-100）
    idx_pcts = [i["pct"] for i in indices[:3]] or [0]
    idx_avg = sum(idx_pcts) / len(idx_pcts)
    idx_score = max(0, min(100, (idx_avg + 3) / 6 * 100))
    total_ud = up + down
    breadth_score = up / total_ud * 100 if total_ud else 50
    limit_score = limit_up / (limit_up + limit_down) * 100 if (limit_up + limit_down) else 50
    if limit_up + limit_down == 0:
        limit_score = 35  # 无涨停 → 偏冷

    # 成交额对比近5日均值
    today = dt.date.today().isoformat()
    prev_amounts = [r[0] for r in ds._db().execute(
        "SELECT total_amount FROM heat_history WHERE date<? ORDER BY date DESC LIMIT 5", (today,)).fetchall()]
    amount_ratio = (total_amount / (sum(prev_amounts) / len(prev_amounts))) if prev_amounts and sum(prev_amounts) else None
    if amount_ratio:
        amount_score = max(0, min(100, (amount_ratio - 0.6) / 1.0 * 100))
    else:
        amount_score = 50

    score = round(idx_score * 0.25 + breadth_score * 0.30 + limit_score * 0.20 + amount_score * 0.25)
    level = ("冰点" if score < 20 else "偏冷" if score < 40 else
             "中性" if score < 60 else "偏热" if score < 80 else "过热")

    components = [
        dict(name="指数动量", score=round(idx_score), desc=f"三大指数平均涨幅 {idx_avg:+.2f}%"),
        dict(name="市场广度", score=round(breadth_score),
             desc=f"上涨 {up} 家 / 下跌 {down} 家（占比 {breadth_score:.0f}%）"),
        dict(name="涨停温度", score=round(limit_score),
             desc=f"涨停 {limit_up} 家 / 跌停 {limit_down} 家"),
        dict(name="量能活跃", score=round(amount_score),
             desc=(f"两市成交 {total_amount / 1e8:,.0f} 亿，为近5日均值的 {amount_ratio:.2f} 倍"
                   if amount_ratio else f"两市成交 {total_amount / 1e8:,.0f} 亿（历史均值数据积累中）")),
    ]

    # 写入历史（当日覆盖）
    with ds._db_lock:
        ds._db().execute(
            "INSERT INTO heat_history(date,score,up,down,limit_up,limit_down,total_amount)"
            " VALUES(?,?,?,?,?,?,?) ON CONFLICT(date) DO UPDATE SET score=excluded.score,"
            " up=excluded.up, down=excluded.down, limit_up=excluded.limit_up,"
            " limit_down=excluded.limit_down, total_amount=excluded.total_amount",
            (today, score, up, down, limit_up, limit_down, total_amount))
        ds._db().commit()
    history = [dict(date=r[0], score=round(r[1], 1), up=r[2], down=r[3],
                    limit_up=r[4], total_amount=round(r[6] / 1e8, 1))
               for r in ds._db().execute(
                   "SELECT date,score,up,down,limit_up,limit_down,total_amount FROM heat_history ORDER BY date DESC LIMIT 30"
               ).fetchall()][::-1]

    return dict(
        date=today, score=score, level=level,
        indices=indices,
        breadth=dict(up=up, down=down, flat=flat, limit_up=limit_up, limit_down=limit_down,
                     total_amount_yi=round(total_amount / 1e8, 1)),
        components=components,
        history=history,
        top_gainers=gainers[:10],
        top_losers=losers[:10],
    )
