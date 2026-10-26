# -*- coding: utf-8 -*-
"""多模型引擎：全球主流大模型注册表 + 统一协议适配 + 编排协调器

设计目标（防错乱 / 防冲突）：
  1) 统一适配层——OpenAI 兼容与 Anthropic 协议归一为 chat(pid, prompt, system)
  2) 分工制——不同任务路由给不同专家模型，各司其职互不干扰
  3) 委员会制——重要问题多模型并行独立作答，由主席模型汇总为
     『共识结论 + 依据 + 分歧点』结构，绝不拼接互相矛盾的原文
  4) 输出规范化——剥离模型自我介绍/越权内容，统一 Markdown 与免责声明
  5) 静默降级——主模型失败→次模型→内置规则引擎，用户无感

配置（server/ai_config.json，任配多家即可同时上线）：
  {
    "primary": "zhipu",
    "collab": true,
    "providers": {
      "zhipu":    {"api_key": "…", "model": "glm-4-flash"},
      "deepseek": {"api_key": "…"}
    }
  }
兼容旧格式：{"api_key": "…", "base_url": "…", "model": "…"}
"""
import json
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FTTimeoutError

import requests

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CFG_PATH = os.path.join(BASE_DIR, "ai_config.json")

# ---------------------------------------------------------------- 模型注册表
# sandbox: 该端点在本沙箱网络实测可达性（True 可达 / False 被出口代理屏蔽）
# 用户本地部署本平台时不受此限制，配置后均可使用
PROVIDERS = [
    # ---- 中国（OpenAI 兼容） ----
    dict(id="zhipu", name="智谱 GLM", org="智谱AI", proto="openai",
         base_url="https://open.bigmodel.cn/api/paas/v4", model="glm-4-flash",
         sandbox=True, skill="通用问答 · 免费额度 · 中文优化"),
    dict(id="deepseek", name="DeepSeek", org="深度求索", proto="openai",
         base_url="https://api.deepseek.com", model="deepseek-chat",
         sandbox=True, skill="深度推理 · 金融量化强项"),
    dict(id="qwen", name="通义千问 Qwen", org="阿里巴巴", proto="openai",
         base_url="https://dashscope.aliyuncs.com/compatible-mode/v1", model="qwen-plus",
         sandbox=True, skill="长文本 · 多语言"),
    dict(id="doubao", name="豆包", org="字节跳动", proto="openai",
         base_url="https://ark.cn-beijing.volces.com/api/v3", model="doubao-pro-32k",
         sandbox=True, skill="中文理解 · 响应快"),
    dict(id="kimi", name="Kimi", org="月之暗面", proto="openai",
         base_url="https://api.moonshot.cn/v1", model="moonshot-v1-8k",
         sandbox=True, skill="超长上下文"),
    dict(id="minimax", name="MiniMax", org="MiniMax", proto="openai",
         base_url="https://api.minimax.chat/v1", model="abab6.5s-chat",
         sandbox=True, skill="多模态 · 语音"),
    dict(id="qianfan", name="文心千帆", org="百度", proto="openai",
         base_url="https://aip.baidubce.com/v2", model="ernie-4.0-8k",
         sandbox=True, skill="中文知识图谱"),
    dict(id="hunyuan", name="混元", org="腾讯", proto="openai",
         base_url="https://api.hunyuan.cloud.tencent.com/v1", model="hunyuan-turbo",
         sandbox=True, skill="生态整合"),
    dict(id="xfyun", name="星火", org="科大讯飞", proto="openai",
         base_url="https://spark-api-open.xf-yun.com/v1", model="generalv3.5",
         sandbox=True, skill="语音交互"),
    dict(id="stepfun", name="阶跃星辰", org="阶跃AI", proto="openai",
         base_url="https://api.stepfun.com/v1", model="step-1-8k",
         sandbox=True, skill="多模态推理"),
    dict(id="baichuan", name="百川", org="百川智能", proto="openai",
         base_url="https://api.baichuan-ai.com/v1", model="Baichuan4",
         sandbox=True, skill="医疗/金融语料"),
    dict(id="yi", name="零一万物", org="零一万物", proto="openai",
         base_url="https://api.lingyiwanwu.com/v1", model="yi-large",
         sandbox=True, skill="双语优化"),
    dict(id="siliconflow", name="硅基流动", org="SiliconFlow", proto="openai",
         base_url="https://api.siliconflow.cn/v1", model="deepseek-ai/DeepSeek-V3",
         sandbox=True, skill="开源模型聚合(DeepSeek/Qwen/Llama)"),
    # ---- 国际 ----
    dict(id="openai", name="GPT", org="OpenAI", proto="openai",
         base_url="https://api.openai.com/v1", model="gpt-4o-mini",
         sandbox=False, skill="综合能力标杆"),
    dict(id="anthropic", name="Claude", org="Anthropic", proto="anthropic",
         base_url="https://api.anthropic.com", model="claude-sonnet-4-5",
         sandbox=True, skill="长文分析 · 严谨推理"),
    dict(id="gemini", name="Gemini", org="Google", proto="gemini",
         base_url="https://generativelanguage.googleapis.com/v1beta",
         model="gemini-2.0-flash", sandbox=False, skill="多模态 · 免费额度"),
    dict(id="groq", name="Groq", org="Groq", proto="openai",
         base_url="https://api.groq.com/openai/v1", model="llama-3.3-70b-versatile",
         sandbox=True, skill="极速推理(LPU)"),
    dict(id="openrouter", name="OpenRouter", org="OpenRouter", proto="openai",
         base_url="https://openrouter.ai/api/v1", model="openai/gpt-4o-mini",
         sandbox=True, skill="数百模型单一入口"),
    dict(id="together", name="Together", org="Together AI", proto="openai",
         base_url="https://api.together.xyz/v1", model="meta-llama/Llama-3.3-70B-Instruct-Turbo",
         sandbox=True, skill="开源模型托管"),
    dict(id="mistral", name="Mistral", org="Mistral AI", proto="openai",
         base_url="https://api.mistral.ai/v1", model="mistral-large-latest",
         sandbox=False, skill="欧洲 · 擅长代码"),
    dict(id="xai", name="Grok", org="xAI", proto="openai",
         base_url="https://api.x.ai/v1", model="grok-3-mini",
         sandbox=False, skill="实时信息"),
]
_PMAP = {p["id"]: p for p in PROVIDERS}

