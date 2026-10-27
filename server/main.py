# -*- coding: utf-8 -*-
"""QuantAI 智能量化分析平台 - 后端服务"""
import base64
import io
import json
import os
import secrets
import threading
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FTTimeoutError

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import (agent, ai_engine, ai_providers, analysis, attribution, backtest,
               capital_flow, datasource as ds, demon, factors,
               fundamental as fund, global_mkt, heat, institutional, limitup,
               market_data, optimizer, portfolio, prediction, risk, screener,
               signals, strategies, trade, validation, world_data, world_model)

app = FastAPI(title="QuantAI 智能量化分析平台", version="1.0")

app.add_middleware(
    CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"],
)


@app.middleware("http")
async def _no_cache(request, call_next):
    # 禁止代理/浏览器缓存，确保用户总能拿到最新页面与脚本
    resp = await call_next(request)
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    return resp


def _load_basic_auth():
    """读取 Basic Auth 凭据：环境变量优先，其次本地凭据文件（不入代码库）。
    未配置任何凭据时不启用验证，便于本地开发。"""
    if os.environ.get("QUANT_AUTH_DISABLED", "").lower() in ("1", "true", "yes"):
        return None
    user = os.environ.get("QUANT_AUTH_USER")
    pwd = os.environ.get("QUANT_AUTH_PASS")
    if not (user and pwd):
        for path in (os.path.join(os.path.expanduser("~"), ".quantai_auth.json"),
                     os.path.join(ds.BASE_DIR, "auth.json")):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    cfg = json.load(f)
                user, pwd = cfg.get("user"), cfg.get("pass")
                if user and pwd:
                    break
            except Exception:  # noqa: BLE001
                continue
    return (user, pwd) if (user and pwd) else None


_BASIC_AUTH = _load_basic_auth()


@app.middleware("http")
async def _basic_auth(request, call_next):
    # 为公网地址加一层账号密码保护，凭据不会写进代码库
    if not _BASIC_AUTH:
        return await call_next(request)
    header = request.headers.get("authorization", "")
    if header[:6].lower() == "basic ":
        try:
            raw = base64.b64decode(header[6:]).decode("utf-8")
            user, _, pwd = raw.partition(":")
            if (secrets.compare_digest(user, _BASIC_AUTH[0])
                    and secrets.compare_digest(pwd, _BASIC_AUTH[1])):
                return await call_next(request)
        except Exception:  # noqa: BLE001
            pass
    return Response(
        status_code=401,
        content="需要账号密码才能访问 QuantAI",
        media_type="text/plain; charset=utf-8",
        headers={"WWW-Authenticate": 'Basic realm="QuantAI"'},
    )


STATIC_DIR = os.path.join(ds.BASE_DIR, "static")

_ai_lock = threading.Lock()
_json_pool = ThreadPoolExecutor(max_workers=4)


def _json_stream(fn):
    """重计算接口的心跳封装：serveo 免费隧道约 5 秒无数据就回 502，
    这里先在后台线程计算，同时持续输出空格占住连接（JSON 允许前导空白）。"""
    def gen():
        fut = _json_pool.submit(fn)
        while True:
            try:
                data = fut.result(timeout=1.0)
                break
            except FTTimeoutError:
                yield b" "
            except Exception as e:
                yield json.dumps({"detail": f"计算失败: {e}"}, ensure_ascii=False).encode()
                return
        yield json.dumps(data, ensure_ascii=False, default=str).encode()

    return StreamingResponse(gen(), media_type="application/json")


def _warm_caches():
    """后台预热共享缓存：公网隧道对首字节有 ~5 秒限制，冷启动不能拖到首页请求上"""
    for fn in (lambda: portfolio.rank_now("factor"),
               global_mkt.overview,
               heat.compute_heat):
        try:
            fn()
        except Exception:
            pass


@app.on_event("startup")
def _startup():
    ds.ensure_stock_list_async()
    market_data.ensure_async()
    ai_providers.warm_health()
    threading.Thread(target=_warm_caches, daemon=True).start()
    agent.start_background()


def _df(code):
    rows = ds.get_kline(code)
    if not rows:
        raise HTTPException(404, f"未找到 {code} 的行情数据（股票列表可能仍在后台加载，请稍后重试）")
    return analysis.build_df(rows)


def _name(code):
    return ds.get_stock_name(code)


def _local_fundamental_text(q, d):
    """基本面数据结构化文案"""
    v = d.get("valuation", {})
    f = d.get("financial", {})
    p = d.get("peer_comparison", {})
    lines = [f"## 📋 {d.get('name','')} 基本面分析", ""]
    lines.append("### 💰 估值指标")
    lines.append(f"- PE（市盈率）：{v.get('pe','-')}（全市场分位 {p.get('pe_percentile','-')}%）")
    lines.append(f"- PB（市净率）：{v.get('pb','-')}（全市场分位 {p.get('pb_percentile','-')}%）")
    lines.append(f"- 总市值：{v.get('mktcap_yi','-')} 亿元")
    lines.append(f"- 换手率：{v.get('turnover','-')}%")
    lines.append("")
    lines.append("### 📊 财务指标（报告期：{}）".format(f.get("report_date", "-")))
    fin_map = [("eps", "每股收益"), ("roe", "净资产收益率(ROE)"), ("roa", "总资产收益率(ROA)"),
               ("net_margin", "销售净利率"), ("debt_ratio", "资产负债率"),
               ("profit_growth", "净利润增速"), ("current_ratio", "流动比率")]
    for k, nm in fin_map:
        if f.get(k) is not None:
            lines.append(f"- {nm}：{f[k]}")
    lines.append("")
    lines.append("### 🏭 同业对比")
    lines.append(f"- 全市场 PE 中位数：{p.get('pe_median','-')}，该股 PE 分位：{p.get('pe_percentile','-')}%")
    lines.append(f"- 全市场 PB 中位数：{p.get('pb_median','-')}，该股 PB 分位：{p.get('pb_percentile','-')}%")
    lines.append("")
    lines.append("⚠️ 以上数据基于最新财报与实时行情，仅供参考。")
    return "\n".join(lines)


def _local_factors_text(q, d):
    """多因子暴露结构化文案"""
    lines = [f"## 🧮 {d.get('name','')} 多因子分析", ""]
    score = d.get("composite_score")
    if score is not None:
        rating = "优秀" if score >= 70 else ("良好" if score >= 55 else ("一般" if score >= 40 else "偏弱"))
        lines.append(f"**综合因子得分：{score} / 100（{rating}）**")
        lines.append("")
    lines.append("### 五大类因子得分")
    cat_names = {"value": "价值", "growth": "成长", "quality": "质量", "momentum": "动量", "volatility": "波动率"}
    for cat, nm in cat_names.items():
        s = d.get("category_scores", {}).get(cat)
        lines.append(f"- {nm}因子：{s if s is not None else '-'} 分")
    lines.append("")
    lines.append("### 因子明细")
    for k, v in d.get("factors", {}).items():
        nm = d.get("factor_names", {}).get(k, k)
        pct = d.get("percentiles", {}).get(k, "-")
        lines.append(f"- {nm}：{v}（全市场分位 {pct}%）")
    lines.append("")
    lines.append("💡 综合得分由价值(20%)+成长(15%)+质量(25%)+动量(25%)+波动率(15%)加权合成。")
    return "\n".join(lines)


def _local_backtest_text(q, bt):
    """策略回测结构化文案"""
    if "error" in bt:
        return f"❌ 回测失败：{bt['error']}"
    m = bt["metrics"]
    lines = [f"## 📈 {bt.get('name','')} {bt.get('strategy_name','')} 策略回测", ""]
    lines.append(f"**回测区间**：{bt['period']['start']} ~ {bt['period']['end']}（{bt['period']['days']} 个交易日）")
    lines.append("")
    lines.append("### 核心绩效")
    lines.append(f"- 总收益率：{m['total_return']}%（基准买入持有 {m['benchmark_return']}%，超额 {m['excess_return']}%）")
    lines.append(f"- 年化收益(CAGR)：{m['cagr']}%")
    lines.append(f"- 年化波动率：{m['annual_volatility']}%")
    lines.append(f"- 夏普比率：{m['sharpe']} | 索提诺：{m['sortino']} | 卡玛：{m['calmar']}")
    lines.append(f"- 最大回撤：{m['max_drawdown']}%（持续 {m['max_dd_duration_days']} 日）")
    lines.append("")
    lines.append("### 交易统计")
    lines.append(f"- 交易次数：{m['trade_count']} 次")
    lines.append(f"- 胜率：{m['win_rate']}%")
    lines.append(f"- 盈亏比：{m.get('profit_factor','-')}")
    lines.append(f"- 平均盈利：{m['avg_win_pct']}% | 平均亏损：{m['avg_loss_pct']}%")
    lines.append(f"- 平均持仓：{m['avg_hold_days']} 天")
    lines.append("")
    lines.append(f"⚠️ 回测假设：初始资金100万，滑点{bt['params']['slippage']*100}%，佣金{bt['params']['commission']*100}%，印花税{bt['params']['stamp_tax']*100}%。历史回测不代表未来收益。")
    return "\n".join(lines)


