# -*- coding: utf-8 -*-
"""自主智能体（Autonomous Agent）

四项能力：
  1) 自主工具调用 + 多步推理：模型自己决定查什么数据，循环取证后作答
  2) 长期记忆 + 个性化：SQLite agent_memory 跨会话保存偏好、结论与主动观察
  3) 主动盯盘 + 自主决策：后台线程定期扫描目标组合与持仓，产生"自主观察"
  4) 自我复核 + 诚实弃权：作答后由审稿模型核对数据一致性，证据不足则降置信/弃权

无 API Key 时不伪造智能：直接返回"未配置模型"的明确提示，由内置规则引擎兜底。
"""
import json
import re
import threading
import time

import requests

from . import (ai_providers, analysis, capital_flow, datasource as ds, demon,
               factors, fundamental as fund, global_mkt, heat, limitup,
               portfolio, risk, screener, signals)

MAX_STEPS = 6
WATCH_INTERVAL = 1800  # 主动盯盘间隔（秒）

_AGENT_SYSTEM = """你是 QuantAI 的自主量化分析师，具备工具调用能力。你必须"先取证、再推理、后复核"，绝不凭空编造。

【输出协议】每轮只输出一个 JSON 对象，不要输出其他文字：
- 需要数据时：{"thought":"为什么需要这项数据","action":"工具名","args":{"code":"600519"}}
- 可以作答时：{"thought":"推理摘要","final":true,"answer":"Markdown 正文","confidence":0.0到1.0,"evidence":["关键数字或工具名"]}

【硬性规则】
1. 只能引用工具真实返回的数字，禁止编造行情、财务、概率。
2. 单轮只调一个工具；最多 6 步，优先调用最能回答问题的 1-3 个工具。
3. 证据不足、工具报错或数据缺失时：confidence ≤ 0.4，并在 answer 中明确写"当前证据不足，无法给出可靠判断"及缺什么数据。
4. answer 结构：结论 → 关键依据（带具体数字）→ 风险提示 → 置信度说明。
5. 不构成投资建议，结尾保留一句风险声明。

【可用工具】
{TOOLS}
"""

_TOOL_TABLE = """1. daily_indicators(code, days=120) — 每日行情核心指标：最新价/涨跌幅/开高低/成交量额、区间涨跌幅与区间高低点、上涨下跌天数、日均量、MA5/10/20/60、MACD、RSI、KDJ、BOLL、量比、ATR、支撑压力、多空信号
2. history_stats(code) — 上市以来全部历史统计（累计涨幅、年化、最大回撤、年度收益等）
3. short_term(code) — 短期涨跌概率、节奏与关键价位
4. fundamental(code) — 估值（PE/PB/市值）与财务（ROE/毛利率/净利率/负债率/增速）
5. factors(code) — 13 个量化因子得分及全市场真实分位
6. capital_flow(code) — 主力资金、北向持股、龙虎榜
7. market_heat() — 市场热度、涨跌家数、情绪与仓位建议
8. global_markets() — 全球市场（美股/港股/指数/商品/外汇）概览
9. limitup_scan() — 涨停与连板梯队、封成比、晋级率
10. demon_scan(code 可选) — 妖股研判或全市场妖股候选扫描
11. stock_pick(strategy) — 多因子/动量/均值回归/趋势突破选股（返回目标持仓）
12. momentum_scan() — 全市场强势动量扫描
13. holdings_monitor() — 当前持仓的止损止盈与异动预警
14. risk_check(codes) — 组合风险：年化波动、VaR、Beta、最大回撤
"""


# ---------------------------------------------------------------- 记忆库

_TABLE_READY = False
_INIT_LOCK = threading.Lock()


def _ensure_table():
    global _TABLE_READY
    if _TABLE_READY:
        return
    with _INIT_LOCK:
        if _TABLE_READY:
            return
        conn = ds._db()  # 必须先于 _db_lock 初始化，否则 _db() 内部再次抢锁会死锁
        with ds._db_lock:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS agent_memory("
                "id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL, "
                "skey TEXT, value TEXT, ts REAL)"
            )
            conn.commit()
        _TABLE_READY = True


