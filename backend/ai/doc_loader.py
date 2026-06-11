"""
DocLoader: 负责在后端启动时一次性加载 data/ 目录下的话术文档到内存，
供 System Prompt 按场景注入使用（use_db_prompts 关闭时的 fallback 路径）。

A0-1：按场景精简 doc 组合，并对每份文档施加字符上限，避免全量 docx 进 system。
"""
from pathlib import Path
from core.logger import logger

# 文档存放路径（相对于 backend/ 目录）
DATA_DIR = Path(__file__).parent.parent / "data"

# 文档 key → 文件名
DOC_FILES = {
    "ai_guide":       "AI聊天助手指引.docx",
    "opening":        "一、开场破冰.docx",
    "strategy":       "2、各标签策略（含203040对应话术）.docx",
    "closing":        "五、促成成交.docx",
}

# 与 prompt_seed.DOC_CHAR_LIMITS 对齐
DOC_CHAR_LIMITS: dict[str, int] = {
    "ai_guide": 4000,
    "strategy": 6000,
    "opening": 3500,
    "closing": 4000,
}

# 场景 → 注入文档 key（按顺序）
SCENARIO_DOC_KEYS: dict[str, list[str]] = {
    "product_recommend": ["ai_guide", "strategy", "closing"],
    "general_chat": ["ai_guide", "strategy"],
    "staff_assistant": ["ai_guide", "strategy"],
}

DOC_TITLES: dict[str, str] = {
    "ai_guide": "销售角色与行为规范",
    "opening": "开场破冰话术参考",
    "strategy": "客户分层话术参考",
    "closing": "促成成交话术参考",
}

# 全局缓存
_doc_cache: dict[str, str] = {}


def _truncate_doc(text: str, doc_key: str) -> str:
    limit = DOC_CHAR_LIMITS.get(doc_key)
    if not text or not limit or limit <= 0:
        return text or ""
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "…（已截断）"


def _read_docx(path: Path) -> str:
    """读取 .docx 文件内容，返回纯文本字符串"""
    try:
        import docx  # python-docx
        doc = docx.Document(str(path))
        return "\n".join(p.text for p in doc.paragraphs if p.text.strip())
    except Exception as e:
        logger.error(f"DocLoader: 读取文档失败 {path.name}: {e}")
        return ""


def load_all_docs():
    """在应用启动时调用，将所有文档加载到内存缓存中"""
    global _doc_cache
    loaded = 0
    for key, filename in DOC_FILES.items():
        path = DATA_DIR / filename
        if path.exists():
            text = _read_docx(path)
            _doc_cache[key] = text
            loaded += 1
            logger.info(f"DocLoader: 已加载 [{filename}] ({len(text)} 字符)")
        else:
            logger.warning(f"DocLoader: 文档不存在，跳过: {path}")
    logger.info(f"DocLoader: 话术文档加载完毕，共 {loaded}/{len(DOC_FILES)} 个文档")


def get_doc(key: str) -> str:
    """获取指定 key 的文档内容（无需重新读取磁盘）"""
    return _doc_cache.get(key, "")


def get_docs_for_scenario(scenario: str) -> dict:
    """
    根据 AI 场景返回需注入的文档内容（已按 A0-1 预算截断）。

    场景 → 注入策略:
    - product_recommend: ai_guide + strategy + closing
    - general_chat:      ai_guide + strategy（不含 opening，避免 ongoing 对话灌入破冰全文）
    - staff_assistant:   ai_guide + strategy（内部问答）
    - 其它:              回退 general_chat 组合
    """
    keys = SCENARIO_DOC_KEYS.get(scenario) or SCENARIO_DOC_KEYS["general_chat"]
    result: dict[str, str] = {}
    for key in keys:
        raw = get_doc(key)
        if raw:
            result[key] = _truncate_doc(raw, key)
    return result


def format_doc_block(scenario: str) -> str:
    """把 get_docs_for_scenario 的结果格式化为 prompts.py 使用的 doc_block 文本。"""
    docs = get_docs_for_scenario(scenario)
    parts: list[str] = []
    keys = SCENARIO_DOC_KEYS.get(scenario) or SCENARIO_DOC_KEYS["general_chat"]
    for key in keys:
        text = docs.get(key)
        if not text:
            continue
        title = DOC_TITLES.get(key, key)
        parts.append(f"\n## {title}\n{text}\n")
    return "".join(parts)