def _local_capital_flow_text(q, cf):
    """资金流向结构化文案"""
    lines = [f"## 💰 {cf.get('name','')} 资金流向分析", ""]
    lines.append(f"**主力资金信号：{cf.get('flow_signal','')}（{cf.get('flow_verdict','')}）**")
    if cf.get("latest_net_amount_yi") is not None:
        lines.append(f"最新主力净流入：{cf['latest_net_amount_yi']} 亿元")
    lines.append("")
    flows = cf.get("money_flow", [])
    if flows:
        lines.append("### 📊 近5日资金流向")
        for r in flows[:5]:
            net_yi = round(r["net_amount"] / 1e8, 2) if r.get("net_amount") else 0
            arrow = "📈" if net_yi > 0 else "📉"
            lines.append(f"- {r['date']}：主力净流入 {net_yi} 亿 {arrow}（收盘 {r.get('close')}）")
        lines.append("")
    nb = cf.get("northbound")
    if nb:
        lines.append("### 🌐 北向资金持股")
        lines.append(f"- 持股日期：{nb.get('date','-')}")
        lines.append(f"- 持股数量：{nb.get('hold_shares','-')} 股")
        lines.append(f"- 持股市值：{round(nb['hold_market_cap']/1e8,2)} 亿元" if nb.get('hold_market_cap') else "")
        lines.append(f"- 占A股流通比例：{nb.get('a_shares_ratio','-')}%")
        lines.append("")
    lhb = cf.get("dragon_tiger", [])
    if lhb:
        lines.append("### 🐯 龙虎榜（近期上榜）")
        for r in lhb[:3]:
            net_yi = round(r["net_amount"] / 1e8, 2) if r.get("net_amount") else 0
            lines.append(f"- {r['date']}：净买入 {net_yi} 亿 | {r.get('reason','')}")
        lines.append("")
    if not (flows or nb or lhb):
        lines.append("暂无资金流向数据。")
    return "\n".join(lines)


# ---------------------------------------------------------------- 股票搜索 / 基础


class SearchReq(BaseModel):
    q: str = ""


# 自然语言提问的特征词（用于"搜索即提问"）
ASK_KEYWORDS = ("?", "？", "吗", "呢", "怎么", "如何", "为什么", "为何", "多少", "会不会",
                "能否", "是否", "还是", "什么时候", "走势", "分析", "预测", "目标价", "前景",
                "买入", "卖出", "加仓", "减仓", "抄底", "逃顶", "怎么办", "啥时候",
                "什么", "哪些", "哪个", "持续", "多久", "推荐", "翻倍", "潜力", "牛股",
                "涨停", "连板", "断板", "几个板", "打板", "封板", "炸板")

# 涨停连板类问题（优先于选股判断）
LIMITUP_KEYWORDS = ("涨停", "连板", "断板", "几个板", "打板", "封板", "炸板", "空间板")

# 妖股类问题（优先于涨停判断：点名个股→个股妖股研判；未点名→全市场妖股扫描）
DEMON_KEYWORDS = ("妖股", "成妖", "走妖", "妖性", "妖王", "妖气", "成精")

# 个股买卖点类问题（问题中点名股票或使用当前标的时命中）
BUYSELL_KEYWORDS = ("买入", "卖出", "买点", "卖点", "止损", "止盈", "什么时候买", "什么时候卖",
                    "进场", "出场", "建仓", "清仓", "加仓", "减仓", "什么价", "多少价位")

# 个股盘后预测类问题（优先于买卖点：预测未来涨跌方向、幅度与时点）
# 注意：不用裸"会上涨/会下跌"，避免与全市场选股问题"什么股票会上涨"误路由
PREDICT_KEYWORDS = ("预测", "预判", "预演", "未来", "接下来", "涨多少", "跌多少",
                    "还会涨", "还会跌", "能涨到", "能跌到", "会涨到", "会跌到",
                    "什么时候涨", "什么时候跌", "什么时候会涨", "什么时候会跌",
                    "什么时候会上涨", "什么时候会下跌", "什么时候开始涨", "什么时候开始跌",
                    "什么时候上涨", "什么时候下跌", "开始上涨", "开始下跌",
                    "上涨多少", "下跌多少", "上涨空间", "下跌空间",
                    "会不会上涨", "会不会下跌", "会不会涨", "会不会跌",
                    "会上涨吗", "会下跌吗", "涨到多少", "跌到多少", "下跌到")

# 全市场选股类问题的特征词（问题中不含具体股票名时命中）
SCAN_KEYWORDS = ("什么股票", "哪些股票", "哪只股票", "什么票", "哪些票", "哪只票",
                 "推荐股票", "推荐几只", "推荐个", "推荐一只", "推荐一支", "推荐一个",
                 "推荐一只票", "推荐一只股", "推荐一支股", "推荐一支票", "推荐下",
                 "给我推荐", "帮我推荐", "选股", "买什么", "买哪", "有什么股票",
                 "有什么票", "值得买", "值得关", "会上涨的", "会涨的", "能涨的",
                 "现在买", "现在买入", "买入会涨", "买入之后", "涨得好", "强势股",
                 "潜力股", "牛股", "翻倍", "什么板块", "哪些板块",
                 "什么行业", "哪些行业", "热点股", "龙头股",
                 "做短线", "做中线", "做长线", "短线还是", "中线还是", "长线还是",
                 "一直赚钱", "持续赚钱", "稳定赚钱", "怎么赚钱", "能赚多少",
                 "明天买入", "明天买", "明日买入", "明天买什么", "明天买哪只",
                 "收益最大化", "收益最大", "最真实", "保证收益", "保证能买", "保证能卖")

# 策略偏好关键词（用于在选股类问题中分派到对应私募模型）
MR_KEYWORDS = ("抄底", "超跌", "反弹", "低位", "低吸", "跌多了", "跌出来", "超卖", "低估", "捡便宜")
TB_KEYWORDS = ("突破", "趋势", "新高", "强势启动", "主升", "追涨", "放量突破", "平台突破")

# 短期最佳推荐关键词：用户要"最可能赚钱/概率最高"的单只票（优先于普通选股）
BESTPICK_KEYWORDS = ("最能赚钱", "最赚钱", "最能涨", "短线最", "短期最", "短期赚钱",
                     "概率最高", "最有把握", "最稳", "最确定", "最推荐", "最值得买",
                     "最可能涨", "最可能上涨", "短期最可能", "短线赚钱", "短期稳赚",
                     "最有潜力", "最强", "最看好", "机会最大", "收益最高", "最可能赚钱",
                     "买什么票", "买哪只票", "现在买什么", "现在买哪只", "买哪个票",
                     "哪只票最", "买什么股票", "现在买什么股", "什么票最", "什么股最",
                     "做短线", "做中线", "做长线", "短线还是", "中线还是", "长线还是",
                     "一直赚钱", "持续赚钱", "稳定赚钱", "怎么赚钱",
                     "明天买入", "明天买", "明日买入", "明天买什么", "明天买哪只",
                     "收益最大化", "收益最大", "最真实", "保证收益", "保证能买", "保证能卖")

# 第一阶段新能力关键词
FUND_KEYWORDS = ("基本面", "财务", "财报", "估值", "PE", "PB", "市盈率", "市净率", "ROE",
                 "毛利率", "净利率", "资产负债", "同业对比", "同行对比", "行业对比")