def remember(kind, skey, value):
    """写入一条长期记忆"""
    _ensure_table()
    try:
        with ds._db_lock:
            conn = ds._db()
            conn.execute(
                "INSERT INTO agent_memory(kind, skey, value, ts) VALUES(?,?,?,?)",
                (kind, skey or "", json.dumps(value, ensure_ascii=False) if not isinstance(value, str) else value,
                 time.time()),
            )
            conn.commit()
    except Exception as e:  # noqa: BLE001
        print("[agent] remember failed:", str(e)[:120])


def list_memory(limit=30, kind=None):
    _ensure_table()
    sql = "SELECT id, kind, skey, value, ts FROM agent_memory"
    args = []
    if kind:
        sql += " WHERE kind=?"
        args.append(kind)
    sql += " ORDER BY id DESC LIMIT ?"
    args.append(limit)
    rows = ds._db().execute(sql, args).fetchall()
    out = []
    for r in rows:
        try:
            val = json.loads(r[3])
        except Exception:  # noqa: BLE001
            val = r[3]
        out.append(dict(id=r[0], kind=r[1], key=r[2], value=val, ts=r[4]))
    return out


def clear_memory(kind=None):
    _ensure_table()
    with ds._db_lock:
        conn = ds._db()
        if kind:
            conn.execute("DELETE FROM agent_memory WHERE kind=?", (kind,))
        else:
            conn.execute("DELETE FROM agent_memory WHERE kind<>'watch'")
        conn.commit()
    return dict(ok=True)


def _bigrams(text):
    s = re.sub(r"\s+", "", text or "")
    return {s[i:i + 2] for i in range(len(s) - 1)} if len(s) > 1 else {s}


def recall(question, code="", limit=5):
    """按字面相似度 + 同标的加权，召回最相关的历史结论"""
    _ensure_table()
    rows = ds._db().execute(
        "SELECT kind, skey, value, ts FROM agent_memory WHERE kind IN ('conclusion','note') "
        "ORDER BY id DESC LIMIT 120"
    ).fetchall()
    qb = _bigrams(question)
    scored = []
    for kind, skey, value, ts in rows:
        try:
            v = json.loads(value)
        except Exception:  # noqa: BLE001
            v = {"q": str(value)}
        if not isinstance(v, dict):
            v = {"q": str(v)}
        text = str(v.get("q", "")) + " " + str(v.get("a", ""))
        similar = len(qb & _bigrams(text)) / max(1, len(qb | _bigrams(text)))
        if code and (v.get("code") == code or skey == code):
            similar += 0.35
        if similar >= 0.12:
            scored.append((similar, v))
    scored.sort(key=lambda x: -x[0])
    return [dict(question=v.get("q", "")[:80], answer=v.get("a", "")[:240],
                 code=v.get("code", ""), ts=v.get("ts")) for _, v in scored[:limit]]


# ---------------------------------------------------------------- 模型调用

def _parse_json(text):
    if not text:
        return None
    t = text.strip()
    t = re.sub(r"^```(?:json)?", "", t).strip()
    t = re.sub(r"```$", "", t).strip()
    try:
        return json.loads(t)
    except Exception:  # noqa: BLE001
        pass
    i = t.find("{")
    if i < 0:
        return None
    depth = 0
    in_str = False
    esc = False
    for j in range(i, len(t)):
        ch = t[j]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(t[i:j + 1])
                except Exception:  # noqa: BLE001
                    return None
    return None


