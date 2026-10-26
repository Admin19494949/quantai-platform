# -*- coding: utf-8 -*-
"""实盘信号 + 持仓监控 + 每日盯盘推送

- 目标组合由 portfolio.rank_now() 按最新交易日生成（行业约束 + 仓位上限）
- 用户持仓存于 SQLite 表 holdings，与目标组合对比生成 买入/持有/卖出 动作
- 监控看板给出止损止盈、放量异动、均线破位、集中度等预警
- 每日盯盘可生成文本简报，并支持推送到自定义 Webhook（钉钉/企业微信/飞书机器人）

说明：信号基于日线收盘数据，盘中变化需人工复核；不构成投资建议。
"""
import datetime as dt
import threading
import time

import requests

from . import datasource as ds, market_data, portfolio

SIGNAL_TTL = 300
STOP_LOSS = -0.08        # 默认止损线（相对成本）
TAKE_PROFIT = 0.20       # 默认止盈线
CONCENTRATION_MAX = 0.35 # 单票集中度上限
IND_CONCENTRATION_MAX = 0.45

_cache = {"ts": 0.0, "key": None, "data": None}
_lock = threading.Lock()


# ---------------------------------------------------------------- 持仓存储

def _init_tables():
    conn = ds._db()
    with ds._db_lock:
        conn.execute(
            """CREATE TABLE IF NOT EXISTS holdings(
                code TEXT PRIMARY KEY, name TEXT, shares INTEGER, cost REAL, updated TEXT)"""
        )
        conn.commit()


def list_holdings():
    """返回持仓列表（含最新价与浮动盈亏）"""
    _init_tables()
    rows = ds._db().execute(
        "SELECT code,name,shares,cost,updated FROM holdings ORDER BY code").fetchall()
    snap = ds.get_realtime_snapshot()
    out = []
    for code, name, shares, cost, updated in rows:
        q = snap.get(code) or {}
        price = q.get("price") or 0
        prev = q.get("prev_close") or 0
        mv = price * shares if price else 0
        pnl_pct = ((price / cost - 1) * 100) if cost and price else None
        out.append(dict(
            code=code, name=name or ds.get_stock_name(code) or code,
            shares=int(shares or 0), cost=round(float(cost), 3) if cost else None,
            price=round(price, 2) if price else None,
            prev_close=round(prev, 2) if prev else None,
            pct=round((price / prev - 1) * 100, 2) if price and prev else None,
            market_value=round(mv, 2),
            pnl_pct=round(pnl_pct, 2) if pnl_pct is not None else None,
            pnl_amount=round((price - cost) * shares, 2) if cost and price else None,
            updated=updated,
        ))
    total = sum(x["market_value"] for x in out)
    for x in out:
        x["weight"] = round(x["market_value"] / total * 100, 1) if total else 0.0
    return dict(total_value=round(total, 2), count=len(out), holdings=out)


def set_holdings(items):
    """整体替换持仓。items: [{'code','shares','cost'}, ...]"""
    _init_tables()
    now = dt.datetime.now().isoformat(timespec="seconds")
    conn = ds._db()
    with ds._db_lock:
        conn.execute("DELETE FROM holdings")
        for it in items or []:
            code = ds.norm_code(str(it.get("code", "")))
            if not code:
                continue
            shares = int(it.get("shares") or 0)
            cost = float(it.get("cost") or 0)
            if shares <= 0:
                continue
            conn.execute(
                "INSERT INTO holdings(code,name,shares,cost,updated) VALUES(?,?,?,?,?)",
                (code, ds.get_stock_name(code) or code, shares, cost, now))
        conn.commit()
    _invalidate()
    return list_holdings()


def upsert_holding(code, shares, cost):
    _init_tables()
    code = ds.norm_code(code)
    if not code or shares is None and cost is None:
        return list_holdings()
    now = dt.datetime.now().isoformat(timespec="seconds")
    conn = ds._db()
    with ds._db_lock:
        row = conn.execute("SELECT shares,cost FROM holdings WHERE code=?", (code,)).fetchone()
        new_shares = int(shares) if shares is not None else (row[0] if row else 0)
        new_cost = float(cost) if cost is not None else (row[1] if row else 0)
        if new_shares <= 0:
            conn.execute("DELETE FROM holdings WHERE code=?", (code,))
        else:
            conn.execute(
                "INSERT INTO holdings(code,name,shares,cost,updated) VALUES(?,?,?,?,?)"
                " ON CONFLICT(code) DO UPDATE SET shares=excluded.shares, cost=excluded.cost,"
                " name=excluded.name, updated=excluded.updated",
                (code, ds.get_stock_name(code) or code, new_shares, new_cost, now))
        conn.commit()
    _invalidate()
    return list_holdings()


def remove_holding(code):
    _init_tables()
    code = ds.norm_code(code)
    conn = ds._db()
    with ds._db_lock:
        conn.execute("DELETE FROM holdings WHERE code=?", (code,))
        conn.commit()
    _invalidate()
    return list_holdings()


