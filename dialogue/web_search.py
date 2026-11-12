"""联网搜索模块 —— 文本架构快脑的搜索增强（search-augmented）。

背景：端到端 SC2.0 的 web_search 是会话内字段，文本架构(老链路快脑走方舟对话补全)搬不过来。
这里改走火山「联网搜索 API」(融合信息搜索/Torchlight) 这个**独立 HTTP 服务**：闸门命中需要
实时事实的轮次时，先调它拿到结构化搜索结果摘要，再把摘要塞进快脑消息当参考，快脑照常流式
回答。好处：不换模型接口、不丢函数调用、不动 TTS 链路；key 复用端到端那把 VOLC_WEBSEARCH_API_KEY。

协议正本：资料文档/联网搜索.txt
- 端点(API Key 接入)：POST https://open.feedcoopapi.com/search_api/web_search
- 鉴权：Authorization: Bearer <VOLC_WEBSEARCH_API_KEY>
- 请求：{"Query": 1~100字, "SearchType": "web", "Count": N, "NeedSummary": true, "TimeRange": ...}
- 响应：Result.WebResults[] 每条含 Title/SiteName/Url/Summary(500~1000字, 推荐喂大模型)/PublishTime
- 错误：ResponseMetadata.Error.CodeN（10406 免费额度用尽 / 700429 QPS 限流 等）

设计原则：任何失败(缺 key/网络/限流/错误码)都返回空结果，让快脑无搜索照常回答，绝不让搜索拖垮发声。
"""
import os
import json
import urllib.request
import urllib.error

WEBSEARCH_URL = os.getenv(
    "VOLC_WEBSEARCH_HTTP_URL", "https://open.feedcoopapi.com/search_api/web_search"
)
# Query 上限 100 字符(过长会被服务端截断)，本地先截一刀避免无谓浪费
_QUERY_MAX = 100


def _get_api_key() -> str:
    return os.getenv("VOLC_WEBSEARCH_API_KEY") or os.getenv("REALTIME_WEBSEARCH_API_KEY") or ""


def search_web(query: str, *, count: int = 5, time_range: str = None,
               timeout: float = 6.0, log_fn=None) -> list[dict]:
    """调联网搜索拿结构化结果。失败一律返回 []（不抛异常）。

    返回每条：{title, site, url, summary, publish_time}
    time_range: None / OneDay / OneWeek / OneMonth / OneYear / "YYYY-MM-DD..YYYY-MM-DD"
    """
    def _log(msg):
        if log_fn:
            log_fn(msg)

    query = (query or "").strip()
    if not query:
        return []
    if len(query) > _QUERY_MAX:
        query = query[:_QUERY_MAX]

    api_key = _get_api_key()
    if not api_key:
        _log("[web_search] 跳过：缺少 VOLC_WEBSEARCH_API_KEY")
        return []

    body = {
        "Query": query,
        "SearchType": "web",
        "Count": max(1, min(count, 50)),
        "NeedSummary": True,
    }
    if time_range:
        body["TimeRange"] = time_range

    req = urllib.request.Request(
        WEBSEARCH_URL,
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        _log(f"[web_search] HTTP {e.code}：{e.reason}")
        return []
    except Exception as e:
        _log(f"[web_search] 请求失败：{type(e).__name__}: {e}")
        return []

    return _parse_results(raw, log_fn=log_fn)


def _parse_results(raw: str, *, log_fn=None) -> list[dict]:
    def _log(msg):
        if log_fn:
            log_fn(msg)
    try:
        data = json.loads(raw)
    except Exception:
        _log("[web_search] 响应非 JSON")
        return []
    # 错误码在 ResponseMetadata.Error.CodeN
    meta = data.get("ResponseMetadata") or {}
    err = meta.get("Error")
    if err:
        _log(f"[web_search] 服务端错误 {err.get('CodeN')}: {err.get('Message')}")
        return []
    result = data.get("Result") or {}
    items = result.get("WebResults") or []
    out = []
    for it in items:
        summary = (it.get("Summary") or it.get("Snippet") or "").strip()
        if not summary:
            continue
        out.append({
            "title": (it.get("Title") or "").strip(),
            "site": (it.get("SiteName") or "").strip(),
            "url": (it.get("Url") or "").strip(),
            "summary": summary,
            "publish_time": (it.get("PublishTime") or "").strip(),
        })
    return out


def is_enabled() -> bool:
    """总开关：默认关闭，验证可用后由 .env 的 FAST_BRAIN_WEBSEARCH_ENABLED=1 打开。"""
    return os.getenv("FAST_BRAIN_WEBSEARCH_ENABLED", "0").strip().lower() in {"1", "true", "yes", "on"}


# 闸门：只在"明确要求查"或"涉及实时事实"的轮次才搜，避免每轮都搜(费钱/加延迟/把闲聊也拿去搜)。
# 保守取词：光有"今天/最近"这种时间词不触发(太吵)；要么带搜索动词，要么带实时事实名词。
_SEARCH_VERBS = ("查一下", "查查", "搜一下", "搜搜", "搜索", "查询", "帮我查", "帮我搜", "搜个", "查个")
# 注意：时间/日期/星期(几号/星期几/现在几点)不列在这里——本机时钟已注入提示词，
# 模型直接答即可，搜网页拿到的是历史快照反而错(2026-06-14 实测踩坑)。
_REALTIME_NOUNS = (
    "天气", "气温", "新闻", "热搜", "热点", "股价", "股票", "汇率", "油价", "金价",
    "票房", "比分", "赛果", "比赛结果", "发布会", "上市", "发售", "发布了", "最新消息",
    "排行榜", "航班", "车次", "票价",
)


def needs_search(text: str) -> bool:
    if not text:
        return False
    if any(v in text for v in _SEARCH_VERBS):
        return True
    if any(n in text for n in _REALTIME_NOUNS):
        return True
    return False


def build_search_context(results: list[dict], *, max_items: int = 3,
                         per_item_chars: int = 500) -> str:
    """把搜索结果拼成给快脑当参考的文本块（结果导向措辞，避免被当正文念）。"""
    if not results:
        return ""
    lines = []
    for i, r in enumerate(results[:max_items], 1):
        s = r["summary"][:per_item_chars]
        when = f"（{r['publish_time'][:10]}）" if r.get("publish_time") else ""
        src = f"［{r['site']}］" if r.get("site") else ""
        lines.append(f"{i}. {src}{when}{s}")
    body = "\n".join(lines)
    return (
        "【联网搜索结果（仅供你参考，用自己的话总结成口语，别照念、别念网址；"
        "涉及时效就说个大概时间；搜的内容对不上就说没查到准的，别硬编）】\n" + body
    )