def _chat(pid, messages, timeout=60, max_tokens=1400):
    """多轮对话（OpenAI 兼容 + Anthropic 协议）"""
    c = ai_providers._pcfg(pid)
    if not c.get("api_key"):
        return None
    try:
        if c["proto"] == "anthropic":
            sys = "\n\n".join(m["content"] for m in messages if m["role"] == "system")
            body = dict(model=c["model"], max_tokens=max_tokens, system=sys,
                        messages=[dict(role=m["role"], content=m["content"])
                                  for m in messages if m["role"] != "system"])
            r = requests.post(c["base_url"].rstrip("/") + "/v1/messages",
                              headers={"x-api-key": c["api_key"],
                                       "anthropic-version": "2023-06-01",
                                       "Content-Type": "application/json"},
                              json=body, timeout=timeout)
            data = r.json()
            return (data.get("content") or [{}])[0].get("text")
        r = requests.post(
            c["base_url"].rstrip("/") + "/chat/completions",
            headers={"Authorization": "Bearer " + c["api_key"], "Content-Type": "application/json"},
            json=dict(model=c["model"], messages=messages, temperature=0.3, max_tokens=max_tokens),
            timeout=timeout,
        )
        data = r.json()
        msg = (data.get("choices") or [{}])[0].get("message") or {}
        return msg.get("content")
    except Exception as e:  # noqa: BLE001
        print(f"[agent] {pid} failed:", str(e)[:160])
        return None


def _pick_model():
    ids = ai_providers.online_ids()
    if not ids:
        return []
    pid = ai_providers.primary_id()
    return ([pid] + [x for x in ids if x != pid])[:2]


# ---------------------------------------------------------------- 工具实现

_DROP_KEYS = {
    "kline", "nav_curve", "bench_curve", "trades", "monthly_avg", "scored",
    "rows", "individual", "dates", "correlation_matrix", "industry_attribution",
    "stock_contribution", "top_correlations", "risk_contribution",
    "risk_parity_weights", "style_exposure", "industry_exposure", "stress_test",
}


def _shrink(obj, depth=0, max_list=8, max_str=600):
    """压缩工具返回值，控制上下文长度"""
    if depth > 4:
        return "..."
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if k in _DROP_KEYS:
                continue
            out[k] = _shrink(v, depth + 1, max_list, max_str)
        return out
    if isinstance(obj, (list, tuple)):
        items = [_shrink(v, depth + 1, max_list, max_str) for v in obj[:max_list]]
        if len(obj) > max_list:
            items.append(f"...共{len(obj)}项")
        return items
    if isinstance(obj, float):
        return round(obj, 3)
    if isinstance(obj, str):
        return obj[:max_str]
    return obj


def _daily(code, days):
    rows = ds.get_kline(code)
    if not rows:
        raise RuntimeError(f"未找到 {code} 的行情数据")
    df = analysis.build_df(rows)
    name = ds.get_stock_name(code)
    d = analysis.daily_analysis(code, name, df)
    r = df.iloc[-1]
    ind = d["indicators"]
    # 与「📊 每日行情」首屏四张卡片完全一致：区间最高/最低、上涨/下跌天数
    seg = df.tail(max(2, min(int(days or 120), len(df))))
    ph = float(seg["high"].max())
    pl = float(seg["low"].min())
    period = dict(
        days=len(seg), high=round(ph, 2), low=round(pl, 2),
        pct=round((float(seg["close"].iloc[-1]) / float(seg["close"].iloc[0]) - 1) * 100, 2),
        amplitude=round((ph / pl - 1) * 100, 2) if pl else None,
        up_days=int((seg["pct"] > 0).sum()), down_days=int((seg["pct"] < 0).sum()),
        avg_volume=round(float(seg["volume"].mean())),
    )
    return _shrink(dict(
        code=code, name=name, date=d["date"], days=days,
        price=ind["close"], pct=ind["pct"],
        open=float(r["open"]), high=float(r["high"]), low=float(r["low"]),
        preclose=float(r["preclose"]) if "preclose" in r else None,
        volume=ind["volume"]["vol"], amount=float(r["amount"]) if "amount" in r else None,
        period=period,
        ma=ind["ma"], macd=ind["macd"], rsi=ind["rsi"], kdj=ind["kdj"],
        boll=ind["boll"], vol_ratio=ind["volume"]["vol_ratio"], atr14=ind["atr14"],
        stats=d["stats"], support_resistance=d["support_resistance"],
        signals=d["signals"],
    ))


def _history(code):
    rows = ds.get_kline(code)
    if not rows:
        raise RuntimeError(f"未找到 {code} 的行情数据")
    df = analysis.build_df(rows)
    return _shrink(analysis.full_history_analysis(code, ds.get_stock_name(code), df))