def _invalidate():
    with _lock:
        _cache["ts"] = 0.0


# ---------------------------------------------------------------- 信号生成

def generate_signals(strategy="factor", max_holdings=10, max_industry_weight=0.35,
                     universe_size=40):
    """对比目标组合与当前持仓，生成可执行动作"""
    key = (strategy, max_holdings, round(max_industry_weight, 3), universe_size)
    now = time.time()
    with _lock:
        if _cache["key"] == key and now - _cache["ts"] < SIGNAL_TTL:
            return _cache["data"]
    rank = portfolio.rank_now(strategy, max_holdings, max_industry_weight, universe_size)
    if rank.get("error"):
        return rank
    target = {t["code"]: t for t in rank["targets"]}
    hold = list_holdings()
    held = {h["code"]: h for h in hold["holdings"]}

    actions = []
    for code, t in target.items():
        h = held.get(code)
        if h:
            actions.append(dict(
                code=code, name=t["name"], industry=t["industry"], action="持有",
                reason=f"仍在目标组合内，评分 {t['score']}，动量20日 {t['ret20']}%",
                price=t["price"], weight=h["weight"], target_weight=t["target_weight"],
                score=t["score"], pnl_pct=h["pnl_pct"],
            ))
        else:
            actions.append(dict(
                code=code, name=t["name"], industry=t["industry"], action="买入",
                reason=f"新进入目标组合（评分 {t['score']}，动量20日 {t['ret20']}%，行业 {t['industry']}）",
                price=t["price"], weight=0.0, target_weight=t["target_weight"],
                score=t["score"], pnl_pct=None,
            ))
    for code, h in held.items():
        if code in target:
            continue
        actions.append(dict(
            code=code, name=h["name"], industry=None, action="卖出",
            reason="已跌出目标组合（评分/信号转弱），建议调出",
            price=h["price"], weight=h["weight"], target_weight=0.0,
            score=None, pnl_pct=h["pnl_pct"],
        ))
    order = {"买入": 0, "卖出": 1, "持有": 2}
    actions.sort(key=lambda x: (order.get(x["action"], 3), -(x["score"] or 0)))

    result = dict(
        date=rank["date"], strategy=strategy,
        target_count=len(target), holdings_count=len(held),
        actions=actions, targets=rank["targets"], scored=rank["scored"],
        universe_count=rank["universe_count"],
        summary=dict(
            buy=len([a for a in actions if a["action"] == "买入"]),
            sell=len([a for a in actions if a["action"] == "卖出"]),
            hold=len([a for a in actions if a["action"] == "持有"]),
        ),
    )
    with _lock:
        _cache["ts"] = now
        _cache["key"] = key
        _cache["data"] = result
    return result


# ---------------------------------------------------------------- 监控预警

def _limit_pct(code):
    if code[:2] in ("30", "68"):
        return 19.5
    if code[0] in ("4", "8") or code[:2] == "92":
        return 29.5
    return 9.5


def monitor():
    """持仓监控 + 异动预警"""
    hold = list_holdings()
    rows = hold["holdings"]
    if not rows:
        return dict(total_value=0, count=0, holdings=[], alerts=[],
                    note="尚未录入持仓，请先在「盯盘」页添加持仓")
    alerts = []
    ind_map = {}
    try:
        ind_map = market_data.get_industry_map()
    except Exception:  # noqa: BLE001
        pass

    for h in rows:
        code, name = h["code"], h["name"]
        pnl = h["pnl_pct"]
        pct = h["pct"]
        if pnl is not None and pnl <= STOP_LOSS * 100:
            alerts.append(dict(code=code, name=name, level="高", type="止损",
                               message=f"浮亏 {pnl}% 已触及止损线 {STOP_LOSS*100:.0f}%"))
        if pnl is not None and pnl >= TAKE_PROFIT * 100:
            alerts.append(dict(code=code, name=name, level="中", type="止盈",
                               message=f"浮盈 {pnl}% 已达止盈线 {TAKE_PROFIT*100:.0f}%，可考虑分批减仓"))
        if pct is not None and abs(pct) >= 7:
            alerts.append(dict(code=code, name=name, level="高" if pct < 0 else "中",
                               type="异动", message=f"当日{'大跌' if pct < 0 else '大涨'} {pct}%"))
        if pct is not None and pct >= _limit_pct(code) - 0.3:
            alerts.append(dict(code=code, name=name, level="中", type="涨停",
                               message="已触及涨停，注意次日溢价与封单变化"))
        # 放量 / 均线破位（用日线）
        try:
            krows = ds.get_kline(code)
            if len(krows) >= 25:
                closes = [r["close"] for r in krows]
                vols = [r["volume"] for r in krows]
                vol_ma5 = sum(vols[-6:-1]) / 5 if len(vols) >= 6 else vols[-1]
                if vol_ma5 > 0 and vols[-1] / vol_ma5 >= 2:
                    alerts.append(dict(code=code, name=name, level="中", type="放量",
                                       message=f"成交量是5日均量的 {vols[-1]/vol_ma5:.1f} 倍"))
                ma20 = sum(closes[-20:]) / 20
                if h["price"] and closes[-1] > ma20 >= h["price"]:
                    alerts.append(dict(code=code, name=name, level="中", type="破位",
                                       message=f"价格跌破20日均线 {ma20:.2f}"))
        except Exception:  # noqa: BLE001
            pass
        if h["weight"] >= CONCENTRATION_MAX * 100:
            alerts.append(dict(code=code, name=name, level="低", type="集中度",
                               message=f"单票权重 {h['weight']}% 超过 {CONCENTRATION_MAX*100:.0f}% 上限"))

    # 行业集中度
    ind_val = {}
    for h in rows:
        ind = ind_map.get(h["code"]) or "其他"
        ind_val[ind] = ind_val.get(ind, 0.0) + h["market_value"]
    total = hold["total_value"] or 1
    for ind, v in sorted(ind_val.items(), key=lambda x: -x[1]):
        w = v / total * 100
        if w >= IND_CONCENTRATION_MAX * 100:
            alerts.append(dict(code="", name=ind, level="中", type="行业集中",
                               message=f"行业「{ind}」权重 {w:.1f}% 超过 {IND_CONCENTRATION_MAX*100:.0f}% 上限"))

    level_order = {"高": 0, "中": 1, "低": 2}
    alerts.sort(key=lambda x: level_order.get(x["level"], 3))
    return dict(total_value=hold["total_value"], count=hold["count"],
                holdings=rows, alerts=alerts,
                alert_summary=dict(
                    high=len([a for a in alerts if a["level"] == "高"]),
                    mid=len([a for a in alerts if a["level"] == "中"]),
                    low=len([a for a in alerts if a["level"] == "低"]),
                ))


