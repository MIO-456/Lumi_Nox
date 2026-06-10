import json
import re


def build_memory_context(storage, identity_key, agent_name, current_input, include_agent=False):
    """组装注入快脑的记忆上下文。

    include_agent=False（默认）时**不注入 Agent 自我记忆**（agent_facts / agent_summaries）——
    这些"自我记忆"由会话结束的 finalize 提取，实测会把临时话题（如某场的烧烤/五花肉）当"事实"
    抽出来，再每轮注入，导致跨场话题污染、主播咬着旧梗不放。观众记忆（viewer_*）一律保留。
    """
    sections = []

    if include_agent:
        agent_facts = storage.list_active_agent_facts(agent_name=agent_name, limit_per_category=2)
        if agent_facts:
            lines = [f"- {item['fact_value']}" for item in agent_facts]
            sections.append(
                "## 你的近期自我记忆（你自己的设定、喜好、做过的约定；可以自然体现，"
                "但里面提到的观众现在不一定在直播间，**不要主动喊他们名字、不要当成他们正在跟你说话**）\n"
                + "\n".join(lines)
            )

    viewer_facts = storage.list_active_viewer_facts(identity_key=identity_key, limit=6)
    if viewer_facts:
        lines = [f"- {item['fact_value']}" for item in viewer_facts]
        sections.append("## 这个观众的已知信息\n" + "\n".join(lines))

    if include_agent:
        agent_summaries = storage.list_recent_agent_summaries(agent_name=agent_name, session_limit=5)
        matched_agent = _filter_relevant_summaries(agent_summaries, current_input)[:2]
        if matched_agent:
            lines = [f"- {item['summary_text']}" for item in matched_agent]
            sections.append(
                "## 你过去几场直播的回顾（这是已经发生过的事，不是现在正在进行的；"
                "里面点到的观众名字现在大概率不在场，**只能当背景，不要主动点名、不要接着上一场的话头喊他们**，"
                "除非这一刻真的有他的弹幕在你输入里）\n"
                + "\n".join(lines)
            )

    viewer_summaries = storage.list_viewer_summaries(identity_key=identity_key)
    matched_viewer = _filter_relevant_summaries(viewer_summaries, current_input)[:3]
    if matched_viewer:
        lines = [f"- {item['summary_text']}" for item in matched_viewer]
        sections.append("## 你和这个观众之前聊过的相关内容\n" + "\n".join(lines))

    if not viewer_facts and not matched_viewer:
        raw_messages = storage.list_recent_viewer_messages(identity_key=identity_key, limit=3)
        if raw_messages:
            lines = [f"- {item['message_text']}" for item in raw_messages]
            sections.append("## 这个观众最近原始发言摘录\n" + "\n".join(lines))

    return "\n\n".join(sections)


def _filter_relevant_summaries(rows, current_input):
    if not rows:
        return []
    current_input = current_input or ""
    terms = _extract_terms(current_input)

    matched = []
    for row in rows:
        summary_text = row["summary_text"]
        keywords = _load_keywords(row.get("keywords_json"))
        haystacks = [summary_text, *keywords]
        keyword_hit = any(keyword and keyword in current_input for keyword in keywords)
        term_hit = any(term and any(term in hay for hay in haystacks) for term in terms)
        # 必须有 current_input 且命中关键词/词块才算相关。空输入（如开播首轮 proactive 没有
        # 上下文）→ 不注入历史摘要，避免无端把过去几场全端上来（顺手减少"喊不在场观众"）。
        if current_input and (keyword_hit or term_hit):
            matched.append(row)
    return matched


def _load_keywords(keywords_json):
    if not keywords_json:
        return []
    try:
        data = json.loads(keywords_json)
    except json.JSONDecodeError:
        return []
    return [str(item) for item in data]


def _extract_terms(text):
    parts = re.findall(r"[A-Za-z0-9_]+|[\u4e00-\u9fff]{1,8}", text or "")
    seen = []
    for part in parts:
        if part not in seen:
            seen.append(part)
    return seen