def _short(code):
    rows = ds.get_kline(code)
    if not rows:
        raise RuntimeError(f"未找到 {code} 的行情数据")
    df = analysis.build_df(rows)
    return _shrink(analysis.short_term_analysis(code, ds.get_stock_name(code), df))


def _run_tool(name, args):
    args = args or {}
    code = str(args.get("code") or "").strip()

    def need_code():
        if not code:
            hit = ds.resolve_stock_in_text(str(args.get("keyword", "")))
            if hit:
                return hit[0]
            raise RuntimeError("缺少 code 参数（6 位股票代码）")
        return ds.norm_code(code)

    if name == "search_stock":
        kw = str(args.get("keyword") or "")
        return dict(results=ds.search_stocks(kw, 8) if kw else [])
    if name == "daily_indicators":
        return _daily(need_code(), int(args.get("days") or 120))
    if name == "history_stats":
        return _history(need_code())
    if name == "short_term":
        return _short(need_code())
    if name == "fundamental":
        return _shrink(fund.get_fundamental(need_code()))
    if name == "factors":
        return _shrink(factors.get_factor_exposure(need_code()))
    if name == "capital_flow":
        return _shrink(capital_flow.get_capital_flow(need_code()))
    if name == "market_heat":
        return _shrink(heat.compute_heat())
    if name == "global_markets":
        return _shrink(global_mkt.overview())
    if name == "limitup_scan":
        return _shrink(limitup.scan_limitup(top=10))
    if name == "demon_scan":
        if code:
            return _shrink(demon.analysis_build(need_code(), ds.get_stock_name(need_code())))
        return _shrink(demon.scan_demon(top=6))
    if name == "stock_pick":
        st = str(args.get("strategy") or "factor")
        if st not in ("factor", "momentum", "mr", "tb", "risk"):
            st = "factor"
        return _shrink(portfolio.rank_now(st, max_holdings=10))
    if name == "momentum_scan":
        return _shrink(screener.scan_stocks(top=8))
    if name == "holdings_monitor":
        return _shrink(signals.monitor())
    if name == "risk_check":
        codes = args.get("codes") or ([code] if code else [])
        if isinstance(codes, str):
            codes = [x.strip() for x in re.split(r"[,，\s]+", codes) if x.strip()]
        if not codes:
            raise RuntimeError("缺少 codes 参数")
        return _shrink(risk.analyze([ds.norm_code(c) for c in codes][:12], days=int(args.get("days") or 250)))
    raise RuntimeError(f"未知工具：{name}")


# ---------------------------------------------------------------- 智能体主循环

def _fallback_text(question):
    return ("### 未启用大模型\n\n"
            "当前没有任何模型配置 API Key，无法进行自主推理。请到「🤖 AI 分析师」页的"
            "「🧠 多模型引擎」面板，为智谱 GLM / DeepSeek / Qwen 等任一模型填入 API Key 后重试。\n\n"
            "在此之前，普通提问仍由**内置量化规则引擎**回答（取消勾选「启用自主推理」即可）。")