FACTOR_KEYWORDS = ("因子", "多因子", "因子暴露", "价值因子", "成长因子", "质量因子",
                   "动量因子", "波动率因子", "IC", "IC评估", "因子有效性")
BT_KEYWORDS = ("回测", "历史回测", "策略回测", "回测表现", "历史表现", "夏普",
               "最大回撤", "胜率", "盈亏比", "净值曲线", "策略验证")
FLOW_KEYWORDS = ("资金流向", "主力资金", "资金流入", "资金流出", "北向", "北向资金",
                 "沪股通", "深股通", "龙虎榜", "融资融券", "大单", "超大单")


def _ask_hint(q):
    """判断输入是否像自然语言提问，并尝试识别其中提到的股票"""
    q = (q or "").strip()
    if len(q) < 4 or not any(k in q for k in ASK_KEYWORDS):
        return None
    hit = ds.resolve_stock_in_text(q)
    return dict(question=q, code=hit[0] if hit else "", name=hit[1] if hit else "")


_CN_NUM = {"零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5,
           "六": 6, "七": 7, "八": 8, "九": 9}


def _parse_capital(text):
    """从自然语言中提取资金金额（元），支持「一万块」「5万」「10000元」「两千元」等。

    解析规则：
    - 阿拉伯数字 + 单位：10000 / 1万 / 5.5万 / 100万 / 1亿
    - 中文数字 + 单位：一万 / 两万 / 十万 / 一百万 / 一千
    - 纯数字视为元
    返回 int（元）或 None
    """
    import re
    text = text or ""
    # 单位映射
    unit_map = {"块": 1, "元": 1, "千": 1000, "w": 10000, "W": 10000,
                "万": 10000, "百万": 1_000_000, "千万": 10_000_000, "亿": 100_000_000}
    # 模式1：阿拉伯数字 + 万/亿/千/块/元
    m = re.search(r"(\d+(?:\.\d+)?)\s*(万|亿|千|块|元|w|W)", text)
    if m:
        num = float(m.group(1))
        unit = m.group(2)
        mult = {"万": 10000, "亿": 100_000_000, "千": 1000, "块": 1, "元": 1, "w": 10000, "W": 10000}[unit]
        return int(num * mult)
    # 模式2：中文数字 + 万/亿/千/块/元
    m = re.search(r"([一二两三四五六七八九十百]+)\s*(万|亿|千|块|元)", text)
    if m:
        cn = m.group(1)
        unit = m.group(2)
        # 解析中文数字（支持 十/二十/一百 等简单组合）
        val = 0
        if cn.startswith("十"):
            val = 10 + (_CN_NUM.get(cn[1], 0) if len(cn) > 1 else 0)
        elif "十" in cn:
            parts = cn.split("十")
            tens = _CN_NUM.get(parts[0], 1)
            val = tens * 10 + (_CN_NUM.get(parts[1], 0) if len(parts) > 1 and parts[1] else 0)
        elif "百" in cn:
            parts = cn.split("百")
            val = _CN_NUM.get(parts[0], 1) * 100
            if len(parts) > 1 and parts[1]:
                val += _CN_NUM.get(parts[1], 0)
        else:
            val = _CN_NUM.get(cn, 0)
        mult = {"万": 10000, "亿": 100_000_000, "千": 1000, "块": 1, "元": 1}[unit]
        return int(val * mult)
    # 模式3：纯阿拉伯数字（至少 3 位，避免误匹配年份/代码）
    m = re.search(r"(\d{3,}(?:\.\d+)?)", text)
    if m:
        num = float(m.group(1))
        if num >= 100:  # 100 元起才视为资金
            return int(num)
    return None


@app.get("/api/stock/search")
def api_search(q: str = ""):
    return dict(results=ds.search_stocks(q), ask=_ask_hint(q), loaded=ds.stocks_loaded())


@app.post("/api/stock/search")
def api_search_post(req: SearchReq):
    # POST 版本：部分代理会解码 query 中的百分号编码导致中文请求行非法
    results = ds.search_stocks(req.q)
    ask = _ask_hint(req.q)
    print(f"[search] q={req.q!r} -> {len(results)} 条 {[r['code'] for r in results[:3]]} ask={'Y' if ask else 'N'}", flush=True)
    return dict(results=results, ask=ask, loaded=ds.stocks_loaded())


@app.get("/api/stock/{code}/quote")
def api_quote(code: str):
    """轻量实时行情（切换个股时刷新顶部行情条）"""
    quote = ds.get_stock_quote(code)
    if not quote:
        raise HTTPException(404, "未找到该股票实时行情")
    pct = round((quote["price"] / quote["prev_close"] - 1) * 100, 2) if quote["prev_close"] else 0
    q = dict(price=quote["price"], pct=pct, open=quote["open"], high=quote["high"], low=quote["low"],
             prev_close=quote["prev_close"], volume=int(quote["volume"]),
             amount=round(quote["amount"] / 1e8, 2), time=f"{quote['date']} {quote['time']}")
    return dict(code=ds.norm_code(code), name=_name(code), quote=q)


@app.get("/api/stock/{code}/daily")
def api_daily(code: str, days: int = 250):
    df = _df(code)
    tail = df.tail(days).copy()
    tail = analysis.add_indicators(tail)
    quote = ds.get_stock_quote(code) or {}
    pct = float(tail["pct"].iloc[-1]) if len(tail) > 1 else 0
    close = float(tail["close"].iloc[-1])
    prev_close = float(tail["close"].iloc[-2]) if len(tail) > 1 else close
    summary = dict(
        period_high=round(float(tail["high"].max()), 2),
        period_low=round(float(tail["low"].min()), 2),
        period_pct=round((close / float(tail["close"].iloc[0]) - 1) * 100, 2),
        up_days=int((tail["pct"] > 0).sum()),
        down_days=int((tail["pct"] < 0).sum()),
        avg_volume=int(tail["volume"].mean()),
    )
    if quote:
        q = dict(price=quote["price"], pct=round((quote["price"] / quote["prev_close"] - 1) * 100, 2) if quote["prev_close"] else pct,
                 open=quote["open"], high=quote["high"], low=quote["low"],
                 prev_close=quote["prev_close"], volume=int(quote["volume"]), amount=round(quote["amount"] / 1e8, 2),
                 time=f"{quote['date']} {quote['time']}")
    else:
        q = dict(price=close, pct=round(pct, 2), open=float(tail["open"].iloc[-1]),
                 high=float(tail["high"].iloc[-1]), low=float(tail["low"].iloc[-1]),
                 prev_close=prev_close, volume=int(tail["volume"].iloc[-1]), amount=None, time=str(tail["day"].iloc[-1]))
    return dict(code=ds.norm_code(code), name=_name(code), quote=q, summary=summary,
                kline=analysis.serialize_kline(tail))


@app.get("/api/stock/{code}/history")
def api_history(code: str):
    df = _df(code)
    # 全量 K 线用紧凑数组传输（公网隧道带宽有限，字典数组约 1.8MB 易被截断）
    return analysis.full_history_analysis(ds.norm_code(code), _name(code), df, compact_kline=True)


@app.get("/api/stock/{code}/history.csv")
def api_history_csv(code: str):
    df = _df(code)
    buf = io.StringIO()
    df.to_csv(buf, index=False, encoding="utf-8-sig")
    buf.seek(0)
    filename = f"{ds.norm_code(code)}_full_history.csv"
    return StreamingResponse(iter([buf.read()]), media_type="text/csv",
                             headers={"Content-Disposition": f"attachment; filename={filename}"})


# ---------------------------------------------------------------- 分析


@app.get("/api/stock/{code}/analysis")
def api_analysis(code: str):
    df = _df(code)
    ctx = analysis.daily_analysis(ds.norm_code(code), _name(code), df)
    ctx["ai_text"] = ai_engine.local_daily_text(ctx)
    return ctx


@app.get("/api/stock/{code}/analysis/full")
def api_analysis_full(code: str):
    df = _df(code)
    ctx = analysis.full_history_analysis(ds.norm_code(code), _name(code), df)
    ctx["ai_text"] = ai_engine.local_full_text(ctx)
    return ctx


@app.get("/api/stock/{code}/shortterm")
def api_shortterm(code: str):
    df = _df(code)
    ctx = analysis.short_term_analysis(ds.norm_code(code), _name(code), df)
    ctx["ai_text"] = ai_engine.local_short_text(ctx)
    return ctx