_cfg_lock = threading.Lock()

ARBITER_SYSTEM = (
    "你是量化分析师委员会的主席。多位AI分析师独立回答了同一个问题，"
    "请把他们的观点合并为一份严谨报告，格式：\n"
    "### 🧠 多模型共识\n（各模型一致的结论，含关键数字）\n"
    "### 📊 核心依据\n（合并去重后的主要论据，Markdown 分点）\n"
    "### ⚠️ 分歧点\n（仅当各模型结论不一致时列出：分歧内容+各自理由；完全一致则写『无』）\n"
    "### 💡 综合建议\n（可操作的参考区间：支撑/压力/止损）\n\n"
    "严格要求：不得添加数据之外的信息；保留数字精度；最后加一行『以上为多模型协作推演，不构成投资建议』。"
)

_DISCLAIMER = "以上为量化模型推演，不构成投资建议"


# ---------------------------------------------------------------- 配置

def _load_cfg():
    cfg = dict(primary="", collab=True, providers={})
    if os.path.exists(CFG_PATH):
        try:
            with open(CFG_PATH, encoding="utf-8") as f:
                raw = json.load(f)
            if isinstance(raw.get("providers"), dict):
                cfg["providers"] = {k: v for k, v in raw["providers"].items() if isinstance(v, dict)}
            cfg["primary"] = raw.get("primary") or ""
            cfg["collab"] = bool(raw.get("collab", True))
            # 兼容旧单模型格式 → 映射为 default 供应商
            if raw.get("api_key"):
                cfg["providers"]["zhipu"] = dict(
                    api_key=raw["api_key"],
                    model=raw.get("model") or _PMAP["zhipu"]["model"],
                    base_url=raw.get("base_url") or _PMAP["zhipu"]["base_url"])
                cfg["primary"] = cfg["primary"] or "zhipu"
        except Exception:  # noqa: BLE001
            pass
    env_key = os.environ.get("AI_API_KEY", "")
    if env_key:
        cfg["providers"].setdefault("zhipu", dict(api_key=env_key))
        cfg["primary"] = cfg["primary"] or "zhipu"
    return cfg