def run(question, code="", deep=True, max_steps=MAX_STEPS):
    """执行一次自主推理，返回 answer/steps/confidence/memory/tools"""
    question = (question or "").strip()
    models = _pick_model()
    if not models:
        return dict(answer=_fallback_text(question), steps=[], confidence=0.0,
                    abstained=True, engine="未配置模型", model="", tools=[], memory=[],
                    mode="agent")

    mem = recall(question, code, limit=5)
    targets = ds.search_stocks(code, 1) if code else []
    focus = ""
    if targets:
        focus = f"\n【当前标的】{targets[0]['code']} {targets[0]['name']}"
    mem_txt = ""
    if mem:
        mem_txt = "\n【历史记忆（仅供参考，需用新数据验证）】\n" + "\n".join(
            f"- 曾问：{m['question']}｜曾答：{m['answer']}" for m in mem)

    messages = [
        dict(role="system", content=_AGENT_SYSTEM.replace("{TOOLS}", _TOOL_TABLE)),
        dict(role="user", content=f"用户问题：{question}{focus}{mem_txt}\n\n请开始。"),
    ]

    steps, tools_used, answer, confidence = [], [], "", 0.0
    pid_used = ""
    for i in range(max_steps):
        raw = None
        pid_used = ""
        for pid in models:
            raw = _chat(pid, messages, timeout=60)
            if raw:
                pid_used = pid
                break
        if not raw:
            break
        obj = _parse_json(raw)
        if not obj:
            messages.append(dict(role="assistant", content=raw[:1500]))
            messages.append(dict(role="user", content='格式错误。请只输出一个 JSON 对象，例如 {"thought":"...","final":true,"answer":"...","confidence":0.7}'))
            steps.append(dict(step=i + 1, type="error", text="模型输出格式错误，要求重试"))
            continue

        if obj.get("final"):
            answer = str(obj.get("answer") or "").strip()
            try:
                confidence = float(obj.get("confidence") or 0)
            except Exception:  # noqa: BLE001
                confidence = 0.0
            confidence = max(0.0, min(1.0, confidence))
            steps.append(dict(step=i + 1, type="final",
                              text=str(obj.get("thought") or "形成结论")[:300],
                              confidence=round(confidence, 2)))
            break

        action = str(obj.get("action") or "")
        args = obj.get("args") or {}
        thought = str(obj.get("thought") or "")[:300]
        t0 = time.time()
        try:
            result = _run_tool(action, args)
            ok = True
            err = ""
        except Exception as e:  # noqa: BLE001
            result = dict(error=str(e)[:200])
            ok = False
            err = str(e)[:200]
        cost = round(time.time() - t0, 2)
        tools_used.append(dict(tool=action, args=args, ok=ok, error=err, seconds=cost))
        steps.append(dict(step=i + 1, type="tool", text=thought, tool=action,
                          args=args, ok=ok, seconds=cost,
                          result=json.dumps(_shrink(result), ensure_ascii=False)[:400]))
        messages.append(dict(role="assistant", content=json.dumps(obj, ensure_ascii=False)))
        messages.append(dict(role="user", content="工具 " + action + " 返回："
                             + json.dumps(result, ensure_ascii=False)[:3500]
                             + "\n\n继续：需要更多数据就再调用工具，否则输出 final。"))
    else:
        steps.append(dict(step=max_steps, type="error", text="达到最大步数，使用已有证据作答"))

    if not answer:
        if tools_used:
            answer = ("### 证据已取到，但模型未给出结构化结论\n\n"
                      "以下是本次取到的关键数据，供你自行判断：\n\n"
                      + "\n".join(f"- `{t['tool']}`：{json.dumps(t, ensure_ascii=False)[:160]}"
                                  for t in tools_used))
            confidence = 0.3
        else:
            answer = "模型未能完成推理（网络或配额异常），请稍后重试或检查 API Key 余额。"
            confidence = 0.0

    # ---- 自我复核（可选，deep=True 时执行一次）----
    critique = None
    if deep and answer and tools_used:
        critic_sys = (
            "你是量化报告审稿人。核对下面这份结论是否与工具数据一致、是否编造、是否遗漏关键风险。"
            "只输出 JSON：{\"verdict\":\"ok\"或\"revise\",\"issues\":[\"...\"],\"confidence\":0到1,"
            "\"revised\":\"若需修改，给出完整修订正文；否则空字符串\"}"
        )
        transcript = "\n".join(
            f"[{t['tool']}] {json.dumps(t, ensure_ascii=False)[:500]}" for t in tools_used)
        critic_user = f"问题：{question}\n\n工具数据：\n{transcript}\n\n结论：\n{answer[:2500]}"
        craw = _chat(pid_used or models[0], [dict(role="system", content=critic_sys),
                                             dict(role="user", content=critic_user)],
                     timeout=45, max_tokens=900)
        cobj = _parse_json(craw)
        if cobj:
            issues = cobj.get("issues") or []
            verdict = str(cobj.get("verdict") or "ok")
            try:
                cconf = float(cobj.get("confidence"))
            except Exception:  # noqa: BLE001
                cconf = None
            if cconf is not None:
                confidence = min(confidence, max(0.0, min(1.0, cconf)))
            revised = str(cobj.get("revised") or "").strip()
            if verdict == "revise" and revised:
                answer = revised
            critique = dict(verdict=verdict, issues=[str(x)[:200] for x in issues][:6],
                            confidence=round(confidence, 2))
            steps.append(dict(step=len(steps) + 1, type="review",
                              text="复核：" + ("通过" if verdict == "ok" else "已修订"),
                              issues=critique["issues"], confidence=round(confidence, 2)))

    if confidence < 0.45:
        answer += "\n\n> ⚠️ 置信度较低（" + f"{confidence:.2f}" + "）：证据不足或数据存在缺口，请谨慎对待。"

    # ---- 写入长期记忆 ----
    remember("conclusion", code or "", dict(
        q=question, a=answer[:600], code=code or "", confidence=round(confidence, 2),
        ts=int(time.time())))
    if code:
        remember("note", code, dict(q=question, a=answer[:400], code=code,
                                    confidence=round(confidence, 2), ts=int(time.time())))

    return dict(
        answer=answer, steps=steps, confidence=round(confidence, 2),
        abstained=confidence < 0.45, engine="自主智能体", model=pid_used,
        tools=tools_used, memory=mem, critique=critique, mode="agent",
        disclaimer="以上为量化模型推演，不构成投资建议",
    )