# ---------------------------------------------------------------- 第一阶段新能力：基本面/多因子/策略回测/资金流向


@app.get("/api/stock/{code}/fundamental")
def api_fundamental(code: str):
    """基本面数据：财务指标 + 估值 + 同业分位"""
    return fund.get_fundamental(code)


@app.get("/api/stock/{code}/factors")
def api_factors(code: str):
    """多因子暴露：价值/成长/质量/动量/波动率 五大类因子 + 综合得分"""
    return factors.get_factor_exposure(code)


@app.get("/api/factors/ic")
def api_factors_ic(factor: str = "600519"):
    """因子IC评估（时序IC）"""
    return factors.evaluate_ic(factor)


@app.get("/api/stock/{code}/backtest/strategy")
def api_strategy_backtest(code: str, strategy: str = "mr", days: int = 500):
    """策略回测：mr均值回归 / tb趋势突破 / momentum动量"""
    return backtest.run_backtest(strategy, code, days)


@app.get("/api/stock/{code}/capital_flow")
def api_capital_flow(code: str):
    """资金流向：个股资金流 + 北向持股 + 龙虎榜"""
    return capital_flow.get_capital_flow(code)


# ---------------------------------------------------------------- 第二阶段：组合回测 / 横截面数据


@app.get("/api/portfolio/universe")
def api_portfolio_universe(size: int = 40):
    """默认组合股票池：按行业分散抽样（市值/PE/ROE/增速过滤）"""
    size = max(10, min(80, int(size)))
    u = portfolio.default_universe(size)
    return dict(count=len(u), industries=len(set(x["industry"] for x in u)),
                stocks=u, data_status=market_data.status())


@app.get("/api/portfolio/backtest")
def api_portfolio_backtest(codes: str = "", strategy: str = "factor", days: int = 500,
                           rebalance: int = 20, max_holdings: int = 10,
                           max_industry_weight: float = 0.35, weighting: str = "equal",
                           stop_loss: float = 0, universe_size: int = 40):
    """多股票组合回测：行业约束 + 仓位管理 + 周期再平衡

    codes: 逗号分隔的股票代码，留空使用默认行业分散池
    strategy: factor 多因子打分 / momentum 动量 / mr 均值回归 / tb 趋势突破
    weighting: equal 等权 / score 评分加权 / risk 逆波动率
    stop_loss: 单票止损比例（0 关闭，0.1 表示 -10%）
    """
    code_list = [c.strip() for c in codes.replace("，", ",").split(",") if c.strip()] or None
    size = max(10, min(80, int(universe_size)))
    return _json_stream(lambda: portfolio.run_portfolio_backtest(
        codes=code_list, strategy=strategy, days=days, rebalance=rebalance,
        max_holdings=max_holdings, max_industry_weight=max_industry_weight,
        weighting=weighting, stop_loss=stop_loss if stop_loss > 0 else None,
        universe_size=size,
    ))


# ---------------------------------------------------------------- 第二阶段：实盘信号 / 持仓监控 / 风险 / 归因 / 优化


class HoldingsBody(BaseModel):
    items: list = []


class PushBody(BaseModel):
    webhook: str = ""
    strategy: str = "factor"
    top: int = 10


def _parse_weights(s):
    """'600519:30,000001:20' -> {code: weight}"""
    out = {}
    for part in (s or "").replace("，", ",").split(","):
        if ":" not in part:
            continue
        c, v = part.split(":", 1)
        c = c.strip()
        try:
            out[c] = float(v)
        except ValueError:
            continue
    return out


def _parse_codes(s):
    return [c.strip() for c in (s or "").replace("，", ",").split(",") if c.strip()]


@app.get("/api/signals/daily")
def api_signals_daily(strategy: str = "factor", top: int = 10,
                      max_industry_weight: float = 0.35):
    """实盘信号：目标组合 + 买入/持有/卖出动作"""
    top = max(3, min(20, int(top)))
    return signals.generate_signals(strategy=strategy, max_holdings=top,
                                     max_industry_weight=max_industry_weight)


@app.get("/api/signals/brief")
def api_signals_brief(strategy: str = "factor", top: int = 10):
    """每日盯盘文本简报"""
    return _json_stream(lambda: signals.daily_brief(
        strategy=strategy, top=max(3, min(20, int(top)))))


@app.get("/api/signals/monitor")
def api_signals_monitor():
    """持仓监控 + 异动预警"""
    return signals.monitor()


@app.get("/api/holdings")
def api_holdings_list():
    return signals.list_holdings()


@app.post("/api/holdings")
def api_holdings_set(body: HoldingsBody):
    """整体替换持仓：[{"code":"600519","shares":100,"cost":1500}, ...]"""
    return signals.set_holdings(body.items or [])


@app.post("/api/holdings/upsert")
def api_holdings_upsert(body: dict):
    return signals.upsert_holding(body.get("code"), body.get("shares"), body.get("cost"))


@app.delete("/api/holdings/{code}")
def api_holdings_delete(code: str):
    return signals.remove_holding(code)


@app.post("/api/signals/push")
def api_signals_push(body: PushBody):
    """把每日简报推送到 Webhook（钉钉/企业微信/飞书机器人）"""
    brief = signals.daily_brief(strategy=body.strategy, top=max(3, min(20, body.top)))
    if brief.get("error"):
        raise HTTPException(503, brief["error"])
    return signals.push(brief["text"], body.webhook or None)


@app.get("/api/portfolio/risk")
def api_portfolio_risk(codes: str = "", weights: str = "", days: int = 250,
                       capital: float = 1_000_000):
    """组合风险模型：波动率/VaR/Beta/回撤/行业与风格暴露/风险贡献/风险平价"""
    return risk.analyze(codes=_parse_codes(codes) or None,
                        weights=_parse_weights(weights) or None,
                        days=days, capital=capital)


@app.get("/api/portfolio/attribution")
def api_portfolio_attribution(codes: str = "", weights: str = "", days: int = 120):
    """收益归因：Brinson 行业配置 / 个股选择 / 交互效应"""
    return attribution.analyze(codes=_parse_codes(codes) or None,
                               weights=_parse_weights(weights) or None, days=days)


@app.get("/api/optimize/mix")
def api_optimize_mix(strategies: str = "factor,momentum,tb,mr", days: int = 500,
                     rebalance: int = 20, max_holdings: int = 10,
                     max_industry_weight: float = 0.35, lookback: int = 60):
    """多策略组合优化：等权 / 逆波动率 / 动量加权"""
    return _json_stream(lambda: optimizer.optimize_mix(
        strategies=tuple(_parse_codes(strategies)),
        days=days, rebalance=rebalance,
        max_holdings=max_holdings,
        max_industry_weight=max_industry_weight,
        lookback=lookback))


@app.get("/api/optimize/grid")
def api_optimize_grid(strategy: str = "factor", days: int = 500,
                      train_ratio: float = 0.7, max_industry_weight: float = 0.35):
    """参数网格寻优 + 样本内外验证"""
    return _json_stream(lambda: optimizer.grid_search(
        strategy=strategy, days=days,
        train_ratio=train_ratio,
        max_industry_weight=max_industry_weight))


# ---------------------------------------------------------------- 机构级验证

@app.get("/api/validation/ic")
def api_validation_ic(codes: str = "", days: int = 500):
    """横截面因子 IC / ICIR / t 值（纯价格因子，无未来函数）"""
    return _json_stream(lambda: validation.cross_section_ic(
        codes=_parse_codes(codes) or None, days=max(120, min(1200, days))))


@app.get("/api/validation/walkforward")
def api_validation_walkforward(codes: str = "", days: int = 750, top_n: int = 10,
                               rebalance: int = 20, cost_bps: float = 10):
    """滚动样本外验证：前 60% 选因子，后 40% 纯执行，量化过拟合衰减"""
    return _json_stream(lambda: validation.walk_forward(
        codes=_parse_codes(codes) or None, days=max(240, min(1200, days)),
        top_n=max(3, min(30, top_n)), rebalance=max(5, min(60, rebalance)),
        cost_bps=max(0, min(100, cost_bps))))


