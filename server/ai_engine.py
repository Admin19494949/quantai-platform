# -*- coding: utf-8 -*-
"""AI 引擎：多模型编排（智谱/DeepSeek/Qwen/Kimi/Claude/GPT 等全球 20 家，
见 ai_providers.py 注册表）生成分析；未配置 Key 或全部失败时，
使用内置量化规则引擎生成专业分析文本。
配置：server/ai_config.json 的 providers 段为任意多家填 api_key 即可。
"""
import json
import os

from . import ai_providers

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CFG_PATH = ai_providers.CFG_PATH

SYSTEM_PROMPT = (
    "你是一名资深A股量化分析师。基于给定的量化指标数据，输出严谨、专业的中文分析。"
    "要求：1) 先给结论；2) 结合指标逐条论证；3) 给出可操作的参考区间（支撑/压力/止损）；"
    "4) 结尾必须用一行提示『以上为量化模型推演，不构成投资建议』。"
    "不要编造数据中没有的信息，语言精炼，用 Markdown 分点。"
)


def _cfg():
    """兼容保留：返回主模型配置（供旧调用点/展示）。"""
    pid = ai_providers.primary_id() or "zhipu"
    c = ai_providers._pcfg(pid)
    return dict(api_key=c["api_key"], base_url=c["base_url"], model=c["model"])


def status():
    st = ai_providers.status()
    return dict(configured=st["configured"], model=st["engine"],
                base_url=f"{st['n_online']} 家模型在线",
                engine=st["engine"],
                collab=st["collab"], primary=st["primary"], models=st["models"])


def ai_chat(user_prompt, system=SYSTEM_PROMPT, timeout=70):
    """多模型编排调用（单模型直答 / 委员会协作+仲裁）；失败返回 None。"""
    try:
        return ai_providers.orchestrate(user_prompt, system, timeout=timeout)
    except Exception as e:  # noqa: BLE001
        print("[ai_engine] orchestrate failed:", e)
        return None


# ================================================================ 内置规则引擎文本


def _fmt(x, suf=""):
    return "—" if x is None else f"{x}{suf}"


def local_daily_text(ctx):
    ind, st, sr = ctx["indicators"], ctx["stats"], ctx["support_resistance"]
    closes = [s["verdict"] for s in ctx["signals"]]
    bull = closes.count("多")
    bear = closes.count("空")
    stance = "偏多" if bull > bear + 1 else "偏空" if bear > bull + 1 else "多空平衡"
    lines = [
        f"### {ctx['name']}（{ctx['code']}）日线量化分析",
        f"**总体判断：{stance}**（{len(closes)} 项信号中 {bull} 项偏多、{bear} 项偏空、{len(closes) - bull - bear} 项中性）",
        "",
        "**一、趋势结构**",
        f"- 现价 {ind['close']}，5日均线 {ind['ma']['ma5']}、10日 {ind['ma']['ma10']}、20日 {ind['ma']['ma20']}、60日 {ind['ma']['ma60']}；",
        f"- 区间表现：近5日 {_fmt(st['pct_5d'], '%')}，近20日 {_fmt(st['pct_20d'], '%')}，近60日 {_fmt(st['pct_60d'], '%')}；近20个交易日中上涨 {st['up_days_20']} 天、下跌 {st['down_days_20']} 天。",
        "",
        "**二、动能与摆动指标**",
        f"- MACD：DIF {ind['macd']['dif']} / DEA {ind['macd']['dea']} / 柱 {ind['macd']['hist']}；",
        f"- RSI：{ind['rsi']['rsi6']} / {ind['rsi']['rsi12']} / {ind['rsi']['rsi24']}；KDJ：K {ind['kdj']['k']}，D {ind['kdj']['d']}，J {ind['kdj']['j']}；",
        f"- 布林带 [{ind['boll']['low']}, {ind['boll']['mid']}, {ind['boll']['up']}]，ATR14={ind['atr14']}（日均波动幅度参考）。",
        "",
        "**三、量价与关键价位**",
        f"- 量比 {ind['volume']['vol_ratio']}（近5日均量 {round(ind['volume']['vol_ma5'] / 1e8, 2) if ind['volume']['vol_ma5'] else '—'} 亿股）；",
        f"- 支撑位 {sr['support']}（20日低点/布林下轨），压力位 {sr['resistance']}（20日高点）。",
        "",
        "**四、操作参考**",
        f"- 短线：站稳支撑位 {sr['support']} 之上可视为强势整理；有效跌破则离场观望；",
        f"- 突破压力位 {sr['resistance']} 且量比 > 1.5 时，可视为趋势加速信号。",
        "",
        "*以上为量化模型推演，不构成投资建议。*",
    ]
    return "\n".join(lines)


def local_full_text(ctx):
    rm = ctx["return_metrics"]
    yearly = ctx["yearly"][-5:]
    ys = "、".join(f"{y['year']}年 {y['ret']:+.1f}%" for y in yearly)
    ma = [m for m in ctx["monthly_avg"] if m is not None]
    best_m = (ma.index(max(ma)) + 1) if ma else None
    worst_m = (ma.index(min(ma)) if ma else None)
    lines = [
        f"### {ctx['name']}（{ctx['code']}）上市以来全生命周期分析",
        f"该股于 **{ctx['ipo_date']}** 上市，至今交易 **{ctx['total_days']} 个交易日**（约 {ctx['years']} 年）。",
        "",
        "**一、长期收益与风险**",
        f"- 累计收益 {_fmt(rm['total_return'], '%')}，年化收益 {_fmt(rm['cagr'], '%')}，年化波动率 {_fmt(rm['annual_volatility'], '%')}，夏普比率 {_fmt(rm['sharpe'])}；",
        f"- 最大回撤 **{_fmt(rm['max_drawdown'], '%')}**（{rm['max_dd_start']} → {rm['max_dd_end']}），是衡量该股历史最深亏损的核心风险指标；",
        f"- 日胜率 {_fmt(rm['daily_win_rate'], '%')}：全部交易日中 {rm['up_days']} 天上涨、{rm['down_days']} 天下跌。",
        "",
        "**二、年度与季节规律**",
        f"- 近年表现：{ys}；",
        f"- 月度季节性：历史上 {'%d月' % best_m if best_m else '—'} 平均表现最好，{'%d月' % (worst_m + 1) if worst_m is not None else '—'} 平均表现最弱（统计均值，仅供季节性参考）。",
        "",
        "**三、极端行情**",
        f"- 单日最大涨幅 {ctx['best_worst']['best_day']['pct']:+}%（{ctx['best_worst']['best_day']['date']}）；",
        f"- 单日最大跌幅 {ctx['best_worst']['worst_day']['pct']:+}%（{ctx['best_worst']['worst_day']['date']}）。",
        "",
        "**四、结论**",
        f"- 该股历史年化 {_fmt(rm['cagr'], '%')} 搭配 {_fmt(rm['annual_volatility'], '%')} 的波动，属于{'高弹性' if (rm['annual_volatility'] or 0) > 40 else '中等波动' if (rm['annual_volatility'] or 0) > 25 else '低波动'}资产；历史上 {_fmt(rm['max_drawdown'], '%')} 的最大回撤提示需控制长期持仓仓位。",
        "- 注：历史行情为交易所原始价格（不复权），长期累计收益受历史除权除息影响，仅供量化统计参考。",
        "",
        "*以上为量化模型推演，不构成投资建议。*",
    ]
    return "\n".join(lines)


def local_short_text(ctx):
    f = ctx["factors"]
    pos = [x for x in f if x["score"] > 0]
    neg = [x for x in f if x["score"] < 0]
    lines = [
        f"### {ctx['name']}（{ctx['code']}）短期涨跌推演（{ctx['date']}）",
        f"**综合评分：{ctx['score']:+d} → {ctx['direction']}（置信度：{ctx['confidence']}）**",
        f"- 未来5日上涨概率模型估计 **{ctx['up_prob_5d']}%**；",
        f"- 基于ATR的5日波动区间参考：{ctx['expected_range'][0]} ~ {ctx['expected_range'][1]}。",
        "",
        "**多头因子：** " + ("；".join(f"{x['name']}（{x['weighted']:+.0f}分，{x['detail']}）" for x in pos) if pos else "无"),
        "",
        "**空头因子：** " + ("；".join(f"{x['name']}（{x['weighted']:+.0f}分，{x['detail']}）" for x in neg) if neg else "无"),
        "",
        "**关键提示**" if abs(ctx["score"]) >= 60 else "**中性提示**",
        f"- 7大因子加权后 {'共振' if abs(ctx['score']) >= 60 else '存在分歧'}，评分 {ctx['score']:+d}。"
        + ("多因子同向共振时，顺势而为的胜率历史上更高，但仍需设定止损。" if ctx["score"] >= 60
           else "空头共振阶段建议规避或对冲，勿盲目抄底。" if ctx["score"] <= -60
           else "信号矛盾阶段以观望为主，等待方向选择。"),
        "",
        "*以上为量化模型推演，不构成投资建议。*",
    ]
    return "\n".join(lines)