# ---------------------------------------------------------------- 主动盯盘

_watch_state = dict(started=False, last_ts=0, last_text="", last_error="")


def _watch_once():
    """扫描目标组合与持仓，生成自主观察"""
    obs = []
    try:
        rank = portfolio.rank_now("factor", max_holdings=10)
        targets = [t.get("code") for t in (rank.get("targets") or [])]
        prev = ds._meta_get("agent_prev_targets")
        prev_list = json.loads(prev) if prev else []
        added = [c for c in targets if c not in prev_list]
        removed = [c for c in prev_list if c not in targets]
        if prev_list and (added or removed):
            obs.append("目标组合变化：新增 " + ("、".join(added[:8]) or "无")
                       + "；剔除 " + ("、".join(removed[:8]) or "无"))
        if not prev_list and targets:
            obs.append("完成首次目标组合扫描：" + "、".join(targets[:10]))
        ds._meta_set("agent_prev_targets", json.dumps(targets))
    except Exception as e:  # noqa: BLE001
        obs.append("目标组合扫描失败：" + str(e)[:120])

    try:
        mon = signals.monitor()
        high = [a for a in (mon.get("alerts") or []) if str(a.get("level")) == "high"]
        if high:
            brief = "；".join(
                f"{a.get('name') or a.get('code')} {a.get('title') or a.get('type') or ''}"
                f"{(' ' + str(a.get('message') or a.get('msg'))) if (a.get('message') or a.get('msg')) else ''}"
                for a in high[:5])
            obs.append("高优先级持仓预警：" + brief)
    except Exception as e:  # noqa: BLE001
        obs.append("持仓监控失败：" + str(e)[:120])

    text = "；".join(obs) if obs else "本轮无新变化"
    _watch_state.update(last_ts=time.time(), last_text=text, last_error="")
    remember("watch", "auto", dict(text=text, ts=int(time.time())))
    return dict(ok=True, at=time.strftime("%Y-%m-%d %H:%M:%S"), observations=obs)


def _watch_loop(interval):
    time.sleep(45)
    while True:
        try:
            _watch_once()
        except Exception as e:  # noqa: BLE001
            _watch_state["last_error"] = str(e)[:160]
        time.sleep(interval)


def start_background(interval=WATCH_INTERVAL):
    if _watch_state["started"]:
        return
    _watch_state["started"] = True
    threading.Thread(target=_watch_loop, args=(interval,), daemon=True,
                     name="agent-watch").start()


def watch_status(limit=20):
    items = [m for m in list_memory(limit=limit, kind="watch")]
    return dict(started=_watch_state["started"], last_ts=_watch_state["last_ts"],
                last_text=_watch_state["last_text"], last_error=_watch_state["last_error"],
                items=items)
