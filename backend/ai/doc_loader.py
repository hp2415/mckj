"""
DocLoader: 负责在后端启动时一次性加载 data/ 目录下的话术文档到内存，
供 System Prompt 按场景注入使用。
"""
import os
from pathlib import Path
from core.logger import logger

# 文档存放路径（相对于 backend/ 目录）
DATA_DIR = Path(__file__).parent.parent / "data"

# 场景对应的文档列表（按注入顺序排列）
# 格式: { "doc_key": filename }
DOC_FILES = {
    "ai_guide":       "AI聊天助手指引.docx",
    "opening":        "一、开场破冰.docx",
    "strategy":       "2、各标签策略（含203040对应话术）.docx",
    "closing":        "五、促成成交.docx",
}

# 全局缓存
_doc_cache: dict[str, str] = {}


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
    根据当前 AI 场景，返回需要注入的文档内容字典。
    
    场景 → 注入文档策略:
    - general_chat  (自由对话):   通用AI指引 + 开场破冰话术 + 标签策略
    - product_recommend (推品报价): 通用AI指引 + 标签策略 + 促成成交话术
    """
    result = {}
    # 所有场景都注入：通用 AI 指引
    result["ai_guide"] = get_doc("ai_guide")
    
    if scenario == "product_recommend":
        result["strategy"] = get_doc("strategy")
        result["closing"]  = get_doc("closing")
    else:  # general_chat 及其他
        result["opening"]  = get_doc("opening")
        result["strategy"] = get_doc("strategy")
    
    return result