def _save_cfg(cfg):
    with _cfg_lock:
        with open(CFG_PATH, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)


def _pcfg(pid):
    """该供应商的合并配置（注册表默认 + 用户覆盖）"""
    p = dict(_PMAP[pid])
    user = _load_cfg()["providers"].get(pid) or {}
    p["api_key"] = user.get("api_key", "")
    if user.get("model"):
        p["model"] = user["model"]
    if user.get("base_url"):
        p["base_url"] = user["base_url"]
    return p


# ---------------------------------------------------------------- 统一调用适配

def chat_any(pid, prompt, system, timeout=55):
    """调用指定模型；返回文本或 None（失败/未配置）。协议差异在适配层抹平。"""
    c = _pcfg(pid)
    if not c["api_key"]:
        return None
    try:
        if c["proto"] == "anthropic":
            r = requests.post(
                c["base_url"].rstrip("/") + "/v1/messages",
                headers={"x-api-key": c["api_key"], "anthropic-version": "2023-06-01",
                         "Content-Type": "application/json"},
                json=dict(model=c["model"], max_tokens=1600, system=system,
                          messages=[dict(role="user", content=prompt)]),
                timeout=timeout)
            data = r.json()
            return (data.get("content") or [{}])[0].get("text")
        # openai 兼容（含 gemini 的兼容模式可后续扩展）
        r = requests.post(
            c["base_url"].rstrip("/") + "/chat/completions",
            headers={"Authorization": "Bearer " + c["api_key"], "Content-Type": "application/json"},
            json=dict(model=c["model"], messages=[
                dict(role="system", content=system), dict(role="user", content=prompt)],
                temperature=0.4, max_tokens=1600),
            timeout=timeout)
        data = r.json()
        return data["choices"][0]["message"]["content"]
    except Exception as e:  # noqa: BLE001
        print(f"[ai_providers] {pid} call failed:", str(e)[:160])
        return None


# ---------------------------------------------------------------- 输出规范化（防错乱）

_BANNED_PREFIX = ("作为一个ai", "作为一个语言模型", "我是{自}", "很高兴为您", "好的，", "当然，")


def normalize(text, primary_pid=""):
    """统一各模型输出格式：剥离自我介绍/开场白，补免责声明，裁剪超长空白。"""
    if not text:
        return text
    name = _PMAP.get(primary_pid, {}).get("name", "")
    lines = text.strip().splitlines()
    # 剥离首行的自我介绍/客套（防模型人格"串台"错乱）
    while lines:
        head = lines[0].strip().lower()
        if head.startswith(("作为一个", "作为一个ai", "作为一个语言模型", "我是", "好的", "当然", "很高兴")) \
                or (name and name.lower() in head and len(head) < 60):
            lines.pop(0)
        else:
            break
    text = "\n".join(lines).strip()
    if _DISCLAIMER not in text.replace(" ", ""):
        text += f"\n\n*{_DISCLAIMER}。*"
    return text


# ---------------------------------------------------------------- 编排协调器

def online_ids():
    """已配置 key 的供应商（按注册表顺序）"""
    provs = _load_cfg()["providers"]
    return [p["id"] for p in PROVIDERS if provs.get(p["id"], {}).get("api_key")]


def primary_id():
    cfg = _load_cfg()
    ids = online_ids()
    if cfg["primary"] in ids:
        return cfg["primary"]
    return ids[0] if ids else ""