@app.get("/api/validation/calibration")
def api_validation_calibration(code: str = "000001", days: int = 500, step: int = 5,
                                k: int = 40):
    """相似形态预测概率校准：Brier 分数 + 分桶命中率"""
    return validation.calibration(code=ds.norm_code(code) or "000001",
                                  days=max(200, min(1200, days)),
                                  step=max(2, min(20, step)), k=max(10, min(80, k)))


@app.get("/api/validation/audit")
def api_validation_audit(codes: str = ""):
    """数据与回测偏差审计"""
    return validation.audit(codes=_parse_codes(codes) or None)


@app.get("/api/validation/gap")
def api_validation_gap():
    """与私募量化基金的能力对标矩阵"""
    return validation.gap_matrix()


@app.get("/api/validation/paper")
def api_validation_paper(days: int = 500, top_n: int = 10, rebalance: int = 20, cost_bps: float = 10):
    """Paper trading：逐日模拟盘净值跟踪 + 交易记录"""
    return _json_stream(lambda: validation.paper_trade(
        days=max(240, min(1200, days)),
        top_n=max(3, min(30, top_n)),
        rebalance=max(5, min(60, rebalance)),
        cost_bps=max(0, min(200, cost_bps)),
    ))


# ---------------------------------------------------------------- 机构级能力（顶级私募对标）

@app.get("/api/inst/barra")
def api_inst_barra(codes: str = "", days: int = 250):
    """Barra 风格风险模型：风格/行业暴露 + 因子协方差 + 组合风险分解"""
    return _json_stream(lambda: institutional.barra_model(
        codes=_parse_codes(codes) or None, days=max(60, min(750, days))))


@app.get("/api/inst/optimize")
def api_inst_optimize(codes: str = "", days: int = 400, top_n: int = 12,
                      risk_aversion: float = 3.0, max_weight: float = 0.12,
                      max_industry: float = 0.35, style_cap: float = 1.0,
                      turnover_penalty: float = 0.5, cost_bps: float = 15):
    """带约束组合优化（单票/行业/风格/换手约束，投影梯度求解）"""
    return _json_stream(lambda: institutional.optimize_portfolio(
        codes=_parse_codes(codes) or None, days=max(120, min(900, days)),
        top_n=max(5, min(30, top_n)), risk_aversion=max(0.1, min(30.0, risk_aversion)),
        max_weight=max(0.02, min(0.5, max_weight)),
        max_industry=max(0.1, min(1.0, max_industry)),
        style_cap=max(0.2, min(3.0, style_cap)),
        turnover_penalty=max(0.0, min(10.0, turnover_penalty)),
        cost_bps=max(0.0, min(200.0, cost_bps))))


@app.get("/api/inst/tca")
def api_inst_tca(codes: str = "", days: int = 400, participation: float = 0.10):
    """算法交易执行对比 + Almgren-Chriss 冲击成本 + 容量曲线"""
    return _json_stream(lambda: institutional.execution_tca(
        codes=_parse_codes(codes) or None, days=max(120, min(900, days)),
        participation=max(0.01, min(0.5, participation))))


@app.get("/api/inst/dsr")
def api_inst_dsr(codes: str = "", days: int = 600):
    """Deflated Sharpe Ratio + PBO 过拟合概率"""
    return _json_stream(lambda: institutional.deflated_sharpe(
        codes=_parse_codes(codes) or None, days=max(200, min(1200, days))))


@app.get("/api/inst/decay")
def api_inst_decay(codes: str = "", days: int = 500):
    """因子 IC 衰减曲线与半衰期"""
    return _json_stream(lambda: institutional.factor_decay(
        codes=_parse_codes(codes) or None, days=max(150, min(1000, days))))


@app.get("/api/inst/ml")
def api_inst_ml(codes: str = "", days: int = 700, horizon: int = 5):
    """GBDT 非线性因子合成 vs 线性 ICIR 加权（样本外 IC 对比）"""
    return _json_stream(lambda: institutional.ml_alpha(
        codes=_parse_codes(codes) or None, days=max(300, min(1200, days)),
        horizon=max(1, min(20, horizon))))


@app.get("/api/inst/alloc")
def api_inst_alloc(codes: str = "", days: int = 600, horizon: int = 5):
    """多策略资金配置（等权/风险平价/最小方差/最大夏普）"""
    return _json_stream(lambda: institutional.strategy_alloc(
        codes=_parse_codes(codes) or None, days=max(200, min(1200, days)),
        horizon=max(1, min(20, horizon))))


@app.get("/api/inst/matrix")
def api_inst_matrix():
    """顶级私募「功能 × 数据支撑 × 个人劣势」全景矩阵"""
    return institutional.inst_matrix()


# ---------------------------------------------------------------- 引擎回测验证

@app.get("/api/backtest")
def api_backtest(code: str = "000001", samples: int = 15):
    """历史回测验证：用截断信息流模拟当时视角，检验引擎信号的真实准确性。
    返回：相似形态预测的方向命中率（vs 随机50%）、概率校准度、七因子评分的收益区分度。"""
    import numpy as np

    code = ds.norm_code(code) or "000001"
    name = _name(code)
    samples = max(8, min(25, int(samples)))
    rows = ds.get_kline(code)
    if not rows:
        raise HTTPException(404, f"未找到 {code} 的行情数据")
    full = analysis.build_df(rows)
    n = len(full)
    if n < 320:
        raise HTTPException(400, f"{name} 历史仅{n}个交易日（需≥320），无法回测")

    # A. 相似形态预测引擎回测：截断到 t 日做预测，对比其后20日实际走势
    spots = np.linspace(n - 260, n - 62, samples).astype(int)
    detail = []
    probs, acts = [], []
    for t in spots:
        cut = full.iloc[: t + 1].reset_index(drop=True)
        try:
            pred = prediction.predict(code, name, cut)
        except Exception:  # noqa: BLE001
            continue
        act = float(full["close"].iloc[t + 20] / full["close"].iloc[t] - 1) * 100
        d = pred["direction"]
        ok = (d == "看多" and act > 0) or (d == "看空" and act < 0) or (d not in ("看多", "看空") and abs(act) <= 3)
        probs.append(float(pred["horizons"]["20日"]["up_prob"] or 50))
        acts.append(act)
        detail.append(dict(date=str(cut["day"].iloc[-1]), pred=d, conf=pred.get("confidence", ""),
                           up_prob=pred["horizons"]["20日"]["up_prob"], actual=round(act, 2), ok=bool(ok)))
    if not detail:
        raise HTTPException(503, "预测引擎回测失败，请稍后重试")
    # 统计口径：方向命中率只统计"引擎开口"（看多/看空）的时点；
    # 中性弃权不计对错，单独报告弃权后实际走势平淡（|涨跌|≤3%）的比例。
    dir_rows = [d_ for d_ in detail if d_["pred"] in ("看多", "看空")]
    dir_hit = sum(1 for d_ in dir_rows if d_["ok"])
    rate = round(dir_hit / len(dir_rows) * 100, 1) if dir_rows else None
    pa, aa = np.array(probs), np.array(acts)
    med = float(np.median(pa))
    calibration = dict(
        hi_avg=round(float(aa[pa >= med].mean()), 2),
        lo_avg=round(float(aa[pa < med].mean()), 2),
        corr=round(float(np.corrcoef(pa, aa)[0, 1]), 3),
    )
    neutral = [d_ for d_ in detail if d_["pred"] not in ("看多", "看空")]
    neutral_ok = sum(1 for d_ in neutral if abs(d_["actual"]) <= 3)

    # B. 七因子评分回测：评分看多信号日 vs 全体（后续5日收益区分度）
    tail = full.tail(1200)
    m = len(tail)
    sc_list, r5_list = [], []
    for t in range(260, m - 6, 5):
        cut = tail.iloc[: t + 1].reset_index(drop=True)
        try:
            st = analysis.short_term_analysis(code, name, cut)
        except Exception:  # noqa: BLE001
            continue
        sc_list.append(float(st["score"]))
        r5_list.append(float((tail["close"].iloc[t + 5] / tail["close"].iloc[t] - 1) * 100))
    if len(sc_list) < 20:
        raise HTTPException(503, "七因子回测样本不足")
    sc_a, r5_a = np.array(sc_list), np.array(r5_list)
    bull_m = sc_a >= 30
    factor = dict(
        samples=int(bull_m.sum()),
        bull_avg=round(float(r5_a[bull_m].mean()), 2) if bull_m.sum() else None,
        bull_win=round(float((r5_a[bull_m] > 0).mean() * 100), 1) if bull_m.sum() else None,
        base_avg=round(float(r5_a.mean()), 2),
        base_win=round(float((r5_a > 0).mean() * 100), 1),
    )
    factor["effective"] = bool(factor["bull_avg"] is not None and factor["bull_avg"] > factor["base_avg"])

    return dict(code=code, name=name, total_days=n,
                pred=dict(total=len(detail), gave_direction=len(dir_rows),
                          dir_hit=dir_hit, rate=rate,
                          neutral=len(neutral),
                          neutral_ok=neutral_ok if neutral else None,
                          neutral_flat=round(neutral_ok / len(neutral) * 100, 1) if neutral else None,
                          calibration=calibration, detail=detail),
                factor=factor)


