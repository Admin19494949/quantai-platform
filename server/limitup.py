# -*- coding: utf-8 -*-
"""涨停连板扫描：识别今日涨停梯队、封板强度（封成比）、历史晋级率与断板预判

核心概念：
- 连板数：连续收盘涨停的天数（按板块阈值 10%/20%/30%，兼容创业板 2020-08-24 注册制改革）
- 封成比：买一封单金额 / 当日成交金额，衡量封板坚决程度（>1 极强，<0.3 烂板）
- 晋级率：历史上该股"涨停次日继续涨停"的频率，用于预测还能走几个板
- 断板预判：封板强度 × 历史晋级率 × 连板高度 → 次日连板概率与断板风险分级
"""
import concurrent.futures as cf
import datetime as dt
import threading
import time

from . import datasource as ds

LIMITUP_TTL = 180      # 扫描结果缓存秒数
DEEP_TOP = 10          # 深度研判的个股数
DEFAULT_PROMO = 0.28   # 全市场经验基准：涨停次日再板概率

_cache = {"ts": 0.0, "data": None}
_lock = threading.Lock()


def limit_ratio(code, day=None):
    """返回该股对应日期的涨停涨幅阈值（小数，如 0.097 表示约 +9.7% 判涨停）"""
    if code[:2] == "30":
        return 0.097 if (day and day < "2020-08-24") else 0.196
    if code[:2] == "68":
        return 0.196
    if code[0] in ("4", "8") or code[:2] == "92":
        return 0.296
    return 0.097


def limit_price(prev_close, code, day=None):
    return round(prev_close * (1 + limit_ratio(code, day)), 2)


def _hist_stats(code, closes, days):
    """历史统计：返回 (当前连板数, 历史涨停次数, 次日晋级率|None, 晋级后平均延伸板数, 最长连板)"""
    n = len(closes)
    is_limit = [False] * n
    for i in range(1, n):
        prev = closes[i - 1]
        if prev <= 0:
            continue
        if closes[i] >= limit_price(prev, code, days[i]) - 1e-6:
            is_limit[i] = True
    cur = 0
    for v in reversed(is_limit):
        if v:
            cur += 1
        else:
            break
    total, promo, extends, max_run, i = 0, 0, [], 0, 1
    while i < n:
        if is_limit[i]:
            j = i
            while j + 1 < n and is_limit[j + 1]:
                j += 1
            run = j - i + 1
            total += 1
            max_run = max(max_run, run)
            if run >= 2:
                promo += 1
                extends.append(run - 1)
            i = j + 1
        else:
            i += 1
    rate = promo / total if total else None
    avg_ext = round(sum(extends) / len(extends), 1) if extends else 0.0
    return cur, total, rate, avg_ext, max_run


def _seal_info(q, code):
    """实时封板信息：返回 (是否封死, 盘中触板, 涨停价, 封单额元, 封成比)"""
    lp = limit_price(q["prev_close"], code)
    price, high = q["price"], q["high"]
    sealed = price >= lp - 1e-6 and (q.get("bid1_price") or 0) >= lp - 1e-6
    touch = high >= lp - 1e-6
    seal_amt = (q.get("bid1_vol") or 0) * (q.get("bid1_price") or 0)
    amount = q.get("amount") or 0
    ratio = round(seal_amt / amount, 3) if amount > 0 else 0.0
    return sealed, touch, lp, seal_amt, ratio


def _forecast(streak, rate, ratio, sealed):
    """预测：返回 (次日连板概率%, 预期高度板, 断板风险, 断板预判文本)"""
    p = rate if rate is not None else DEFAULT_PROMO
    if not sealed:
        p *= 0.35
    elif ratio >= 1.0:
        p = min(0.9, p * 1.25)
    elif ratio < 0.3:
        p *= 0.55
    p = min(0.9, max(0.03, p))
    # 期望延伸板数 E = p + p^2 + ... （截断）
    e, term, h = 0.0, p, 0
    while h < 6 and term > 0.05:
        e += term
        term *= p
        h += 1
    height = streak + round(e)
    if not sealed:
        risk, txt = "极高", "今日盘中已开板未回封（炸板），断板已成事实；明日竞价若低开超2%大概率直接走弱"
    elif ratio >= 1.0:
        risk, txt = "低", "封单金额超过全天成交额，封板极为坚决，短期断板概率低；断板信号看后续放量滞涨"
    elif ratio >= 0.5:
        risk, txt = "中", "封单中等强度，若明日竞价高开2%以内且量能不爆，可继续持有观察1~2个板；放量开板需警惕"
    else:
        risk, txt = "高", "封单薄弱属烂板（封成比不足0.3），若明日竞价平开或低开，开盘冲高不回封即是断板时点"
    return round(p * 100), height, risk, txt


