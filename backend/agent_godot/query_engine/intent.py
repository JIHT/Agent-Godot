"""query_engine/intent.py —— 五分类器（M12 §1.1 / §4 步骤 1）

分诊护士的三秒判断：治病（code_edit）/咨询（knowledge）/打招呼
（chitchat）/要最新信息（search）/话说一半（ambiguous）。

三层结构（成本从零到低）：
1. 规则 fast-path：明显闲聊直判（0ms/零成本，摊薄整体延迟）；
2. 结果缓存：同句重复输入直接吃缓存（dict，有界防泄漏）；
3. LLM few-shot 主分类器：意图集是活的（M16 加语音），换意图只改提示词。

小模型场景：本分类器用 registry.llm_for_task("intent") 装配——
models.yaml 未配 intent 键时回落 ask 主 LLM，配置了就走专属小模型。
"""
from __future__ import annotations

import hashlib
import logging
import re
from enum import Enum

from ..core import LLMRequest, LLMResponse, Message

logger = logging.getLogger(__name__)

INTENT_PROMPT = """判断用户输入的意图，只输出标签（不要输出其他任何内容）：
code_edit=修改/创建项目代码 | knowledge=询问 Godot/引擎/项目知识 | chitchat=闲聊
| search=需要联网的时效性查询 | ambiguous=依赖上下文才能理解

判据：只要不动项目文件就优先 knowledge。示例：
"给敌人加AI巡逻" → code_edit
"信号和回调的区别" → knowledge
"帮我看看 latest stable 版本是" → search
"好的谢谢" → chitchat
"那第二个呢" → ambiguous

输入：{input}
标签："""

# 规则 fast-path：明显闲聊直判（"谢谢/你好"不值得花 300ms 分类）
_CHITCHAT_RE = re.compile(
    r"^(谢谢|多谢|辛苦了?|你好|您好|嗨|哈喽|hello|hi|ok|okay|好的|嗯+|"
    r"再见|拜拜|晚安)[!！。.~～\s]*$", re.IGNORECASE)


class Intent(Enum):
    CODE_EDIT = "code_edit"       # → craft 模式（M13），不预取知识
    KNOWLEDGE = "knowledge"       # → RAG/图谱通道，只读问答
    CHITCHAT = "chitchat"         # → 直答，零检索（省钱省延迟）
    SEARCH = "search"             # → 联网通道（时效性）
    AMBIGUOUS = "ambiguous"       # → 上下文消解后二次分类
    UNKNOWN = "unknown"           # → 非法输出出口，路由保守默认（knowledge）


_VALID_LABELS = {i.value for i in Intent}


class IntentClassifier:
    """few-shot 分类器：规则 fast-path → 缓存 → 小模型，全部 fail-soft。"""

    def __init__(self, llm, model: str, temperature: float = 0.0,
                 max_tokens: int = 16, cache_limit: int = 1024):
        self.llm = llm                     # registry.llm_for_task("intent")
        self.model = model
        self.temperature = temperature     # 分类要稳：0
        self.max_tokens = max_tokens
        self.cache_limit = cache_limit
        self._cache: dict[str, Intent] = {}

    async def classify(self, input: str, history: list[Message] | None = None
                       ) -> Intent:
        text = (input or "").strip()
        if not text:
            return Intent.UNKNOWN
        # ① 规则 fast-path：明显闲聊零成本直判
        if _CHITCHAT_RE.match(text):
            return Intent.CHITCHAT
        # ② 缓存：同句重复输入直接命中
        key = hashlib.md5(text.encode()).hexdigest()
        if key in self._cache:
            return self._cache[key]
        # ③ 小模型 few-shot
        try:
            resp: LLMResponse = await self.llm.complete(LLMRequest(
                model=self.model,
                messages=[Message(role="user",
                                  content=INTENT_PROMPT.format(input=text))],
                temperature=self.temperature,
                max_tokens=self.max_tokens,
                stream=False))
            label = (resp.content or "").strip().strip("`\"'。. \n").lower()
            intent = Intent(label) if label in _VALID_LABELS else Intent.UNKNOWN
        except Exception as e:                    # noqa: BLE001 —— 分类挂了走保守
            logger.warning("意图分类失败（%s），按 unknown 保守处理", e)
            intent = Intent.UNKNOWN
        self._remember(key, intent)
        return intent

    def _remember(self, key: str, intent: Intent) -> None:
        """缓存写入（FIFO 淘汰防无界增长）。"""
        if len(self._cache) >= self.cache_limit and key not in self._cache:
            self._cache.pop(next(iter(self._cache)))
        self._cache[key] = intent


__all__ = ["INTENT_PROMPT", "Intent", "IntentClassifier"]