# ---------------------------------------------------------------- 市场热度


@app.get("/api/market/heat")
def api_heat():
    try:
        ctx = heat.compute_heat()
    except RuntimeError as e:
        raise HTTPException(503, str(e))
    ctx["ai_text"] = ai_engine.local_heat_text(ctx)
    return ctx


# ---------------------------------------------------------------- AI


class AnalyzeReq(BaseModel):
    code: str
    type: str  # daily | full | short | heat


@app.post("/api/ai/analyze")
def api_ai_analyze(req: AnalyzeReq):
    with _ai_lock:  # 大模型串行，避免并发限流
        try:
            if req.type == "heat":
                ctx = heat.compute_heat()
                text = ai_engine.ai_chat(ai_engine.prompt_heat(ctx))
                if not text:
                    text = ai_engine.local_heat_text(ctx)
                return dict(text=text, engine=ai_engine.status()["engine"])
            df = _df(req.code)
            code, name = ds.norm_code(req.code), _name(req.code)
            if req.type == "daily":
                ctx = analysis.daily_analysis(code, name, df)
                text = ai_engine.ai_chat(ai_engine.prompt_daily(ctx)) or ai_engine.local_daily_text(ctx)
            elif req.type == "full":
                ctx = analysis.full_history_analysis(code, name, df)
                text = ai_engine.ai_chat(ai_engine.prompt_full(ctx)) or ai_engine.local_full_text(ctx)
            elif req.type == "short":
                ctx = analysis.short_term_analysis(code, name, df)
                text = ai_engine.ai_chat(ai_engine.prompt_short(ctx)) or ai_engine.local_short_text(ctx)
            else:
                raise HTTPException(400, "type 必须是 daily/full/short/heat")
            return dict(text=text, engine=ai_engine.status()["engine"])
        except HTTPException:
            raise
        except Exception as e:  # noqa: BLE001
            raise HTTPException(500, f"AI分析失败: {e}")


class AskReq(BaseModel):
    question: str
    code: str = ""