def _eval_one(item):
    code, q, sealed, touch, lp, seal_amt, ratio = item
    try:
        rows = ds.get_kline(code)
        if len(rows) < 30:
            return None
        closes = [r["close"] for r in rows]
        days = [r["day"] for r in rows]
        cur, total, rate, avg_ext, max_run = _hist_stats(code, closes, days)
        prob, height, risk, btxt = _forecast(cur, rate, ratio, sealed)
        return dict(
            code=code, name=q["name"], price=q["price"],
            pct=round((q["price"] / q["prev_close"] - 1) * 100, 2),
            streak=cur, seal_yi=round(seal_amt / 1e8, 2), seal_ratio=ratio,
            amount_yi=round((q.get("amount") or 0) / 1e8, 2),
            broken=not sealed, hist_limits=total, promo_rate=round(rate, 2) if rate is not None else None,
            avg_extend=avg_ext, max_streak=max_run,
            next_day_prob=prob, expected_height=height, break_risk=risk, break_hint=btxt,
        )
    except Exception:  # noqa: BLE001
        return None


def scan_limitup(top=DEEP_TOP):
    """全市场涨停连板扫描。返回 dict(time, universe, limit_up, broken, ladder, stocks)"""
    now = time.time()
    with _lock:
        if _cache["data"] and now - _cache["ts"] < LIMITUP_TTL:
            return _cache["data"]
        snap = ds.get_realtime_snapshot()
        if not snap:
            raise RuntimeError("全市场实时快照尚未就绪，请稍后重试")
        tasks, broken_list = [], []
        for code, q in snap.items():
            price, prev = q.get("price") or 0, q.get("prev_close") or 0
            if price <= 0 or prev <= 0 or len(code) != 6 or not code.isdigit():
                continue
            sealed, touch, lp, seal_amt, ratio = _seal_info(q, code)
            if touch:
                if sealed:
                    tasks.append((code, q, sealed, touch, lp, seal_amt, ratio))
                else:
                    broken_list.append((code, q["name"], round((price / prev - 1) * 100, 2)))
        if not tasks:
            raise RuntimeError("当前市场无涨停股票")
        # 梯队统计需要K线：并发计算
        with cf.ThreadPoolExecutor(max_workers=8) as ex:
            results = [r for r in ex.map(_eval_one, tasks) if r]
        if not results:
            raise RuntimeError("涨停股K线分析失败，请稍后重试")
        results.sort(key=lambda x: (-x["streak"], -x["seal_ratio"]))
        ladder = dict(
            fb=sum(1 for r in results if r["streak"] == 1),
            lb2=sum(1 for r in results if r["streak"] == 2),
            lb3p=sum(1 for r in results if r["streak"] >= 3),
        )
        top_stock = max(results, key=lambda x: (x["streak"], x["seal_ratio"]))
        broken_list.sort(key=lambda x: -x[2])
        data = dict(
            time=dt.datetime.now().strftime("%Y-%m-%d %H:%M"),
            universe=len(snap),
            limit_up=len(results),
            broken=len(broken_list),
            broken_rate=round(len(broken_list) / max(len(results) + len(broken_list), 1) * 100, 1),
            ladder=ladder,
            max_streak=top_stock["streak"],
            max_streak_name=top_stock["name"],
            broken_samples=[dict(code=c, name=n, pct=p) for c, n, p in broken_list[:5]],
            stocks=results[:top],
        )
        _cache["ts"] = now
        _cache["data"] = data
        return data