def local_heat_text(ctx):
    b = ctx["breadth"]
    lines = [
        f"### 市场热度分析（{ctx['date']}）",
        f"**综合热度：{ctx['score']}/100 → {ctx['level']}**",
        "",
        "**市场概览**",
        f"- 三大指数：" + "，".join(f"{i['name']} {i['pct']:+.2f}%" for i in ctx["indices"][:3]) + "；",
        f"- 赚钱效应：上涨 {b['up']} 家 vs 下跌 {b['down']} 家，涨停 {b['limit_up']} 家、跌停 {b['limit_down']} 家；",
        f"- 两市成交额 {b['total_amount_yi']:,.0f} 亿元。",
        "",
        "**分项解读**",
    ]
    for c in ctx["components"]:
        bar = "█" * int(c["score"] // 10) + "░" * (10 - int(c["score"] // 10))
        lines.append(f"- {c['name']} {bar} {c['score']}分 —— {c['desc']}")
    tg = "、".join(f"{g['name']} {g['pct']:+.1f}%" for g in ctx["top_gainers"][:5])
    lines += ["", f"**涨幅榜前列：** {tg}",
              "", "**策略基调：** "
              + ("过热阶段控制追高冲动，注意兑现节奏。" if ctx["level"] == "过热"
                 else "偏热环境可积极参与主线题材，注意轮动。" if ctx["level"] == "偏热"
                 else "中性市以结构性行情为主，精选个股。" if ctx["level"] == "中性"
                 else "偏冷阶段降低仓位、防守为主。" if ctx["level"] == "偏冷"
                 else "冰点行情往往是中长期布局的观察窗口，但需等待右侧信号。"),
              "", "*以上为量化模型推演，不构成投资建议。*"]
    return "\n".join(lines)


# ================================================================ LLM Prompt 构建


def _ctx_json(obj):
    return json.dumps(obj, ensure_ascii=False, default=str)


def prompt_daily(ctx):
    slim = dict(name=ctx["name"], code=ctx["code"], date=ctx["date"],
                indicators=ctx["indicators"], stats=ctx["stats"],
                support_resistance=ctx["support_resistance"],
                signals=ctx["signals"])
    return ("请基于以下量化数据撰写该股票当日交易数据的深度分析研报：\n" + _ctx_json(slim))


def prompt_full(ctx):
    slim = dict(name=ctx["name"], code=ctx["code"], ipo_date=ctx["ipo_date"],
                total_days=ctx["total_days"], years=ctx["years"],
                return_metrics=ctx["return_metrics"], yearly=ctx["yearly"][-15:],
                monthly_avg=ctx["monthly_avg"], best_worst=ctx["best_worst"])
    return ("请基于该股票上市以来全部历史数据的量化统计，撰写一份全生命周期分析报告（长期收益、风险回撤、季节性规律、历史极端行情与长期配置启示）：\n" + _ctx_json(slim))


def prompt_short(ctx):
    slim = dict(name=ctx["name"], code=ctx["code"], date=ctx["date"], score=ctx["score"],
                direction=ctx["direction"], up_prob_5d=ctx["up_prob_5d"],
                expected_range=ctx["expected_range"], factors=ctx["factors"])
    return ("请基于以下多因子模型输出，撰写短期（未来5个交易日）涨跌推演报告，论证多空因素并给出交易参考：\n" + _ctx_json(slim))


def prompt_heat(ctx):
    slim = dict(date=ctx["date"], score=ctx["score"], level=ctx["level"],
                indices=ctx["indices"], breadth=ctx["breadth"],
                components=ctx["components"], top_gainers=ctx["top_gainers"][:5],
                top_losers=ctx["top_losers"][:5])
    return ("请基于以下全市场量化数据，撰写当日市场热度分析（情绪周期位置、赚钱效应、资金活跃度与仓位建议）：\n" + _ctx_json(slim))


def prompt_ask(question, stock_ctx=None, heat_ctx=None):
    parts = [f"用户提问：{question}"]
    if stock_ctx:
        parts.append("当前关注股票的量化上下文：\n" + _ctx_json(stock_ctx))
    if heat_ctx:
        slim = dict(score=heat_ctx["score"], level=heat_ctx["level"], breadth=heat_ctx["breadth"],
                    indices=heat_ctx["indices"])
        parts.append("当前市场热度上下文：\n" + _ctx_json(slim))
    return "\n\n".join(parts)


# ================================================================ 全市场选股扫描


def local_scan_text(question, scan, heat_ctx=None):
    cands = scan["candidates"]
    lines = [
        f"### 全市场智能选股（{scan['time']}）",
        f"**针对问题：** {question}",
        "",
        "**筛选过程**",
        f"- 扫描范围：A股全市场 {scan['universe']:,} 只；",
        f"- 初筛：剔除 ST/退市整理、成交额低于 1.5 亿、当日涨幅不足 1.5% 或已封板买不进的标的，剩余 {scan['shortlist']} 只；",
        f"- 对初筛股票逐只计算七因子短期评分（均线/MACD/RSI/KDJ/动量/量价/区间位置），按综合得分取前 {len(cands)} 名。",
        "",
    ]
    # ---- 🎯 首选推荐（综合评分最高的看多标的）----
    longs = [c for c in cands if c["direction"] in ("看多", "偏多")]
    top = (longs[0] if longs else cands[0]) if cands else None
    if top:
        bz = top["buy_zone"]
        reward = round((top["expected_range"][1] - top["price"]) / top["price"] * 100, 1)
        risk = round((top["price"] - top["stop_loss"]) / top["price"] * 100, 1)
        rr = round(reward / risk, 2) if risk > 0 else None
        lines += [
            "### 🎯 首选推荐（综合评分第一）",
            "",
            f"**{top['name']}（{top['code']}）**  · 现价 {top['price']} 元 · 今日 +{top['pct']}% · 成交额 {top['amount_yi']} 亿",
            "",
            f"- **综合评分**：{top['score']:+d}（{top['direction']}，置信度 {top['confidence']}），在 {len(cands)} 只候选中排名第 1；",
            f"- **核心驱动**：{top['top_factor']} —— {top['top_factor_detail']}；",
            f"- **买入参考区**：{bz[0]} ~ {bz[1]} 元（5 日均线 {top['ma5']} 附近，回调不破可轻仓介入）；",
        ]
        if top["pct"] >= 5:
            lines.append(
                f"- **止损位**：{top['stop_loss']} 元（强势启动股紧止损：5 日均线 ×0.97，单笔风险 ≈ {risk}%）；"
            )
        else:
            lines.append(
                f"- **止损位**：{top['stop_loss']} 元（跌破 10 日均线 {top['ma10']} 或近 20 日低点 {top['recent_low']} 即离场，单笔风险 ≈ {risk}%）；"
            )
        lines.append(
            f"- **目标位**：5 日波动上沿 {top['expected_range'][1]} 元，潜在收益 ≈ {reward}%，**盈亏比 {rr or '—'}:1**；"
        )
        lines.append(
            f"- **持续性**：当前已连涨 {top['streak_up']} 天，历史平均连涨 {top['avg_streak']} 天（最长 {top['max_streak']} 天），5 日上涨概率 {top['up_prob_5d']}%；"
        )
        lines.append("")
        lines.append("**操作建议**：参考买入区分批建仓，跌破止损位无条件离场；若放量滞涨（量增价平）或市场热度跌回 40 以下，止盈出局。")
        lines.append("")
    if heat_ctx:
        b = heat_ctx["breadth"]
        lines += [
            "**市场环境**",
            f"- 市场热度 {heat_ctx['score']}/100（{heat_ctx['level']}）；上涨 {b['up']} 家 / 下跌 {b['down']} 家，涨停 {b['limit_up']} 家；",
            f"- 两市成交额 {b['total_amount_yi']:,.0f} 亿，"
            + ("热度较高环境下动量策略胜率提升，但需警惕高位分化。" if heat_ctx["score"] >= 60
               else "中性偏弱环境下强者恒强的分化格局更明显，宜精选龙头。" if heat_ctx["score"] >= 40
               else "弱势环境下连涨持续性普遍打折，建议轻仓试错、快进快出。"),
            "",
        ]
    lines += [
        "**候选榜单**",
        "",
        "| 股票 | 现价 | 今日涨幅 | 成交额 | 评分(共振) | 方向 | 策略标签 | 5日上涨概率 |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for c in cands:
        tags = "·".join(c.get("strategy_tags", ["动量"]))
        sc = c.get("boosted_score", c["score"])
        lines.append(
            f"| {c['name']} {c['code']} | {c['price']} | +{c['pct']}% | {c['amount_yi']}亿 "
            f"| {sc:+d} | {c['direction']} | {tags} | {c['up_prob_5d']}% |"
        )
    lines += ["", "**逐只点评（上涨驱动与持续性）**", ""]
    for c in cands:
        lo, hi = c["expected_range"]
        cont = (
            f"历史上平均一轮连涨持续 {c['avg_streak']} 个交易日（最长 {c['max_streak']} 天），当前已连涨 {c['streak_up']} 天；"
            if c["streak_up"] > 0 else
            f"今日刚转强（此前未连涨），历史上该股平均连涨一轮 {c['avg_streak']} 天（最长 {c['max_streak']} 天）；"
        )
        lines.append(
            f"- **{c['name']}（{c['code']}）** 现价 {c['price']}，今日 +{c['pct']}%，成交额 {c['amount_yi']} 亿，近5日 +{c['ret5']}%、近20日 +{c['ret20']}%。"
            f"七因子评分 {c['score']:+d}（{c['direction']}，置信度{c['confidence']}），核心驱动为「{c['top_factor']}」：{c['top_factor_detail']}。"
            f"{cont}模型估计未来5日上涨概率 {c['up_prob_5d']}%，若延续强势，5日参考波动区间 {lo}~{hi}。"
        )
    lines += [
        "",
        "**上涨持续多久（结论）**",
        "- A股历史统计中，单轮连涨行情中位持续 2~4 个交易日；上述候选多为资金强度排名靠前的动量标的，"
        "若量能不衰减，通常存在 1~3 个交易日的惯性冲高窗口；",
        "- 观察信号：跌破 5 日均线、放量滞涨（量增价平）、或市场热度骤降（跌回 40 以下）时，动量大概率终结，应及时离场。",
        "",
        f"*以上为量化引擎对全市场 {scan['universe']:,} 只股票实时扫描的结果，仅供研究参考，不构成投资建议。*",
    ]
    return "\n".join(lines)


def prompt_scan(question, scan, heat_ctx=None):
    slim = dict(time=scan["time"], universe=scan["universe"],
                shortlist=scan["shortlist"], candidates=scan["candidates"])
    parts = [
        f"用户提问：{question}",
        "以下是量化引擎刚完成的全市场动量选股扫描结果。"
        "初筛规则：剔除ST/退市整理/成交额低于1.5亿/当日涨幅低于1.5%/已封板，按资金强度（成交额×涨幅）取前40只，"
        "逐只计算七因子短期评分后按得分排序：\n" + _ctx_json(slim),
    ]
    if heat_ctx:
        slim_h = dict(score=heat_ctx["score"], level=heat_ctx["level"],
                      breadth=heat_ctx["breadth"], indices=heat_ctx["indices"])
        parts.append("当前市场热度上下文：\n" + _ctx_json(slim_h))
    parts.append(
        "请据此回答用户问题：1) 明确给出**首选推荐的一只股票**（综合评分最高且方向看多者），"
        "说明现价、综合评分、核心驱动因子、参考买入区（5日均线附近）、止损位（10日均线或近20日低点）、"
        "目标位（5日波动上沿）与盈亏比；2) 列出其余候选榜单及简评；"
        "3) 明确回答『上涨可能持续多久』，引用候选的历史平均连涨天数、当前连涨天数与5日上涨概率；"
        "4) 给出失效信号（如跌破止损位、放量滞涨）；5) 结尾附风险提示。"
    )
    return "\n\n".join(parts)


# ================================================================ 短期最佳推荐（概率+收益双维度）


def local_bestpick_text(question, bp, heat_ctx=None):
    """深度最佳推荐：动量扫描候选 + 相似形态预测，按期望收益选最可能赚钱的标的"""
    best = bp.get("best")
    lines = [
        f"### 短期最佳推荐 · 概率与收益双维度排序（{bp['time']}）",
        f"**针对问题：** {question}",
        "",
        "**分析流程（两步筛选，确保概率最高）**",
        f"- 第一步：扫描全市场 {bp['universe']:,} 只股票，剔除 ST/退市/成交额不足/已封板，"
        f"按资金强度取前 {bp['shortlist']} 只，逐只跑七因子评分，得到 {bp['scored']} 只动量候选；",
        f"- 第二步：对 {bp['analyzed']} 只候选逐只跑「相似形态预测」（在自身历史中找 k-NN 相似走势，"
        "统计其后 5/10/20 日的真实上涨概率与中位收益），再用「5 日上涨概率 × 中位收益 = 短期期望收益」排序（20 日概率作确认）；",
        f"- 仅保留「三信号共识看多 且 5 日上涨概率 ≥ 50%、20 日 ≥ 55%」的标的，最终入选 {bp['tradable']} 只；",
        "",
    ]
    if not best:
        lines.append("**当前市场暂无同时满足「三信号看多 + 历史上涨概率≥55%」的标的，建议观望。**")
        lines.append("")
    else:
        bz = best["buy_zone"]
        up5 = best.get("up_prob_5d_real")
        up20 = best.get("up_prob_20d_real")
        med5 = best.get("med_ret_5d")
        med20 = best.get("med_ret_20d")
        tgt = best.get("target_20d")
        lo = best.get("low_pt_20d")
        sl = best["stop_loss"]
        price = best["price"]
        reward = round((tgt - price) / price * 100, 1) if tgt else None
        risk = round((price - sl) / price * 100, 1)
        rr = round(reward / risk, 2) if (reward and risk and risk > 0) else None
        # 三信号明细
        ens = best.get("ensemble_agree", "—")
        lines += [
            "### 🎯 短期最可能赚钱的标的（期望收益第一）",
            "",
            f"**{best['name']}（{best['code']}）**  · 现价 {price} 元 · 今日 +{best['pct']}% · 成交额 {best['amount_yi']} 亿",
            "",
            "**概率证据（来自该股历史相似形态的真实统计，非拍脑袋）**",
        ]
        if up5 is not None:
            lines.append(f"- **5 日上涨概率 {up5}%**，相似形态中位收益 {med5:+.1f}%；")
        if up20 is not None:
            lines.append(f"- **20 日上涨概率 {up20}%**，相似形态中位收益 {med20:+.1f}%，"
                         f"20 日目标价 {tgt} 元（潜在收益 {reward}%）；")
        lines.append(
            f"- **短期期望收益（Kelly 分数）**：{best.get('ev_5d')} = {up5}% × {med5}%（5 日主排序），"
            f"20 日期望收益 {best.get('ev_20d')} 作中期确认，在所有候选中排名第 1；"
        )
        lines.append(f"- **三信号共识**：相似形态 + 七因子评分 + 市场环境 → **{ens}**"
                     f"（置信度 {best.get('pred_confidence', '—')}）；")
        # 振幅/回撤
        mg = best.get("amp_max_gain_med")
        md = best.get("amp_max_dd_med")
        if mg is not None and md is not None:
            lines.append(f"- **历史波动特征**：相似形态 20 日内最大涨幅中位 {mg:+.1f}%、最大回撤中位 {md:+.1f}%；")
        lines += [
            "",
            "**操作计划**",
            f"- **参考买入区**：{bz[0]} ~ {bz[1]} 元（5 日均线 {best['ma5']} 附近，回调不破可介入）；",
            f"- **止损位**：{sl} 元（单笔风险 ≈ {risk}%）；",
        ]
        if reward is not None:
            lines.append(f"- **目标位**：{tgt} 元（20 日相似形态中位目标），潜在收益 ≈ {reward}%，**盈亏比 {rr or '—'}:1**；")
        # ---- 明天买入计划（次日执行口径：预估开盘价 / 买入区间 / 可成交性） ----
        ndp = best.get("next_day_plan")
        if ndp:
            lines.append("")
            lines.append("**📅 明天买入计划（次日执行口径）**")
            lines.append(
                f"- **预估次日开盘价**：{ndp['open_est']} 元（基于该股历史 {ndp['n_gap']} 个交易日的"
                f"次日开盘跳空分布：中位 {ndp['gap_med']:+.2f}%、25 分位 {ndp['gap_p25']:+.2f}%、"
                f"75 分位 {ndp['gap_p75']:+.2f}%）；"
            )
            lines.append(
                f"- **次日买入价区间**：{ndp['entry_low']} ~ {ndp['entry_high']} 元"
                f"（买入区按跳空分布调整后）；"
            )
            if ndp["can_buy_tomorrow"]:
                lines.append("- **次日可成交性**：✓ 未接近涨停板，次日限价单大概率可成交。")
            else:
                lines.append("- **次日可成交性**：⚠️ " + ndp["tomorrow_note"].split("。")[0] + "。")
            lines.append(f"- **执行建议**：{ndp['tomorrow_note']}")
        # ---- 怎么样买入 ----
        buy_method = best.get("buy_method") or []
        if buy_method:
            lines.append("")
            lines.append("**🛒 怎么样买入（执行方法）**")
            for m in buy_method:
                lines.append(f"- {m}")
        # ---- 仓位建议 ----
        pk = best.get("position_kelly")
        ps = best.get("position_score")
        if pk or ps:
            lines.append("")
            lines.append("**💰 仓位建议**")
            if pk:
                lines.append(f"- Kelly 仓位（数据驱动）：{pk}")
            if ps:
                lines.append(f"- 七因子评分仓位：{ps}")
            lines.append("- 取两者较保守者作为实际仓位上限，单票仓位不超过总资金 50%。")
        # ---- 怎么卖出 ----
        sell_method = best.get("sell_method") or []
        if sell_method:
            lines.append("")
            lines.append("**📤 怎么卖出（执行方法）**")
            for m in sell_method:
                lines.append(f"- {m}")
        # ---- 资金分配（按用户指定金额计算） ----
        cap = best.get("capital")
        if cap:
            amt = cap["amount"]
            shares = cap["shares"]
            used = cap["capital_used"]
            pos_pct = cap["position_pct"]
            insufficient = cap.get("insufficient", False)
            shortfall = cap.get("shortfall")
            lines.append("")
            lines.append(f"**💵 资金分配（以 {amt:,} 元为例）**")
            if insufficient:
                lines.append(
                    f"- ⚠️ **资金不足**：该股 1 手（100 股）需 {used:,.0f} 元，"
                    f"当前资金还差 **{shortfall:,.0f} 元**。以下按 1 手测算收益供参考，"
                    f"实际可考虑价格更低的标的或增加资金。"
                )
            else:
                lines.append(
                    f"- 按参考买入价 {cap['buy_ref']} 元（买入区下沿），可买 **{shares} 股**"
                    f"（{shares // 100} 手），实际投入 **{used:,.0f} 元**（占资金 {pos_pct}%）。"
                )
            # 预期收益（毛收益 vs 净收益：净收益已扣除佣金/印花税/过户费/滑点）
            ev5 = cap.get("expected_profit_5d")
            ev20 = cap.get("expected_profit_20d")
            net_ev5 = cap.get("net_expected_profit_5d")
            net_ev20 = cap.get("net_expected_profit_20d")
            cost_pct = cap.get("cost_pct", 0)
            lines.append(
                f"- **预期能赚多少（毛 vs 净）**："
                f"5 日期望收益 毛 **{ev5:+,.0f} 元** / 净 **{net_ev5:+,.0f} 元**，"
                f"20 日期望收益 毛 **{ev20:+,.0f} 元** / 净 **{net_ev20:+,.0f} 元**；"
                f"交易成本约占投入金额 **{cost_pct:.3f}%**（佣金万2.5双向最低5元 + 印花税卖出0.05% + 过户费0.001%双向 + 滑点估算）。"
            )
            # 各止盈位收益（毛 + 净）
            tpp = cap.get("tp_profits") or []
            net_tpp = cap.get("net_tp_profits") or []
            if tpp:
                lines.append("- **各止盈位收益（毛 / 净）**：")
                for i, p in enumerate(tpp, 1):
                    np_ = net_tpp[i - 1] if i - 1 < len(net_tpp) else None
                    net_str = f" / 净 **{np_['net_profit']:+,.0f} 元**" if np_ else ""
                    lines.append(
                        f"  - 止盈 {i}（{p['price']} 元，{p['why']}）："
                        f"毛 **{p['profit']:+,.0f} 元**{net_str}（涨幅 {p['ret_pct']:+.1f}%）"
                    )
            # 止损亏损（毛 + 净：净亏损含交易成本，更大）
            loss = cap.get("loss_at_stop")
            loss_pct = cap.get("loss_pct")
            net_loss = cap.get("net_loss_at_stop")
            if loss is not None:
                lines.append(
                    f"- **止损最大亏损（毛 / 净）**：毛 {loss:+,.0f} 元 / 净 **{net_loss:+,.0f} 元**"
                    f"（跌幅 {loss_pct:+.1f}%，净亏损含交易成本故更大）。"
                )
        # ---- 短/中/长线对比与推荐 ----
        hp = best.get("horizon_plan")
        if hp and hp.get("horizons"):
            lines.append("")
            lines.append("**📊 短线 / 中线 / 长线 对比与推荐**")
            lines.append("")
            lines.append("| 周期 | 持有天数 | 上涨概率 | 中位收益 | 期望收益 | 目标价 | 止损价 | 风险调整收益 |")
            lines.append("|---|---|---|---|---|---|---|---|")
            for h in hp["horizons"]:
                tgt = h.get("target") or "—"
                lines.append(
                    f"| {h['name']} | {h['days']} | {h['up_prob']}% | {h['med_ret']:+.1f}% | "
                    f"{h['ev']:+.2f} | {tgt} | {h['stop']} | {h['risk_adj']:+.3f} |"
                )
            lines.append("")
            rec = hp.get("recommended")
            advice = hp.get("recommended_advice", "")
            if rec and rec["risk_adj"] > 0:
                lines.append(f"- **推荐**：{advice}")
                # 给出该周期的具体操作
                lines.append(
                    f"- **{rec['name']}操作方案**：买入价 {best['buy_zone'][0]}~{best['buy_zone'][1]} 元，"
                    f"目标价 {rec['target']} 元，止损 {rec['stop']} 元，持有约 {rec['days']} 个交易日。"
                )
            else:
                lines.append(f"- ⚠️ {advice}")
        # ---- 执行保证（能否买入 / 能否全部卖出） ----
        eg = best.get("execution_guarantee")
        if eg:
            lines.append("")
            lines.append("**✅ 执行保证（能否买入 / 能否全部卖出）**")
            lines.append(f"- **买入保证**：{eg['buy_guarantee']}")
            lines.append(f"- **卖出保证**：{eg['sell_guarantee']}")
            cb = "✓ 可保证买入" if eg["can_buy"] else "⚠️ 需分批买入"
            cs = "✓ 可全部卖出" if eg["can_sell_all"] else "⚠️ 需分批卖出"
            lines.append(f"- **结论**：{cb}，{cs}。")
        lines += [
            f"- **持续性参考**：当前已连涨 {best['streak_up']} 天，历史平均连涨 {best['avg_streak']} 天（最长 {best['max_streak']} 天）；",
            "",
            "**核心驱动**："
            f"{best['top_factor']} —— {best['top_factor_detail']}。",
            "",
        ]
    # 候选榜单（期望收益排序）
    ranked = bp.get("ranked", [])
    if ranked:
        lines += [
            "**候选榜单（按短期期望收益排序）**",
            "",
            "| 股票 | 现价 | 5日概率 | 5日中位收益 | 5日期望收益 | 20日概率 | 20日中位收益 | 三信号 |",
            "|---|---|---|---|---|---|---|---|",
        ]
        for c in ranked[:8]:
            up5 = c.get("up_prob_5d_real") or "—"
            med5 = c.get("med_ret_5d")
            med5s = f"{med5:+.1f}%" if med5 is not None else "—"
            ev5 = c.get("ev_5d")
            ev5s = f"{ev5:+.2f}" if ev5 is not None else "—"
            up20 = c.get("up_prob_20d_real") or "—"
            med20 = c.get("med_ret_20d")
            med20s = f"{med20:+.1f}%" if med20 is not None else "—"
            ens = c.get("ensemble_agree", "—")
            lines.append(
                f"| {c['name']} {c['code']} | {c['price']} | {up5}% | {med5s} | {ev5s} | {up20}% | {med20s} | {ens} |"
            )
        lines.append("")
    if heat_ctx:
        b = heat_ctx["breadth"]
        lines += [
            "**市场环境**",
            f"- 市场热度 {heat_ctx['score']}/100（{heat_ctx['level']}）；上涨 {b['up']} 家 / 下跌 {b['down']} 家，涨停 {b['limit_up']} 家；"
            f"两市成交额 {b['total_amount_yi']:,.0f} 亿。",
            "",
        ]
    lines += [
        "**失效信号（任一出现即离场）**",
        "- 跌破止损位（5 日均线 ×0.97 或 10 日均线/近20日低点）；",
        "- 放量滞涨（量增价平）或冲高回落留下长上影；",
        "- 市场热度骤降（跌回 40 以下）或出现系统性风险信号。",
        "",
        f"*以上为量化引擎对全市场 {bp['universe']:,} 只股票扫描 + {bp['analyzed']} 只相似形态预测的结果，"
        f"概率与收益均来自该股自身历史相似走势的统计，仅供研究参考，不构成投资建议。*",
    ]
    return "\n".join(lines)


def prompt_bestpick(question, bp, heat_ctx=None):
    slim = dict(time=bp["time"], universe=bp["universe"], shortlist=bp["shortlist"],
                scored=bp["scored"], analyzed=bp["analyzed"], tradable=bp["tradable"],
                best=bp["best"], ranked=bp["ranked"][:8], note=bp["note"])
    parts = [
        f"用户提问：{question}",
        "以下是量化引擎的「短期最佳推荐」深度分析结果。"
        "方法：先对全市场做动量扫描得到候选，再对每只候选跑相似形态预测（在该股自身历史中找 k-NN 相似走势，"
        "统计其后 5/10/20 日的真实上涨概率与中位收益），最后按「5日上涨概率 × 中位收益 = 短期期望收益」排序（20日概率作确认），"
        "只保留三信号共识看多且5日上涨概率≥50%、20日≥55%的标的。\n" + _ctx_json(slim),
    ]
    if heat_ctx:
        slim_h = dict(score=heat_ctx["score"], level=heat_ctx["level"],
                      breadth=heat_ctx["breadth"], indices=heat_ctx["indices"])
        parts.append("当前市场热度上下文：\n" + _ctx_json(slim_h))
    parts.append(
        "请据此回答用户问题：1) 明确给出**最佳推荐的一只股票**（期望收益最高且概率达标者），"
        "必须列出 5日/20日历史上涨概率、中位收益、期望收益、三信号共识方向、现价、买入区、止损位、目标价与盈亏比；"
        "2) 说明该概率来自该股自身历史相似形态的真实统计，而非主观判断；"
        "3) 列出候选榜单（按期望收益排序）及简评；"
        "4) 给出完整执行方案——必须包含：①「明天买入计划」（见 best.next_day_plan：预估次日开盘价、次日买入价区间、次日可成交性）、"
        "②怎么样买入（限价单价位/分批比例/挂单时机）、③仓位建议（Kelly 分数与评分仓位）、"
        "④怎么卖出（止损单/分批止盈价位/移动止损规则/时间止损）；"
    )
    # 若用户提问含资金金额，best 中会有 capital 字段，要求 LLM 给出具体元数
    has_capital = bool(bp.get("best", {}).get("capital"))
    has_horizon = bool(bp.get("best", {}).get("horizon_plan"))
    if has_capital and has_horizon:
        parts[-1] += (
            "5) 用户给出了具体资金金额（见 best.capital），必须回答「能赚多少」："
            "列出可买股数、实际投入金额、5日/20日预期收益（元，毛收益与净收益都要列，净收益=毛收益-交易成本）、"
            "各止盈位收益（元，毛/净）、止损最大亏损（元，毛/净）；并说明交易成本构成（佣金万2.5双向最低5元+印花税卖出0.05%+过户费0.001%双向+滑点）；"
            "6) 用户问短线/中线/长线如何选择（见 best.horizon_plan），必须对比三周期的上涨概率、中位收益、期望收益、风险调整后收益，"
            "推荐风险调整后收益最高的周期，并给出该周期的买入价/目标价/止损价/持有天数；"
            "7) 回答「保证买入/全部卖出」（见 best.execution_guarantee）：说明订单占日均成交额比例、能否限价单成交、卖出策略；"
            "8) 给出失效信号；9) 结尾附风险提示——**任何策略都无法保证收益最大化或保证赚钱**，历史概率不代表未来，"
            "次日开盘价为基于历史跳空分布的估算值非确定值，无 Level-2 逐笔数据，仅供研究参考，不构成投资建议。"
        )
    elif has_capital:
        parts[-1] += (
            "5) 用户给出了具体资金金额（见 best.capital），必须回答「能赚多少」："
            "列出可买股数、实际投入金额、5日/20日预期收益（元，毛/净）、各止盈位收益（元，毛/净）、止损最大亏损（元，毛/净）；"
            "6) 给出失效信号；7) 结尾附风险提示，强调不构成投资建议。"
        )
    else:
        parts[-1] += "5) 给出失效信号；6) 结尾附风险提示，强调不构成投资建议。"
    return "\n\n".join(parts)


# ================================================================ 均值回归（超跌反弹）


def local_mr_text(question, scan, heat_ctx=None):
    cands = scan["candidates"]
    lines = [
        f"### 均值回归·超跌反弹选股（{scan['time']}）",
        f"**针对问题：** {question}",
        "",
        "**策略逻辑（私募均值回归模型）**",
        "- 扫描全市场，寻找 RSI 超卖、触及布林下轨或短期急跌后缩量企稳的标的；",
        f"- 评估池 {scan['shortlist']} 只，命中超跌条件 {scan['scored']} 只，取前 {len(cands)} 名；",
        "- 回归目标：价格向布林中轨（20日均线）修复；止损：跌破近20日低点。",
        "",
    ]
    if cands:
        top = cands[0]
        lines += [
            "### 🎯 首选超跌反弹标的",
            "",
            f"**{top['name']}（{top['code']}）** · 现价 {top['price']} 元 · 今日 {top['pct']:+.2f}% · 成交额 {top['amount_yi']} 亿",
            "",
            f"- **超跌信号**：{top['reason']}；",
            f"- **指标**：RSI6={top['rsi6']}，近20日 {top['ret20']:+.1f}%，量比 {top['vol_ratio']}，布林下轨 {top['boll_low']} / 中轨 {top['boll_mid']}；",
            f"- **估值**：PE {top['pe'] or '—'}，PB {top['pb'] or '—'}；",
            f"- **买入参考区**：{top['buy_zone'][0]} ~ {top['buy_zone'][1]} 元；",
            f"- **止损位**：{top['stop_loss']} 元（近20日低点 {top['recent_low']} 下方，风险 ≈ {top['risk']}%）；",
            f"- **目标位**：{top['target']} 元（布林中轨，潜在收益 ≈ {top['reward']}%，盈亏比 {top['rr'] or '—'}:1）；",
            "",
        ]
    lines += [
        "**候选榜单**",
        "",
        "| 股票 | 现价 | 今日 | RSI6 | 近20日 | 量比 | 布林下轨 | 目标中轨 | 盈亏比 |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for c in cands:
        lines.append(
            f"| {c['name']} {c['code']} | {c['price']} | {c['pct']:+.2f}% | {c['rsi6']} "
            f"| {c['ret20']:+.1f}% | {c['vol_ratio']} | {c['boll_low']} | {c['boll_mid']} | {c['rr'] or '—'} |"
        )
    lines += [
        "",
        "**风险提示**：均值回归本质是抄底，需警惕『下跌中继』——若放量跌破近20日低点应立即止损；"
        "熊市中反弹高度有限，建议快进快出、不恋战。",
        "",
        "*以上为量化引擎扫描结果，仅供研究参考，不构成投资建议。*",
    ]
    return "\n".join(lines)


def prompt_mr(question, scan, heat_ctx=None):
    slim = dict(time=scan["time"], universe=scan["universe"],
                shortlist=scan["shortlist"], candidates=scan["candidates"])
    parts = [
        f"用户提问：{question}",
        "以下是均值回归（超跌反弹）策略扫描结果：寻找RSI超卖/触及布林下轨/短期急跌后缩量企稳的标的，"
        "回归目标为布林中轨：\n" + _ctx_json(slim),
    ]
    if heat_ctx:
        parts.append("市场热度上下文：\n" + _ctx_json(dict(score=heat_ctx["score"], level=heat_ctx["level"])))
    parts.append(
        "请据此回答：1) 给出首选超跌反弹标的及超跌信号（RSI/布林位置/跌幅）；"
        "2) 买入区、止损位（近20日低点）、目标位（布林中轨）与盈亏比；"
        "3) 候选榜单；4) 强调抄底风险与止损纪律。"
    )
    return "\n\n".join(parts)


# ================================================================ 趋势突破


def local_tb_text(question, scan, heat_ctx=None):
    cands = scan["candidates"]
    lines = [
        f"### 趋势突破选股（{scan['time']}）",
        f"**针对问题：** {question}",
        "",
        "**策略逻辑（私募趋势跟踪模型）**",
        "- 扫描放量突破近20日平台高点、且均线多头排列（MA5>MA10>MA20）的标的；",
        f"- 评估池 {scan['shortlist']} 只，命中突破条件 {scan['scored']} 只，取前 {len(cands)} 名；",
        "- 盈利逻辑：突破后惯性上涨，目标看前高+ATR；止损：跌回突破位下方。",
        "",
    ]
    if cands:
        top = cands[0]
        lines += [
            "### 🎯 首选趋势突破标的",
            "",
            f"**{top['name']}（{top['code']}）** · 现价 {top['price']} 元 · 今日 +{top['pct']}% · 成交额 {top['amount_yi']} 亿",
            "",
            f"- **突破信号**：{top['reason']}；",
            f"- **均线**：{top['ma_align']}排列，量比 {top['vol_ratio']}，距60日高点 {top['room60']}% 空间；",
            f"- **买入参考区**：{top['buy_zone'][0]} ~ {top['buy_zone'][1]} 元（突破位 {top['high20']} 上方）；",
            f"- **止损位**：{top['stop_loss']} 元（跌回突破位下方，风险 ≈ {top['risk']}%）；",
            f"- **目标位**：{top['target']} 元（前高+ATR，潜在收益 ≈ {top['reward']}%，盈亏比 {top['rr'] or '—'}:1）；",
            "",
        ]
    lines += [
        "**候选榜单**",
        "",
        "| 股票 | 现价 | 今日 | 量比 | 均线 | 突破位 | 距前高 | 盈亏比 |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for c in cands:
        lines.append(
            f"| {c['name']} {c['code']} | {c['price']} | +{c['pct']}% | {c['vol_ratio']} "
            f"| {c['ma_align']} | {c['high20']} | {c['room60']}% | {c['rr'] or '—'} |"
        )
    lines += [
        "",
        "**风险提示**：假突破是趋势策略最大敌人——若突破后3日内跌回突破位且放量，应果断止损；"
        "建议突破确认（站稳2日）后再介入，避免盘中假突破诱多。",
        "",
        "*以上为量化引擎扫描结果，仅供研究参考，不构成投资建议。*",
    ]
    return "\n".join(lines)


def prompt_tb(question, scan, heat_ctx=None):
    slim = dict(time=scan["time"], universe=scan["universe"],
                shortlist=scan["shortlist"], candidates=scan["candidates"])
    parts = [
        f"用户提问：{question}",
        "以下是趋势突破策略扫描结果：寻找放量突破20日平台+均线多头排列的标的，"
        "目标看前高+ATR，止损跌回突破位：\n" + _ctx_json(slim),
    ]
    if heat_ctx:
        parts.append("市场热度上下文：\n" + _ctx_json(dict(score=heat_ctx["score"], level=heat_ctx["level"])))
    parts.append(
        "请据此回答：1) 给出首选趋势突破标的及突破信号（平台突破/放量/多头排列）；"
        "2) 买入区、止损位（突破位下方）、目标位（前高+ATR）与盈亏比；"
        "3) 候选榜单；4) 强调假突破风险与确认纪律。"
    )
    return "\n\n".join(parts)


# ================================================================ 涨停连板扫描


def local_limitup_text(question, lu):
    l = lu["ladder"]
    lines = [
        f"### 涨停连板全景（{lu['time']}）",
        f"**针对问题：** {question}",
        "",
        f"**今日涨停 {lu['limit_up']} 家 / 炸板 {lu['broken']} 家（炸板率 {lu['broken_rate']}%）**",
        f"- 连板梯队：首板 {l['fb']} 家 · 2连板 {l['lb2']} 家 · 3板及以上 {l['lb3p']} 家；"
        f"空间板为 **{lu['max_streak_name']}（{lu['max_streak']} 连板）**；",
        "- 断板氛围：" + ("炸板率超过30%，情绪分歧加大，追高需谨慎。" if lu["broken_rate"] > 30
                       else "炸板率适中，情绪尚可但已非一致性。" if lu["broken_rate"] > 15
                       else "炸板率极低，市场情绪一致性极强（往往接近情绪高点，注意物极必反）。"),
    ]
    if lu.get("broken_samples"):
        names = "、".join(f"{b['name']}({b['pct']:+}%)" for b in lu["broken_samples"][:5])
        lines.append(f"- 今日炸板代表：{names}。")
    stocks = lu["stocks"]
    lines += [
        "",
        "**重点连板股研判（能走几个板 / 何时断板）**",
        "",
        "| 股票 | 现价 | 连板 | 封单额 | 封成比 | 历史晋级率 | 次日连板概率 | 预期高度 | 断板风险 |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for s in stocks:
        lines.append(
            f"| {s['name']} {s['code']} | {s['price']} | {s['streak']}板 | {s['seal_yi']}亿 "
            f"| {s['seal_ratio']} | {('' if s['promo_rate'] is None else str(int(s['promo_rate']*100))+'%')} "
            f"| {s['next_day_prob']}% | {s['expected_height']}板 | {s['break_risk']} |"
        )
    lines += ["", "**逐只断板预判**", ""]
    for s in stocks:
        pr = "无历史样本（用全市场基准28%估计）" if s["promo_rate"] is None else \
            f"历史上共涨停 {s['hist_limits']} 次，其中次日晋级率 {int(s['promo_rate']*100)}%，晋级后平均再走 {s['avg_extend']} 板（历史最长 {s['max_streak']} 连板）"
        lines.append(
            f"- **{s['name']}（{s['code']}）{s['streak']} 连板**，封单 {s['seal_yi']} 亿（封成比 {s['seal_ratio']}）。"
            f"{pr}。综合封板强度，模型估计次日继续涨停概率 {s['next_day_prob']}%，**预期还能走出约 {max(0, s['expected_height'] - s['streak'])} 个板（看到 {s['expected_height']} 板附近）**。"
            f"断板风险：**{s['break_risk']}** —— {s['break_hint']}。"
        )
    lines += [
        "",
        "**什么时候会断板（信号清单）**",
        "- **封单衰减**：收盘封成比 < 0.3 的烂板，多数撑不过次日开盘半小时；",
        "- **竞价定生死**：连板股次日竞价低于 -2% 且抢筹无力，开盘即是断板点；高开 3% 以上缩量秒板则晋级；",
        "- **高度压制**：市场空间板（当前 " + str(lu['max_streak']) + " 连板）断板当日，低位跟风板通常集体断板；",
        "- **情绪指标**：炸板率连续两日 > 30%、涨停家数较前日腰斩，即为情绪退潮信号，所有连板股断板概率同步抬升。",
        "",
        f"*以上为量化引擎对全市场 {lu['universe']:,} 只股票实时扫描的推演结果，涨停/连板博弈风险极高，仅供研究参考，不构成投资建议。*",
    ]
    return "\n".join(lines)


def prompt_limitup(question, lu):
    slim = dict(time=lu["time"], limit_up=lu["limit_up"], broken=lu["broken"],
                broken_rate=lu["broken_rate"], ladder=lu["ladder"],
                max_streak=lu["max_streak"], max_streak_name=lu["max_streak_name"],
                broken_samples=lu.get("broken_samples"), stocks=lu["stocks"])
    return (
        f"用户提问：{question}\n\n"
        "以下是量化引擎刚完成的全市场涨停连板扫描结果（含连板梯队、封单/封成比、"
        "个股历史涨停晋级率、次日连板概率与预期高度模型输出）：\n" + _ctx_json(slim) +
        "\n\n请据此回答：1) 当前涨停梯队与情绪周期位置（启动/发酵/高潮/退潮）；"
        "2) 重点连板股『还能走几个板』——引用封成比、历史晋级率与预期高度模型；"
        "3) 『什么时候会断板』——给出每只关键股的断板风险与时点判断（竞价/盘中放量/空间板压制）；"
        "4) 结尾必须附涨停博弈风险极高的提示。"
    )


# ================================================================ 个股买卖点研判


def local_trade_text(question, p, heat_ctx=None):
    mark = lambda ok: "✓ 已满足" if ok else "✗ 未满足"  # noqa: E731
    zone_emoji = {"买入区": "🟢", "持有区": "🟡", "观望区": "⚪", "卖出区": "🔴"}.get(p["zone"], "⚪")
    lines = [
        f"### 买卖点研判：{p['name']}（{p['code']}）· {p['date']}",
        f"**当前信号：{zone_emoji} {p['zone']}**（七因子评分 {p['score']:+d}，{p['direction']}，置信度{p['confidence']}）",
        f"现价 **{p['close']}**，支撑 **{p['support']}** / 压力 **{p['resistance']}**（20日区间+布林带），ATR≈{p['atr']}。",
        "",
        "**一、什么时候买入（三套方案，任选其一执行）**",
        "",
    ]
    for b in p["buy_plans"]:
        lo, hi = b["price"]
        lines.append(f"- **{b['style']}方案**：{b['action']}，参考价 **{lo} ~ {hi}** 元 —— {b['note']}。")
    lines += [
        "",
        "**二、什么时候卖出（价位+条件双保险）**",
        f"- **止损位：{p['sell_plan']['stop']}（现价 -{abs(p['sell_plan']['stop_pct'])}%）** —— 收盘跌破无条件离场，不抗单；",
        f"- **第一止盈：{p['sell_plan']['tp1']}（+{p['sell_plan']['tp1_pct']}%）** —— 触及压力位减仓 1/2，剩余移动止盈；",
        f"- **第二止盈：{p['sell_plan']['tp2']}（+{p['sell_plan']['tp2_pct']}%）** —— ATR 外推目标，到价全部了结；",
        f"- **移动止盈**：{p['sell_plan']['trailing']}；",
        f"- 按{'稳健' if len(p['buy_plans'])>1 else '激进'}买点计算，**盈亏比约 {p['reward_risk']}:1**"
        + ("（≥1.5 可做，<1.5 放弃本次交易）。" if (p['reward_risk'] or 0) < 1.5 else "。"),
        "",
        "**三、买入触发条件核对（当前时点）**",
    ]
    lines += [f"- {mark(ok)}  {c}" for c, ok in p["buy_checks"]]
    lines += ["", "**四、卖出触发条件核对（当前时点）**"]
    lines += [f"- {mark(ok)}  {c}" for c, ok in p["sell_checks"]]
    lines += [
        "",
        "**五、时点结论**",
        f"- {p['timing']}；",
        f"- **建议仓位：{p['position']}**。",
    ]
    if heat_ctx:
        lines.append(f"- 大盘环境：市场热度 {heat_ctx['score']}/100（{heat_ctx['level']}），"
                     + ("热度配合，个股买点的胜率环境尚可。" if heat_ctx["score"] >= 50
                        else "大盘偏弱，个股买点宜再压低仓位或等待大盘企稳。"))
    lines += [
        "",
        "*以上为量化模型基于技术指标推演的交易计划，不构成投资建议；请结合基本面与自身风险承受能力执行。*",
    ]
    return "\n".join(lines)


def prompt_trade(question, p, heat_ctx=None):
    slim = {k: p[k] for k in ("code", "name", "date", "close", "score", "direction",
                              "zone", "support", "resistance", "buy_plans", "sell_plan",
                              "reward_risk", "position", "timing", "buy_checks", "sell_checks")}
    parts = [f"用户提问：{question}",
             "以下是量化引擎为该股票生成的买卖点交易计划（含信号分区、三套买入方案、止损止盈价位、"
             "触发条件核对、盈亏比与仓位建议）：\n" + _ctx_json(slim)]
    if heat_ctx:
        parts.append("当前市场热度上下文：\n" + _ctx_json(dict(
            score=heat_ctx["score"], level=heat_ctx["level"], breadth=heat_ctx["breadth"])))
    parts.append("请据此回答『什么时候买入、什么时候卖出』：直接给出可执行的价位与时点"
                 "（买什么价、到什么价卖、跌破什么价止损、仓位多少），并说明触发条件与失效情形，结尾附风险提示。")
    return "\n\n".join(parts)


# ---------------------------------------------------------------- 妖股研判


def _r(x):
    return "—" if x is None else x


def local_demon_text(question, d, scan_list=None):
    """妖股研判：本地规则引擎研报"""
    icon = "🐉" if d["score"] >= 55 else ("🥚" if d["score"] >= 40 else "🐔")
    head = (f"### 妖股研判：{d['name']}（{d['code']}）· {d['date'][:10]}\n"
            f"**妖股评分 {d['score']}/100 —— {icon} {d['level']}**\n"
            f"现价 {d['price']}（今日 {_r(d['pct'])}%），生命周期阶段：**{d['stage']}**\n\n"
            "**六因子评分明细**\n\n"
            "| 因子 | 得分 | 满分 | 依据 |\n|---|---|---|---|\n")
    for f in d["factors"]:
        head += f"| {f['name']} | {f['got']} | {f['full']} | {f['detail']} |\n"
    lines = [head,
             f"\n**近期上涨因素复盘**\n- 5日/10日/20日涨幅：+{d['ret5']}% / +{d['ret10']}% / +{d['ret20']}%；"
             f"近20日涨停 {d['limit_count_20']} 次（当前{'断板' if d['cur_streak'] == 0 else str(d['cur_streak']) + '连板'}，最长 {d['max_streak']} 连板）\n"
             f"- 量能：5日均额为20日的 {d['vol_ratio']} 倍；日均振幅 {d['avg_amp']}%"
             f"{'；已创历史新高' if d['is_new_high'] else ''}"
             f"{'；偏离年线 +' + str(d['dist_ma250']) + '%' if d['dist_ma250'] is not None else ''}\n"
             f"\n**生命周期定位：{d['stage']}**\n- {d['stage_detail']}\n"
             f"\n**能不能涨成妖股 / 还能涨多高**\n- {d['hist_par']}\n- {d['potential']}\n"]
    if d["top_signals"]:
        lines.append("**见顶信号核对（出现任意2条警惕，3条离场）**\n"
                     + "\n".join(f"- ⚠️ {s}" for s in d["top_signals"]))
    else:
        lines.append("**见顶信号核对**\n- ✓ 暂未出现明显见顶信号")
    if scan_list:
        lines.append("\n**同类强势股妖气对比（今日扫描前几名）**\n"
                     + "| 股票 | 妖股评分 | 评级 | 阶段 | 20日涨幅 |\n|---|---|---|---|---|\n"
                     + "\n".join(f"| {c['name']} {c['code']} | {c['score']} | "
                                 f"{c['level'].split('（')[0]} | {c['stage'].split('（')[0]} | +{c['ret20']}% |"
                                 for c in scan_list[:5]))
    lines.append("\n*妖股=高波动情绪博弈，随时可能断板腰斩，以上为量化模型推演，不构成投资建议。*")
    return "\n".join(lines)


def local_demon_scan_text(question, scan):
    """全市场妖股扫描研报"""
    cands = scan["candidates"]
    lines = [
        f"### 全市场妖股扫描 · {scan['date']}",
        f"**针对问题：** {question}",
        "",
        f"**扫描过程**：今日强势股（涨幅≥5%且成交额≥5亿）共 {scan['shortlist']} 只，"
        f"逐只做妖股六因子评分（爆发力/涨停密度/连板结构/量能异动/巨幅震荡/趋势突破），取前 {len(cands)} 名。",
        "",
        "**妖股候选榜**",
        "",
        "| 股票 | 现价 | 今日 | 妖股评分 | 评级 | 生命周期阶段 | 20日涨幅 | 连板 |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for c in cands:
        lines.append(f"| {c['name']} {c['code']} | {c['price']} | {_r(c.get('today_pct', c.get('pct')))}% | "
                     f"{c['score']} | {c['level']} | {c['stage']} | +{c['ret20']}% | "
                     f"{'断板' if c['cur_streak'] == 0 else str(c['cur_streak']) + '板'} |")
    lines += ["", "**逐只点评（能不能涨成妖股）**", ""]
    for c in cands:
        lines.append(
            f"- **{c['name']}（{c['code']}）** 妖股评分 {c['score']}/100（{c['level']}）。"
            f"核心依据：20日涨幅 +{c['ret20']}%、近20日涨停 {c['limit_count_20']} 次、"
            f"量能异动 {c['vol_ratio']} 倍。**{c['stage']}**——{c['stage_detail']} "
            f"{c['potential']}"
        )
    lines += [
        "",
        "**妖股判定要点**：评分≥75 为已成妖（短期翻倍+涨停密集+巨量换手），55~74 为准妖股"
        "（妖性十足、处于主升概率高），40~54 为妖股胚胎（有待放量确认）。妖股寿命通常 8~15 个交易日，"
        "追妖本质是情绪接力，务必遵守断板/巨量滞涨离场纪律。",
        "",
        "*以上为量化模型扫描结果，不构成投资建议。*",
    ]
    return "\n".join(lines)


def prompt_demon(question, d, scan_list=None):
    slim = {k: d[k] for k in ("code", "name", "price", "pct", "score", "level",
                              "factors", "stage", "stage_detail", "ret5", "ret10", "ret20",
                              "limit_count_20", "cur_streak", "max_streak", "vol_ratio",
                              "avg_amp", "is_new_high", "dist_ma250", "top_signals",
                              "potential", "hist_par")}
    parts = [f"用户提问：{question}",
             "以下是量化引擎对该股的妖股六因子评估（评分维度：短期爆发力/涨停密度/连板结构/"
             "量能异动/巨幅震荡/趋势突破，含生命周期定位、见顶信号与历史妖股统计基准）：\n" + _ctx_json(slim)]
    if scan_list:
        parts.append("同期其他强势股妖股评分对比：\n" + _ctx_json(
            [dict(name=c["name"], code=c["code"], score=c["score"], level=c["level"],
                  stage=c["stage"], ret20=c["ret20"]) for c in scan_list[:5]]))
    parts.append("请据此回答『该股能不能涨成妖股、处于妖股生命周期哪个阶段、还能涨多久多高、"
                 "何时该警惕见顶』：先给结论（妖股评级与阶段），再逐条论证，结尾附风险提示。")
    return "\n\n".join(parts)


def prompt_demon_scan(question, scan):
    slim = dict(date=scan["date"], shortlist=scan["shortlist"],
                candidates=scan["candidates"])
    parts = [f"用户提问：{question}",
             "以下是量化引擎的全市场妖股扫描结果（今日涨幅≥5%且成交额≥5亿的强势股，"
             "逐只做妖股六因子评分后取前几名）：\n" + _ctx_json(slim)]
    parts.append("请据此回答『哪些股票有妖股相、能不能涨成妖股、还能涨多久』：给出候选榜单与逐只论证"
                 "（引用六因子、生命周期阶段与历史妖股统计基准），结尾附风险提示。")
    return "\n\n".join(parts)


# ---------------------------------------------------------------- 盘后预测


def local_predict_text(question, p):
    h5, h10, h20 = p["horizons"]["5日"], p["horizons"]["10日"], p["horizons"]["20日"]
    icon = {"看多": "📈", "看空": "📉", "震荡": "↔️", "中性": "➡️"}.get(p["direction"], "")
    t, a = p.get("timing") or {}, p.get("amp") or {}
    price = p["price"]
    peak_d = p.get("avg_days_peak20")
    trough_d = p.get("avg_days_trough20")
    peak_d = int(peak_d) if peak_d is not None else None
    trough_d = int(trough_d) if trough_d is not None else None

    def px(pct):
        if pct is None:
            return "—"
        return f"{price * (1 + pct / 100):.2f}"

    def day(v):
        return "—" if v is None else (int(v) if float(v) == int(v) else v)

    t = dict(t)
    for k in ("up2_days_med", "dn2_days_med"):
        if t.get(k) is not None:
            t[k] = day(t[k])

    lines = [
        f"### 盘后预测：{p['name']}（{p['code']}）· 最高概率回答 · {p['date'][:10]}",
        f"**总体研判：{icon} {p['direction']}**（现价 {price}，置信度：{p.get('confidence', '—')}；"
        f"扫描该股上市以来全部 **{p['scan_days']} 个交易日**，匹配 **{p['similar_count']} 个最相似时点**统计其后走势）",
        "",
    ]

    # ---- 📊 逻辑分析：列举看涨/看跌原因（每条带数据支撑）----
    signals = p.get("_signals") or []
    ind = p.get("_indicators") or {}
    direction = p.get("direction", "")
    if signals:
        if direction in ("看多", "偏多"):
            reasons = [s for s in signals if s.get("verdict") == "多"]
            oppose = [s for s in signals if s.get("verdict") == "空"]
            rlabel = "看涨原因"
        elif direction in ("看空", "偏空"):
            reasons = [s for s in signals if s.get("verdict") == "空"]
            oppose = [s for s in signals if s.get("verdict") == "多"]
            rlabel = "看跌原因"
        else:
            reasons = [s for s in signals if s.get("verdict") in ("多", "空")]
            oppose = []
            rlabel = "多空分歧原因"
        if reasons:
            lines.append(f"**📊 逻辑分析：{rlabel}（基于当前技术面数据）**")
            lines.append("")
            for s in reasons[:6]:
                lines.append(f"- **{s['name']}**：{s['desc']}")
            # 补充关键量化数据
            extra = []
            ma = ind.get("ma") or {}
            if ma:
                above = sum(1 for n in (5, 10, 20, 60) if ma.get(f"ma{n}") and price > ma[f"ma{n}"])
                extra.append(f"价格站上 {above}/4 条主要均线")
            macd = ind.get("macd") or {}
            if macd.get("dif") is not None and macd.get("dea") is not None:
                extra.append(f"MACD DIF={macd['dif']} / DEA={macd['dea']} / 柱={macd.get('hist')}")
            rsi = ind.get("rsi") or {}
            if rsi.get("rsi6") is not None:
                extra.append(f"RSI6={rsi['rsi6']}")
            vol = ind.get("volume") or {}
            if vol.get("vol_ratio") is not None:
                extra.append(f"量比={vol['vol_ratio']}")
            stats = p.get("_stats") or {}
            if stats.get("pct_5d") is not None:
                extra.append(f"近5日 {stats['pct_5d']}% / 近20日 {stats.get('pct_20d')}%")
            if extra:
                lines.append("")
                lines.append(f"**关键数据**：{'；'.join(extra)}")
            if oppose:
                lines.append("")
                lines.append(f"**⚠️ 反向风险信号**（需警惕）：" + "；".join(
                    f"{s['name']}({s['desc']})" for s in oppose[:3]))
            lines.append("")

    # 三信号共识投票明细（相似形态 × 七因子评分 × 市场环境）
    ens = p.get("ensemble") or {}
    if ens.get("votes"):
        lines += [
            "**🧭 三信号共识投票**（仅当多数信号同向才给出方向——单一弱信号无预测力，回测已验证）",
            "",
            "| 信号 | 投票 | 依据 |",
            "|---|---|---|",
        ]
        for v in ens["votes"]:
            lines.append(f"| {v['name']} | {v.get('signal') or '不可用'} | {v.get('detail') or '—'} |")
        lines.append("")
    lines.append("**⚡ 最高概率路径（一句话）**")
    if p["direction"] == "中性":
        lines.append(
            "**⛔ 三信号未形成多数同向，本模型选择弃权而非猜测方向。**历史回测验证：此状态下 20 日方向命中率"
            "≈随机（50%），任何方向断言都不可靠。以下历史分布统计仅供风险管理参考，建议观望或用小仓位应对。")
    elif (h20["up_prob"] or 50) >= 50:
        lines.append(
            f"未来20日上涨概率 **{h20['up_prob']}%**；最可能 **第 {t.get('up2_days_med') or '1~3'} 个交易日**出现≥2%的上涨启动，"
            f"先冲高至 **{px(a.get('max_gain_med'))}（{a.get('max_gain_med')}%）**；回调低点约 **第 {trough_d or '9'} 个交易日**，"
            f"极限回踩 **{px(a.get('max_dd_med'))}（{a.get('max_dd_med')}%）**。")
    else:
        lines.append(
            f"未来20日下跌概率 **{round(100 - (h20['up_prob'] or 50), 1)}%**；反弹平均在第 {peak_d or '11'} 个交易日附近见顶 "
            f"**{px(a.get('max_gain_med'))}（{a.get('max_gain_med')}%）**，之后回落，"
            f"低点出现在第 {trough_d or '9'} 个交易日附近 **{px(a.get('max_dd_med'))}（{a.get('max_dd_med')}%）**。")
    lines.append("")

    # ① 什么时候开始上涨
    lines += [
        "**① 什么时候开始上涨？**",
        f"- 相似形态出现后，**{t.get('up2_hit')}%** 的样本在20日内出现单日≥2%的上涨脉冲，"
        f"首次出现平均在 **第 {t.get('up2_days_med')} 个交易日**",
        f"- 明天（次日）直接上涨的概率 **{t.get('next_day_up')}%**（次日中位 {t.get('next_day_med')}%）",
        f"- 上涨动能最集中的窗口：第 1~{peak_d if peak_d is not None else '—'} 个交易日"
        f"（相似样本的阶段高点平均出现于第 {peak_d if peak_d is not None else '—'} 日）",
    ]
    # ② 上涨多少
    lines += [
        "",
        "**② 能上涨多少？**",
        f"- 20日内最高冲高幅度：中位 **+{a.get('max_gain_med')}%**（对应 **{px(a.get('max_gain_med'))}**），"
        f"乐观情形（75%分位）**+{a.get('max_gain_p75')}%**（**{px(a.get('max_gain_p75'))}**）",
        f"- 20日收盘目标价 **{_r(h20['target'])}**（中位涨跌 {h20['med_ret']}%）；10日目标 {_r(h10['target'])}、5日目标 {_r(h5['target'])}",
    ]
    if p.get("mid60"):
        m = p["mid60"]
        lines.append(f"- 中期（60日）展望：上涨概率 {m['up_prob']}%，中位 {m['med_ret']}%，"
                     f"60日参考目标 {m['target']}")
    # ③ 会不会下跌 / 什么时候跌
    lines += [
        "",
        "**③ 会不会下跌、什么时候跌？**",
        f"- 20日下跌概率 **{round(100 - (h20['up_prob'] or 0), 1)}%**（=相似样本中此后收跌的比例）",
        f"- **{t.get('dn2_hit')}%** 的相似样本在20日内出现单日≥2%的回调，首次回调平均在 **第 {t.get('dn2_days_med')} 个交易日**",
        f"- 阶段低点平均出现在 **第 {trough_d if trough_d is not None else '—'} 个交易日**；"
        f"若前 5 日先跌破 {_r(h5['low_pt'])}，则提前转入下跌情形",
    ]
    # ④ 下跌到多少
    lines += [
        "",
        "**④ 下跌到多少？**",
        f"- 20日内最低回撤：中位 **{a.get('max_dd_med')}%**（风险价 **{px(a.get('max_dd_med'))}**），"
        f"悲观情形（25%分位，跌得更深）**{a.get('max_dd_p25')}%**（**{px(a.get('max_dd_p25'))}**）",
        f"- 操作防线：跌破 **{_r(h5['low_pt'])}**（5日窗口25%分位低点）说明走失败样本路径，参考极端情形 {h20['worst']}%",
    ]

    # ---- 💰 买入计划：什么时候买、什么价买、能不能买进去 ----
    ind2 = p.get("_indicators") or {}
    stats2 = p.get("_stats") or {}
    cur_pct = float(ind2.get("pct") or 0)
    vol_ratio = ind2.get("volume", {}).get("vol_ratio")
    code = p.get("code", "")
    # 涨跌停阈值
    if code[:2] in ("30", "68"):
        lim = 19.5
    elif code[0] in ("4", "8") or code[:2] == "92":
        lim = 29.5
    else:
        lim = 9.5
    is_limit_up = cur_pct >= lim - 0.3
    is_limit_down = cur_pct <= -(lim - 0.3)
    # 流动性评估
    if vol_ratio is not None:
        if vol_ratio >= 1.0:
            liq = "成交活跃，可正常买入"
        elif vol_ratio >= 0.5:
            liq = "成交温和，建议分批挂单"
        else:
            liq = "成交萎缩，可能难以足额买入，建议小仓位试探"
    else:
        liq = "流动性数据不足，建议分批挂单"

    buy_day = t.get("up2_days_med") or "1~3"
    buy_low = px(a.get("max_dd_med"))  # 回踩低点附近是较优买点
    lines += [
        "",
        "**💰 买入计划**",
        f"- **买入时机**：预计第 **{buy_day}** 个交易日出现上涨启动（次日直接上涨概率 {t.get('next_day_up')}%）；"
        f"若想更低成本，可等回踩至 **{buy_low}**（20日中位回撤低点）附近介入。",
        f"- **买入价区间**：{round(price * 0.99, 2)} ~ {round(price * 1.01, 2)}（现价附近），激进可追、稳健等回踩；不建议追高超过 +3%。",
        f"- **能否买进去**：{'⛔ 当前涨停封板，排队未必能成交，建议次日看竞价' if is_limit_up else f'✅ {liq}'}。",
        f"- **分批策略**：首仓 30~40%，启动确认（放量站上5日线）后加到 60~70%，留 30% 应对回调加仓；单只仓位不超过总资金 20%。",
    ]

    # ---- 🎯 卖出计划：什么时候卖、什么价卖、能不能卖出去 ----
    sell_day = peak_d if peak_d is not None else "9~11"
    target_med = px(a.get("max_gain_med"))
    target_opt = px(a.get("max_gain_p75"))
    lines += [
        "",
        "**🎯 卖出计划**",
        f"- **卖出时机**：阶段高点平均出现在第 **{sell_day}** 个交易日，届时考虑分批止盈；"
        f"若提前出现放量滞涨（量增价平）或长上影线，不等时点直接减仓。",
        f"- **止盈目标**：第一目标 **{target_med}**（中位冲高 {a.get('max_gain_med')}%）减半仓；"
        f"第二目标 **{target_opt}**（乐观情形 {a.get('max_gain_p75')}%）清仓；到达目标位不贪。",
        f"- **止损位**：跌破 **{_r(h5['low_pt'])}**（5日风险低点）无条件止损，单笔亏损控制在 5~8% 以内。",
        f"- **能否卖出去**：{'⛔ 当前跌停，挂单未必能成交，次日尽早处理' if is_limit_down else '✅ 正常流动性下可卖出，建议挂限价单而非市价单避免滑点'}。",
        f"- **止盈纪律**：达到第一目标减半、第二目标清仓；若 5 日内未启动且跌破买入价 3%，认错离场不恋战。",
    ]

    lines += [
        "",
        "**分时段预测明细**",
        "",
        "| 窗口 | 上涨概率 | 中位涨跌 | 乐观 | 悲观 | 目标价 | 风险低点 |",
        "|---|---|---|---|---|---|---|",
    ]
    for lbl, h in (("未来5日", h5), ("未来10日", h10), ("未来20日", h20)):
        lines.append(f"| {lbl} | {h['up_prob']}% | {_r(h['med_ret'])}% | "
                     f"{h['best']}% | {h['worst']}% | {_r(h['target'])} | {_r(h['low_pt'])} |")
    lines += [
        "",
        "**最相似的8个历史时点（此后实际走势）**",
        "",
        "| 历史日期 | 当时价格 | 后5日 | 后10日 | 后20日 |",
        "|---|---|---|---|---|",
    ]
    for s in p["similar"]:
        lines.append(f"| {s['date']} | {_r(s['price'])} | {_r(s['ret5'])}% | {_r(s['ret10'])}% | {_r(s['ret20'])}% |")
    lines += [
        "",
        "*预测基于该股全部历史交易数据的相似形态统计，历史不会简单重复；极端行情/黑天鹅无法被该模型捕捉，不构成投资建议。*",
    ]
    return "\n".join(lines)


def prompt_predict(question, p):
    parts = [f"用户提问：{question}",
             "以下是量化引擎对该股的盘后预测（相似形态统计法：扫描该股全部历史交易数据，找出与当前9维技术状态"
             "最相似的时点，统计其后5/10/20/60日真实走势，含上涨概率、涨跌幅度、上涨/回调启动时点、"
             "最大冲高与最大回撤分布、目标价与风险低点）。"
             "其中 _signals 为当前技术面多空信号（含具体指标数值），_indicators/_stats 为当前关键数据：\n" + _ctx_json(p)]
    parts.append(
        "请按以下结构回答：\n"
        "1) **逻辑分析**：先列举看涨或看跌的原因，必须引用 _signals 中的具体指标数值（如 MACD DIF/DEA、RSI6、量比、均线位置、近5/20日涨跌幅等），"
        "至少 3 条，每条带数据；同时列出反向风险信号；\n"
        "2) 逐条回答四问：①什么时候开始上涨 ②能上涨多少 ③会不会下跌、什么时候跌 ④下跌到多少；"
        "每问给出最高概率的数字答案（概率/幅度/价格/第几个交易日）；\n"
        "3) **买入计划**：明确买入时机（第几个交易日）、买入价区间、能否买进去（是否涨停/流动性）、分批仓位策略；\n"
        "4) **卖出计划**：明确卖出时机（第几个交易日）、止盈目标价（分批止盈）、止损位、能否卖出去（是否跌停）、止盈纪律；\n"
        "5) 汇总成一句话的最高概率路径；\n"
        "6) 结尾强调统计预测的局限性与风险提示。"
    )
    return "\n\n".join(parts)


# ---------------------------------------------------------------- 全球市场资产

def local_global_text(question, ga, pred=None):
    """全球资产研报：实时行情 + 技术面（复用 A 股量化引擎）+ 相似形态预测"""
    meta = ga.get("meta") or {}
    q_ = ga.get("quote") or {}
    name = meta.get("name") or q_.get("name") or meta.get("symbol", "")
    cat_name = meta.get("cat_name", "")
    lines = [
        f"### 🌍 全球资产分析：{name}（{meta.get('symbol', '')} · {cat_name}）",
    ]
    px = _r(q_.get("price"))
    pct = _r(q_.get("pct"))
    arrow = "📈" if (pct or 0) >= 0 else "📉"
    lines.append(f"**实时行情：{arrow} {px}（{pct}%）**"
                 f"　今开 {_r(q_.get('open'))}　最高 {_r(q_.get('high'))}　"
                 f"最低 {_r(q_.get('low'))}　昨收 {_r(q_.get('prev_close'))}　"
                 f"<em>{q_.get('time') or ''}</em>")
    if ga.get("kline_error"):
        lines.append(f"\n*{ga['kline_error']}，以上为实时行情分析。*")
    daily = ga.get("daily")
    if daily:
        lines.append(f"\n**历史数据**：{ga.get('history_days')} 个交易日"
                     f"（{ga.get('first_day')} 至今全历史）")
        ind = daily.get("indicators") or {}
        ma = ind.get("ma") or {}
        macd = ind.get("macd") or {}
        rsi = ind.get("rsi") or {}
        kdj = ind.get("kdj") or {}
        boll = ind.get("boll") or {}
        st = daily.get("stats") or {}
        lines += [
            "",
            "**核心指标**",
            f"- 均线：MA5 {_r(ma.get('ma5'))} / MA20 {_r(ma.get('ma20'))} / MA60 {_r(ma.get('ma60'))}"
            f"（现价 {ind.get('close')}）",
            f"- MACD：DIF {_r(macd.get('dif'))} vs DEA {_r(macd.get('dea'))}（柱 {_r(macd.get('hist'))}）",
            f"- RSI：RSI6 {_r(rsi.get('rsi6'))} / RSI12 {_r(rsi.get('rsi12'))}；"
            f"KDJ：K {_r(kdj.get('k'))} D {_r(kdj.get('d'))} J {_r(kdj.get('j'))}",
            f"- 布林带：上轨 {_r(boll.get('up'))} / 中轨 {_r(boll.get('mid'))} / 下轨 {_r(boll.get('low'))}；ATR14 {_r(ind.get('atr14'))}",
            f"- 区间表现：5日 {_r(st.get('pct_5d'))}% · 20日 {_r(st.get('pct_20d'))}% · 60日 {_r(st.get('pct_60d'))}%"
            f"（20日内 {_r(st.get('up_days_20'))} 涨 / {_r(st.get('down_days_20'))} 跌）",
        ]
        srs = daily.get("support_resistance") or {}
        lines.append(f"- **支撑 {_r(srs.get('support'))} / 压力 {_r(srs.get('resistance'))}**（20日高低+布林轨道）")
        sigs = daily.get("signals") or []
        if sigs:
            lines += ["", "**技术面信号**"]
            for s in sigs:
                mark = {"多": "🟢", "空": "🔴", "中性": "⚪"}.get(s.get("verdict"), "⚪")
                lines.append(f"- {mark} **{s['name']}**：{s['desc']}")
    if pred:
        lines += ["", "---", ""]
        lines.append(_predict_digest(pred))
    lines += ["", "*全球资产分析基于该资产全部可得历史交易数据；不构成投资建议。*"]
    return "\n".join(lines)


def _predict_digest(p):
    """相似形态预测摘要（用于全球资产研报内嵌）"""
    h20 = (p.get("horizons") or {}).get("20日") or {}
    t = p.get("timing") or {}
    a = p.get("amp") or {}
    price = p.get("price")

    def px(pct_):
        return "—" if pct_ is None or price is None else f"{price * (1 + pct_ / 100):.2f}"

    icon = {"看多": "📈", "看空": "📉", "震荡": "↔️", "中性": "➡️"}.get(p.get("direction"), "")
    head = (f"**📈 相似形态预测（{p.get('name')}）**：{icon} {p.get('direction')}（{p.get('confidence', '—')}）——"
            f"扫描 {p.get('scan_days')} 个历史交易日，匹配 {p.get('similar_count')} 个最相似时点。"
            f"20日上涨概率 **{h20.get('up_prob')}%**")
    if p.get("direction") == "中性":
        body = ["- ⛔ 三信号（相似形态/七因子/市场环境）未形成多数同向，模型弃权方向猜测（回测显示此状态下命中率≈随机）；"
                "以下历史分布仅供风险管理参考。"]
    else:
        body = [
        f"- 上涨启动：{t.get('up2_hit')}% 样本 20 日内出现≥2%上涨脉冲，首次平均第 {t.get('up2_days_med')} 个交易日；"
        f"次日上涨概率 {t.get('next_day_up')}%",
        f"- 冲高目标：中位 +{a.get('max_gain_med')}%（{px(a.get('max_gain_med'))}），"
        f"乐观 +{a.get('max_gain_p75')}%（{px(a.get('max_gain_p75'))}）",
        f"- 回调风险：{t.get('dn2_hit')}% 样本出现≥2%回调（首次平均第 {t.get('dn2_days_med')} 日），"
        f"回撤中位 {a.get('max_dd_med')}%（{px(a.get('max_dd_med'))}）",
        f"- 收盘目标：20日 {px(None) if h20.get('target') is None else _r(h20.get('target'))}，"
        f"风险低点 {_r(h20.get('low_pt'))}",
    ]
    return head + "\n" + "\n".join(body)


def prompt_global(question, ga, pred=None):
    parts = [f"用户提问：{question}",
             "以下是量化引擎对该全球资产的实时行情与技术面分析（指标体系与A股通用：均线/MACD/RSI/KDJ/"
             "布林带/ATR、多空信号、支撑压力，以及基于全历史数据的相似形态预测）：\n" + _ctx_json(ga)]
    if pred:
        parts.append("相似形态预测数据：\n" + _ctx_json(pred))
    parts.append("请据此回答用户问题：先给方向结论与核心数字（价格/概率/支撑压力），"
                 "再列技术面依据，若有预测数据请回答什么时候涨跌、涨跌多少；结尾强调不构成投资建议。")
    return "\n\n".join(parts)