@app.post("/api/ai/ask")
def api_ai_ask(req: AskReq):
    with _ai_lock:
        # 优先从问题文本中自动识别股票（搜索即提问），未识别到再用前端传入的标的
        code = (req.code or "").strip()
        q = req.question
        hit = ds.resolve_stock_in_text(q)
        # 防误判：问题含"什么股票/哪些股票"等强选股指示词时，问的是全市场而非撞名个股
        #（如 A股"值得买"300785 撞上"有什么股票值得买入"）
        STRONG_SCAN_WORDS = ("什么股票", "哪些股票", "哪只股票", "什么票", "哪些票",
                             "有什么股票", "有什么票", "什么板块", "哪些板块")
        if hit and any(k in q for k in STRONG_SCAN_WORDS):
            hit = None
        if hit:
            code = hit[0]
        # 妖股类问题（优先于涨停/选股）：点名个股→个股妖股研判；未点名→全市场妖股扫描
        if any(k in q for k in DEMON_KEYWORDS):
            d = dscan = None
            try:
                if hit:
                    ddf = _df(hit[0])
                    d = demon.demon_analysis(ds.norm_code(hit[0]), _name(hit[0]), ddf)
                else:
                    dscan = demon.scan_demon()
            except RuntimeError as e:
                raise HTTPException(503, str(e))
            if ai_engine.status()["configured"]:
                if hit:
                    text = ai_engine.ai_chat(ai_engine.prompt_demon(q, d)) \
                        or ai_engine.local_demon_text(q, d)
                else:
                    text = ai_engine.ai_chat(ai_engine.prompt_demon_scan(q, dscan)) \
                        or ai_engine.local_demon_scan_text(q, dscan)
            else:
                if hit:
                    text = ai_engine.local_demon_text(q, d)
                else:
                    text = ai_engine.local_demon_scan_text(q, dscan)
            return dict(text=text, engine=ai_engine.status()["engine"],
                        code=ds.norm_code(hit[0]) if hit else "", name=hit[1] if hit else "",
                        type="demon")
        # 涨停连板类问题（优先于选股）：走涨停扫描引擎
        if not hit and any(k in q for k in LIMITUP_KEYWORDS):
            try:
                lu = limitup.scan_limitup()
            except RuntimeError as e:
                raise HTTPException(503, str(e))
            if ai_engine.status()["configured"]:
                text = ai_engine.ai_chat(ai_engine.prompt_limitup(q, lu)) \
                    or ai_engine.local_limitup_text(q, lu)
            else:
                text = ai_engine.local_limitup_text(q, lu)
            return dict(text=text, engine=ai_engine.status()["engine"],
                        code="", name="", type="limitup")
        # 个股盘后预测问题：预测未来什么时候涨/跌、涨多少/跌多少（优先于买卖点）
        # 未点名个股且问的是全市场选股（"未来哪些股票会涨"）时让位给选股引擎
        # 问题指向全球资产（A股未命中）时同样让位——不能用当前标的顶替（如默认000001）
        ghit = None if hit else global_mkt.resolve_global_in_text(q)
        if ghit and any(k in q for k in STRONG_SCAN_WORDS):
            ghit = None  # "黄金板块什么股票会涨"这类全市场选股问题让位给扫描引擎
        pred_code = hit[0] if hit else code
        if (pred_code and not ghit and any(k in q for k in PREDICT_KEYWORDS)
                and not (not hit and any(k in q for k in SCAN_KEYWORDS))):
            try:
                pdf = _df(pred_code)
                pr = prediction.predict(ds.norm_code(pred_code), _name(pred_code), pdf)
                # 同时取当前技术面指标，供回答列举涨跌原因
                da = analysis.daily_analysis(pred_code, _name(pred_code), pdf)
                pr["_signals"] = da.get("signals")
                pr["_indicators"] = da.get("indicators")
                pr["_stats"] = da.get("stats")
            except HTTPException:
                raise
            except RuntimeError as e:
                raise HTTPException(503, str(e))
            except Exception as e:  # noqa: BLE001
                raise HTTPException(500, f"预测分析失败: {e}")
            if ai_engine.status()["configured"]:
                text = ai_engine.ai_chat(ai_engine.prompt_predict(q, pr)) \
                    or ai_engine.local_predict_text(q, pr)
            else:
                text = ai_engine.local_predict_text(q, pr)
            return dict(text=text, engine=ai_engine.status()["engine"],
                        code=ds.norm_code(pred_code), name=_name(pred_code), type="predict")
        # 个股买卖点问题：点名了股票，或未点名但使用当前选中标的
        # 未点名个股且问的是全市场选股（"买什么股票好"）时让位给选股引擎
        trade_code = hit[0] if hit else code
        if (trade_code and not ghit and any(k in q for k in BUYSELL_KEYWORDS)
                and not (not hit and any(k in q for k in SCAN_KEYWORDS))):
            try:
                tdf = _df(trade_code)
                plan = trade.trade_plan(ds.norm_code(trade_code), _name(trade_code), tdf)
            except HTTPException:
                raise
            except Exception as e:  # noqa: BLE001
                raise HTTPException(500, f"买卖点分析失败: {e}")
            try:
                heat_ctx = heat.compute_heat()
            except Exception:  # noqa: BLE001
                heat_ctx = None
            if ai_engine.status()["configured"]:
                text = ai_engine.ai_chat(ai_engine.prompt_trade(q, plan, heat_ctx)) \
                    or ai_engine.local_trade_text(q, plan, heat_ctx)
            else:
                text = ai_engine.local_trade_text(q, plan, heat_ctx)
            return dict(text=text, engine=ai_engine.status()["engine"],
                        code=ds.norm_code(trade_code), name=_name(trade_code), type="trade")
        # 全球市场资产问题：美股/港股/全球指数/外汇/大宗商品/国内期货
        if ghit:
            gcat, gsym, gname = ghit
            try:
                ga = global_mkt.asset_analysis(gcat, gsym)
            except RuntimeError as e:
                raise HTTPException(503, str(e))
            except Exception as e:  # noqa: BLE001
                raise HTTPException(500, f"全球资产分析失败: {e}")
            pred = None
            if any(k in q for k in PREDICT_KEYWORDS):
                try:
                    rows = global_mkt.get_kline(gcat, gsym)
                    pred = prediction.predict(gsym, gname, analysis.build_df(rows))
                except Exception:  # noqa: BLE001
                    pred = None  # 无历史K线的资产（港股/指数）只给行情+技术面
            if ai_engine.status()["configured"]:
                text = ai_engine.ai_chat(ai_engine.prompt_global(q, ga, pred)) \
                    or ai_engine.local_global_text(q, ga, pred)
            else:
                text = ai_engine.local_global_text(q, ga, pred)
            return dict(text=text, engine=ai_engine.status()["engine"],
                        code=f"{gcat}:{gsym}", name=gname, type="global")
        # 全市场选股类问题（问题中未点名具体股票）：走扫描引擎，忽略前端传入的当前标的
        if not hit and any(k in q for k in SCAN_KEYWORDS):
            try:
                heat_ctx = heat.compute_heat()
            except Exception:  # noqa: BLE001
                heat_ctx = None
            # 按策略偏好分派到对应私募模型
            # 「短期最能赚钱/概率最高」类问题：走深度最佳推荐（动量扫描 + 相似形态预测 + 期望收益排序）
            if any(k in q for k in BESTPICK_KEYWORDS):
                capital = _parse_capital(q)  # 提取用户提问中的资金金额（如「一万块」）
                try:
                    bp = screener.best_short_term_pick(capital=capital)
                except RuntimeError as e:
                    raise HTTPException(503, str(e))
                if ai_engine.status()["configured"]:
                    text = ai_engine.ai_chat(ai_engine.prompt_bestpick(q, bp, heat_ctx)) \
                        or ai_engine.local_bestpick_text(q, bp, heat_ctx)
                else:
                    text = ai_engine.local_bestpick_text(q, bp, heat_ctx)
                return dict(text=text, engine=ai_engine.status()["engine"],
                            code="", name="", type="bestpick")
            is_mr = any(k in q for k in MR_KEYWORDS)
            is_tb = any(k in q for k in TB_KEYWORDS) and not is_mr
            try:
                if is_mr:
                    scan = strategies.mean_reversion_scan()
                    text = ai_engine.local_mr_text(q, scan, heat_ctx)
                    if ai_engine.status()["configured"]:
                        text = ai_engine.ai_chat(ai_engine.prompt_mr(q, scan, heat_ctx)) or text
                    stype = "scan_mr"
                elif is_tb:
                    scan = strategies.trend_breakout_scan()
                    text = ai_engine.local_tb_text(q, scan, heat_ctx)
                    if ai_engine.status()["configured"]:
                        text = ai_engine.ai_chat(ai_engine.prompt_tb(q, scan, heat_ctx)) or text
                    stype = "scan_tb"
                else:
                    scan = screener.scan_stocks()
                    text = ai_engine.local_scan_text(q, scan, heat_ctx)
                    if ai_engine.status()["configured"]:
                        text = ai_engine.ai_chat(ai_engine.prompt_scan(q, scan, heat_ctx)) or text
                    stype = "scan"
            except RuntimeError as e:
                raise HTTPException(503, str(e))
            return dict(text=text, engine=ai_engine.status()["engine"],
                        code="", name="", type=stype)
        # 第一阶段新能力：基本面/多因子/回测/资金流向（需点名个股）
        feature_code = hit[0] if hit else code
        if feature_code:
            # 基本面分析
            if any(k in q for k in FUND_KEYWORDS):
                fdata = fund.get_fundamental(feature_code)
                text = _local_fundamental_text(q, fdata)
                return dict(text=text, engine="内置量化规则引擎",
                            code=ds.norm_code(feature_code), name=_name(feature_code), type="fundamental")
            # 多因子分析
            if any(k in q for k in FACTOR_KEYWORDS):
                fdata = factors.get_factor_exposure(feature_code)
                text = _local_factors_text(q, fdata)
                return dict(text=text, engine="内置量化规则引擎",
                            code=ds.norm_code(feature_code), name=_name(feature_code), type="factors")
            # 策略回测
            if any(k in q for k in BT_KEYWORDS):
                bt_strategy = "tb" if any(k in q for k in TB_KEYWORDS) else ("mr" if any(k in q for k in MR_KEYWORDS) else "momentum")
                bt = backtest.run_backtest(bt_strategy, feature_code, 500)
                text = _local_backtest_text(q, bt)
                return dict(text=text, engine="内置量化规则引擎",
                            code=ds.norm_code(feature_code), name=_name(feature_code), type="backtest")
            # 资金流向
            if any(k in q for k in FLOW_KEYWORDS):
                cf = capital_flow.get_capital_flow(feature_code)
                text = _local_capital_flow_text(q, cf)
                return dict(text=text, engine="内置量化规则引擎",
                            code=ds.norm_code(feature_code), name=_name(feature_code), type="capital_flow")
        meta = dict(code=code, name=_name(code) if code else "")
        stock_ctx = None
        heat_ctx = None
        if code:
            try:
                df = _df(code)
                ctx = analysis.daily_analysis(ds.norm_code(code), _name(code), df)
                stock_ctx = dict(name=ctx["name"], code=ctx["code"], date=ctx["date"],
                                 indicators=ctx["indicators"], stats=ctx["stats"],
                                 support_resistance=ctx["support_resistance"], signals=ctx["signals"])
            except HTTPException:
                pass
        try:
            heat_ctx = heat.compute_heat()
        except Exception:  # noqa: BLE001
            pass
        if ai_engine.status()["configured"]:
            text = ai_engine.ai_chat(ai_engine.prompt_ask(req.question, stock_ctx, heat_ctx))
            if text:
                return dict(text=text, engine=ai_engine.status()["engine"], **meta)
        # 本地规则引擎兜底：基于上下文给出结构化回答
        q = req.question
        if heat_ctx and not stock_ctx and any(k in q for k in ("市场", "大盘", "热度", "情绪", "仓位")):
            ctx = heat.compute_heat()
            return dict(text=ai_engine.local_heat_text(ctx), engine="内置量化规则引擎", type="heat", **meta)
        if stock_ctx and code:
            df = analysis.build_df(ds.get_kline(code))
            ctx = analysis.daily_analysis(ds.norm_code(code), _name(code), df)
            base = ai_engine.local_daily_text(ctx)
            if any(k in q for k in ("短期", "涨", "跌", "明天", "买", "卖")):
                sc = analysis.short_term_analysis(ds.norm_code(code), _name(code), df)
                base += "\n\n---\n\n" + ai_engine.local_short_text(sc)
            return dict(text=base, engine="内置量化规则引擎", type="daily", **meta)
        if heat_ctx and any(k in q for k in ("市场", "大盘", "热度", "情绪", "仓位")):
            return dict(text=ai_engine.local_heat_text(heat_ctx), engine="内置量化规则引擎", type="heat", **meta)
        return dict(text="你可以这样提问：\n\n- **问具体股票**：`贵州茅台明天会涨吗`、`000001 短期走势怎么样`（自动识别股票并给出量化分析）\n- **问全市场选股**：`明天什么股票会上涨？上涨持续多久？`、`推荐几只强势股`（AI 扫描全市场后作答）\n- **问涨停连板**：`哪些股票要涨停了？能走几个板？什么时候断板？`（涨停梯队 + 封成比 + 历史晋级率推演）\n- **问市场热度**：`现在市场热度怎么样，该加仓还是减仓`\n- **问全球市场**：`特斯拉什么时候会上涨？`、`黄金还会跌吗`、`欧元兑日元走势`（美股/港股/指数/外汇/商品/期货全历史）\n\n提示：当前使用内置量化规则引擎回答。到「🤖 AI 分析师」页的多模型引擎面板，为智谱/DeepSeek/Kimi/Claude/GPT 等任意模型填入 API Key 即可启用大模型深度分析（多家配置可开启委员会协作模式）。",
                    engine="内置量化规则引擎", **meta)