# ---------------------------------------------------------------- 每日简报 / 推送

def daily_brief(strategy="factor", top=10):
    """生成每日盯盘文本简报"""
    sig = generate_signals(strategy=strategy, max_holdings=top)
    if sig.get("error"):
        return dict(error=sig["error"])
    mon = monitor()
    lines = [f"## 📮 每日盯盘简报（{sig.get('date') or dt.date.today().isoformat()}）", ""]
    s = sig.get("summary") or {}
    lines.append(f"**目标组合**：{sig.get('target_count')} 只（候选池 {sig.get('universe_count')} 只）")
    lines.append(f"**操作建议**：买入 {s.get('buy',0)} · 卖出 {s.get('sell',0)} · 持有 {s.get('hold',0)}")
    if sig.get("targets"):
        lines.append("")
        lines.append("### 今日目标组合")
        for i, t in enumerate(sig["targets"], 1):
            lines.append(f"{i}. {t['code']} {t['name']}（{t['industry']}）评分 {t['score']}，"
                         f"动量20日 {t['ret20']}%，目标权重 {t['target_weight']}%")
    buys = [a for a in sig.get("actions", []) if a["action"] == "买入"]
    sells = [a for a in sig.get("actions", []) if a["action"] == "卖出"]
    if buys:
        lines.append("")
        lines.append("### 建议买入")
        for a in buys:
            lines.append(f"- {a['code']} {a['name']} @ {a['price']}（{a['reason']}）")
    if sells:
        lines.append("")
        lines.append("### 建议卖出")
        for a in sells:
            lines.append(f"- {a['code']} {a['name']} @ {a['price']}（{a['reason']}）")
    if mon.get("alerts"):
        lines.append("")
        lines.append("### ⚠️ 持仓预警")
        for a in mon["alerts"][:12]:
            lines.append(f"- [{a['level']}] {a['name']}：{a['message']}")
    lines.append("")
    lines.append("> 信号基于日线收盘数据，盘中变化需人工复核；不构成投资建议。")
    text = "\n".join(lines)
    return dict(date=sig.get("date"), text=text, signals=sig, monitor=mon)


def push(text, webhook_url=None):
    """推送到自定义 Webhook（钉钉/企业微信/飞书机器人）

    未配置 Webhook 时仅返回本地简报，不伪造发送成功。
    """
    url = webhook_url or ds._meta_get("push_webhook")
    if not url:
        return dict(ok=False, reason="未配置 Webhook 地址（可在页面填写或设置 meta.push_webhook）",
                    text=text)
    # 兼容钉钉/企业微信/飞书常见文本消息体
    payload = {"msgtype": "text", "text": {"content": text}}
    try:
        r = requests.post(url, json=payload, timeout=12)
        ok = r.status_code == 200
        return dict(ok=ok, status=r.status_code,
                    response=(r.text or "")[:300],
                    reason="" if ok else f"HTTP {r.status_code}")
    except Exception as e:  # noqa: BLE001
        return dict(ok=False, reason=f"推送失败：{e}", text=text)