def orchestrate(prompt, system, timeout=70):
    """多模型编排入口（防错乱核心）：
    - 0 家在线 → None（调用方降级内置规则引擎）
    - 1 家在线 / 关闭协作 → 单模型直答
    - 多家在线且协作开 → 委员会模式：并行独立作答 → 主席仲裁合并 → 规范化
    """
    ids = online_ids()
    if not ids:
        return None
    pid = primary_id()
    if len(ids) == 1 or not _load_cfg()["collab"]:
        return normalize(chat_any(pid, prompt, system, timeout), pid)
    # ---- 委员会模式：最多 3 家并行（主模型 + 其他前2家）----
    panel = ([pid] + [x for x in ids if x != pid])[:3]
    answers = {}

    def _ask(p_):
        t0 = time.time()
        txt = chat_any(p_, prompt, system, timeout=timeout)
        answers[p_] = (txt, round(time.time() - t0, 1))

    with ThreadPoolExecutor(max_workers=3) as ex:
        futs = [ex.submit(_ask, p_) for p_ in panel]
        for f in futs:
            try:
                f.result(timeout=timeout + 5)
            except FTTimeoutError:
                pass
    ok = [(p_, t) for p_, t in answers.items() if t]
    if not ok:
        return None
    if len(ok) == 1:
        return normalize(ok[0][1], ok[0][0])
    # ---- 主席仲裁：主模型汇总，绝不拼接原文（防互相矛盾/攻击）----
    quotes = "\n\n".join(
        f"【分析师{i + 1}】\n{(t or '')[:900]}" for i, (p_, t) in enumerate(ok))
    arb_prompt = (f"用户问题（已附量化数据上下文）：\n{prompt[:4000]}\n\n"
                  f"各分析师独立回答如下：\n{quotes}")
    merged = chat_any(pid, arb_prompt, ARBITER_SYSTEM, timeout=timeout)
    final = merged or ok[0][1]  # 主席失败→直接采用主模型答案，不做拼接
    tag = "🧠 委员会：" + "、".join(_PMAP[p_]["name"] for p_, _ in ok)
    return normalize(final, pid) + f"\n\n<em style=\"color:#5a6b8c\">— {tag}</em>"


# ---------------------------------------------------------------- 健康检查

_health_cache = {"ts": 0, "data": {}}


def health(force=False):
    """各端点网络可达性（1小时内缓存；401/403=在线待配key，SSL错=被网络屏蔽）"""
    if not force and time.time() - _health_cache["ts"] < 3600:
        return _health_cache["data"]

    def _ping(p):
        try:
            if p["proto"] == "anthropic":
                r = requests.post(p["base_url"].rstrip("/") + "/v1/messages",
                                  headers={"x-api-key": "probe", "anthropic-version": "2023-06-01"},
                                  json=dict(model=p["model"], max_tokens=1,
                                            messages=[dict(role="user", content="hi")]), timeout=2)
            else:
                r = requests.post(p["base_url"].rstrip("/") + "/chat/completions",
                                  headers={"Authorization": "Bearer probe"},
                                  json=dict(model=p["model"], max_tokens=1,
                                            messages=[dict(role="user", content="hi")]), timeout=2)
            return "online" if r.status_code in (400, 401, 403, 422) else f"http{r.status_code}"
        except Exception:
            return "blocked"

    # 全部端点并发探测，整体耗时≈单次超时，避免串行轮次把响应拖到 5 秒以上
    with ThreadPoolExecutor(max_workers=max(1, len(PROVIDERS))) as ex:
        res = dict(zip([p["id"] for p in PROVIDERS], ex.map(_ping, PROVIDERS)))
    _health_cache.update(ts=time.time(), data=res)
    return res


def warm_health():
    """后台预热健康检查缓存（各 worker 启动时调用，首次访问不再等待）"""
    if time.time() - _health_cache["ts"] < 3600:
        return
    threading.Thread(target=health, daemon=True).start()


def status():
    """完整状态（供 /api/ai/status 与前端模型面板）"""
    cfg = _load_cfg()
    h = health()
    ids = online_ids()
    models = []
    for p in PROVIDERS:
        prov = cfg["providers"].get(p["id"]) or {}
        models.append(dict(
            id=p["id"], name=p["name"], org=p["org"], skill=p["skill"],
            model=prov.get("model") or p["model"],
            configured=bool(prov.get("api_key")),
            network=h.get(p["id"], "unknown"),
            primary=(p["id"] == primary_id() if ids else False)))
    return dict(
        engine=("多模型协作" if len(ids) > 1 and cfg["collab"] else
                "大模型" if ids else "内置量化规则引擎"),
        configured=bool(ids), collab=cfg["collab"], n_online=len(ids),
        primary=primary_id() or None, models=models)