@app.get("/api/ai/status")
def api_ai_status():
    return ai_engine.status()


class ProviderCfg(BaseModel):
    pid: str
    api_key: str = ""
    model: str = ""
    primary: bool = False


class AiConfigReq(BaseModel):
    providers: list = []
    collab: bool = None


@app.post("/api/ai/config")
def api_ai_config(req: AiConfigReq):
    """保存模型配置：providers=[{pid, api_key, model, primary}] + collab 协作开关"""
    cfg = ai_providers._load_cfg()
    for item in req.providers or []:
        pid = (item.get("pid") or "").strip()
        if pid not in ai_providers._PMAP:
            raise HTTPException(400, f"未知模型: {pid}")
        entry = cfg["providers"].setdefault(pid, {})
        if item.get("api_key"):
            entry["api_key"] = item["api_key"].strip()
        if item.get("model", ""):
            entry["model"] = item["model"].strip() or entry.get("model", "")
        if item.get("primary"):
            cfg["primary"] = pid
    if req.collab is not None:
        cfg["collab"] = bool(req.collab)
    # 未配置 key 的空条目不落盘
    cfg["providers"] = {k: v for k, v in cfg["providers"].items() if v.get("api_key")}
    ai_providers._save_cfg(cfg)
    ai_providers._health_cache.update(ts=0)
    return ai_engine.status()


@app.get("/api/ai/health")
def api_ai_health():
    """强制刷新各模型端点网络可达性"""
    return ai_providers.health(force=True)


# ---------------------------------------------------------------- 自主智能体

class AgentReq(BaseModel):
    question: str
    code: str = ""
    deep: bool = True


@app.post("/api/ai/agent")
def api_ai_agent(req: AgentReq):
    """自主智能体：多步工具调用 → 推理 → 自我复核 → 长期记忆"""
    q = (req.question or "").strip()
    if not q:
        raise HTTPException(400, "问题不能为空")
    return _json_stream(lambda: agent.run(q, (req.code or "").strip(), deep=bool(req.deep)))


@app.get("/api/agent/memory")
def api_agent_memory(limit: int = 30):
    return dict(items=agent.list_memory(limit=max(1, min(200, limit))))


@app.delete("/api/agent/memory")
def api_agent_memory_clear(kind: str = ""):
    return agent.clear_memory(kind or None)


@app.get("/api/agent/watch")
def api_agent_watch(limit: int = 20):
    return agent.watch_status(limit=max(1, min(100, limit)))


@app.post("/api/agent/watch/run")
def api_agent_watch_run():
    return agent._watch_once()


# ---------------------------------------------------------------- 全球市场

@app.get("/api/global/overview")
def api_global_overview():
    try:
        return global_mkt.overview()
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, f"全球行情获取失败: {e}")


class GlobalSearchReq(BaseModel):
    q: str = ""


@app.post("/api/global/search")
def api_global_search(req: GlobalSearchReq):
    return dict(results=global_mkt.search_global(req.q))


@app.get("/api/global/search")
def api_global_search_get(q: str = ""):
    return dict(results=global_mkt.search_global(q))


@app.get("/api/global/asset/{cat}/{sym}")
def api_global_asset(cat: str, sym: str):
    try:
        return global_mkt.asset_analysis(cat, sym)
    except RuntimeError as e:
        raise HTTPException(503, str(e))
    except Exception as e:  # noqa: BLE001
        raise HTTPException(500, f"全球资产分析失败: {e}")


@app.get("/api/global/asset/{cat}/{sym}/kline")
def api_global_kline(cat: str, sym: str, days: int = 250):
    try:
        rows = global_mkt.get_kline(cat, sym)
    except RuntimeError as e:
        raise HTTPException(503, str(e))
    except Exception as e:  # noqa: BLE001
        raise HTTPException(500, f"K线获取失败: {e}")
    return dict(kline=rows[-days:] if days else rows)


@app.get("/api/global/asset/{cat}/{sym}/predict")
def api_global_predict(cat: str, sym: str):
    try:
        rows = global_mkt.get_kline(cat, sym)
        df = analysis.build_df(rows)
        pr = prediction.predict(sym, global_mkt._asset_meta(cat, sym)["name"], df)
    except RuntimeError as e:
        raise HTTPException(503, str(e))
    except Exception as e:  # noqa: BLE001
        raise HTTPException(500, f"预测分析失败: {e}")
    return pr


# ---------------------------------------------------------------- 全球数据融合

@app.get("/api/world/coverage")
def api_world_coverage():
    """10 大资产类别覆盖度 + 数据源/历史起点/局限 + A股全市场统计"""
    try:
        return world_data.coverage()
    except Exception as e:  # noqa: BLE001
        raise HTTPException(500, f"覆盖度统计失败: {e}")


@app.get("/api/world/quotes")
def api_world_quotes(limit_per_class: int = 4):
    """各类别代表性实时行情"""
    try:
        rows = world_model.live_quotes(limit_per_class=max(1, min(limit_per_class, 20)))
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, f"全球行情获取失败: {e}")
    return dict(quotes=rows, n=len(rows))


@app.get("/api/world/search")
def api_world_search(q: str = "", limit: int = 20):
    try:
        return dict(results=world_data.search(q, limit=limit))
    except Exception as e:  # noqa: BLE001
        raise HTTPException(500, f"全球目录搜索失败: {e}")


@app.get("/api/world/dashboard")
def api_world_dashboard(days: int = 600):
    """6 个全球因子：收益/波动/相关矩阵/与A股同期及滞后相关"""
    try:
        return world_model.dashboard(days=max(120, min(days, 1500)))
    except Exception as e:  # noqa: BLE001
        raise HTTPException(500, f"全球因子看板失败: {e}")


@app.get("/api/world/leadlag")
def api_world_leadlag(days: int = 500, max_lag: int = 5):
    """全球因子与 A 股的领先滞后结构"""
    try:
        return world_model.leadlag(days=max(120, min(days, 1500)),
                                   max_lag=max(0, min(max_lag, 10)))
    except Exception as e:  # noqa: BLE001
        raise HTTPException(500, f"领先滞后分析失败: {e}")


@app.get("/api/world/beta")
def api_world_beta(days: int = 500, portfolio_value: float = 1e8):
    """A股组合对 6 个全球因子的 beta / t / R² / 对冲名义"""
    try:
        return world_model.world_beta(days=max(120, min(days, 1500)),
                                      portfolio_value=max(0.0, portfolio_value))
    except Exception as e:  # noqa: BLE001
        raise HTTPException(500, f"全球Beta回归失败: {e}")


@app.get("/api/world/regime")
def api_world_regime(days: int = 1000, horizon: int = 5):
    """全球股票×美元 四象限择时"""
    try:
        return world_model.regime(days=max(120, min(days, 2000)),
                                  horizon=max(1, min(horizon, 20)))
    except Exception as e:  # noqa: BLE001
        raise HTTPException(500, f"全球择时分析失败: {e}")


@app.get("/api/world/alpha")
def api_world_alpha(days: int = 600, horizon: int = 5):
    """原有 14 因子 vs 追加 3 个全球因子的样本外 IC 对比"""
    try:
        return world_model.world_alpha(days=max(200, min(days, 1200)),
                                       horizon=max(1, min(horizon, 20)))
    except Exception as e:  # noqa: BLE001
        raise HTTPException(500, f"全球因子接入测试失败: {e}")


@app.get("/api/world/matrix")
def api_world_matrix():
    """全球数据在模型中的接入点状态矩阵"""
    return world_model.integration_matrix()


@app.get("/api/world/kline/{cat}/{sym}")
def api_world_kline(cat: str, sym: str, days: int = 250):
    """任意全球标的 K 线（用于前端浏览）"""
    try:
        rows = world_data.kline(cat, sym, limit=max(30, min(days, 5000)))
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, f"K线获取失败: {e}")
    return dict(cat=cat, symbol=sym, kline=rows, n=len(rows))


@app.get("/")
def index():
    return FileResponse(os.path.join(STATIC_DIR, "index.html"))


# 静态资源（置于最后，避免遮蔽 API 路由）
app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
