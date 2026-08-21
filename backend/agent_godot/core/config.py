"""core/config.py —— 配置注册中心（models.yaml 的运行时镜像）

大白话：总机 + 通讯录 + 路由表的合体。models.yaml 是唯一的"供应商花名册"：
- providers 节：每家厂商一个条目（DeepSeek/Qwen/GLM/Kimi/OpenAI/Gemini/
  Anthropic/LM Studio/Ollama/vLLM…）
- routing 节：ask/craft/plan/multi 四模式各用哪个模型（含采样参数）

密钥纪律：yaml 只写 api_key_env（环境变量名），密钥本体在 .env / 系统环境变量里
——密钥永不进 git（.env 已在 .gitignore）。

LM Studio 特性：model 填 "auto" 时首次使用自动查 /v1/models 取已加载模型，
彻底解决"模型名不匹配"的坑（你 8 月 18 日踩过的）。
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path

import httpx
import yaml

from .llm import ModelConfig, get_llm
from .semantic_cache import OllamaEmbedder, SemanticCache

logger = logging.getLogger(__name__)

DEFAULT_CONFIG_PATHS = (
    Path("config/models.yaml"),
    Path("../config/models.yaml"),
    Path("../../config/models.yaml"),
)


@dataclass
class RouteRule:
    """routing 表的一项：模式 → 模型引用 + 采样参数覆盖。"""
    ref: str                      # 如 "lmstudio/auto" / "deepseek/deepseek-chat"
    temperature: float | None = None
    top_p: float | None = None
    max_tokens: int | None = None


class ModelRegistry:
    """models.yaml 的运行时镜像。一个进程一个实例即可（load_registry 工厂）。"""

    def __init__(self, data: dict, source: Path):
        self._source = source
        self._providers: dict[str, dict] = data.get("providers") or {}
        self._routing: dict[str, dict] = data.get("routing") or {}
        self._configs: dict[str, ModelConfig] = {}   # ref → 已解析配置（缓存）
        self._cache = self._build_cache(data)

        if not self._providers:
            raise ValueError(f"{source} 里没有任何 providers 配置")

    @staticmethod
    def _build_cache(data: dict) -> SemanticCache:
        """embedding 节驱动装配：enabled → 真嵌入（ollama bge-m3），
        否则 → 假嵌入（零依赖，CI/单测环境友好）。

        注意 fail-soft 语义：enabled=true 但 ollama 没启动时构造照样成功
        （OllamaEmbedder 是懒连接），只在每次编码时返回 None 让缓存让路。
        """
        emb = data.get("embedding") or {}
        if not emb.get("enabled", False):
            return SemanticCache()
        return SemanticCache(
            embedder=OllamaEmbedder(
                base_url=emb.get("base_url", "http://127.0.0.1:11434/v1"),
                model=emb.get("model", "bge-m3")),
            threshold=float(emb.get("cache_threshold", 0.92)),
            ttl=float(emb.get("cache_ttl", 86400.0)))

    # ---------- 加载与解析 ----------

    def _resolve_key(self, entry: dict, name: str) -> str:
        """密钥解析：api_key_env 优先 → api_key 明文 → 空（本地服务用占位）。"""
        if env_name := entry.get("api_key_env"):
            key = os.environ.get(env_name, "")
            if not key and entry.get("provider") not in ("lmstudio", "openai"):
                # openai provider 可能接的也是本地 vLLM/Ollama（免 key），只警告不阻断
                logger.warning(
                    "环境变量 %s 未设置（厂商 %s）——"
                    "本地服务可忽略；云端厂商调用会 401", env_name, name)
            return key
        return entry.get("api_key", "")

    def get(self, ref: str) -> ModelConfig:
        """ref 形如 "deepseek/deepseek-chat"（厂商名/模型名）。

        模型名可在 providers 节声明（defaults），也可在 ref 里覆盖。
        "auto" 模型（LM Studio）保留原样，由适配器首次使用时发现。
        """
        if ref in self._configs:
            return self._configs[ref]

        provider_name, _, model = ref.partition("/")
        entry = self._providers.get(provider_name)
        if entry is None:
            raise KeyError(
                f"models.yaml 里没有厂商 {provider_name!r}。"
                f"已配置: {sorted(self._providers)}（ref={ref!r}）")

        model = model or entry.get("model", "")
        if not model:
            raise KeyError(f"{ref!r} 未指定模型名（providers.{provider_name}.model "
                           f"缺失且 ref 里也没写）")

        cfg = ModelConfig(
            provider=entry.get("provider", "openai"),   # 协议适配器，默认 openai 兼容
            base_url=entry.get("base_url", ""),
            model=model,
            api_key=self._resolve_key(entry, provider_name),
            api_key_env=entry.get("api_key_env"),
            timeout=float(entry.get("timeout", 120.0)),
            pricing=tuple(entry.get("pricing", (0.0, 0.0))),
            default_headers=dict(entry.get("default_headers") or {}),
            extra_body=dict(entry.get("extra_body") or {}),
        )
        self._configs[ref] = cfg
        return cfg

    # ---------- 模式路由 ----------

    def route(self, mode: str) -> RouteRule:
        """ask/craft/plan/multi → 路由规则。未配置的模式回退 ask。"""
        rule = self._routing.get(mode)
        if rule is None:
            rule = self._routing.get("ask")
        if rule is None:
            first = next(iter(self._providers))
            model = self._providers[first].get("model", "auto")
            logger.warning("routing.%s 未配置，回退 %s/%s", mode, first, model)
            return RouteRule(ref=f"{first}/{model}")
        return RouteRule(
            ref=rule.get("ref", ""),
            temperature=rule.get("temperature"),
            top_p=rule.get("top_p"),
            max_tokens=rule.get("max_tokens"),
        )

    # ---------- 便捷出口 ----------

    def llm(self, ref: str):
        """按引用取适配器实例：registry.llm("lmstudio/auto")。"""
        return get_llm(self.get(ref), cache=self._cache)

    def llm_for_mode(self, mode: str):
        """按模式取适配器实例（craft 用稳模型、ask 用活模型……全在 yaml 定）。"""
        return self.llm(self.route(mode).ref)

    # ---------- LM Studio / Ollama 模型发现 ----------

    async def discover_models(self, provider_name: str) -> list[str]:
        """查本地服务的已加载模型列表（GET /v1/models）。
        用途：①LM Studio auto 模式解析 ②CLI 里 `godot-agent models` 展示。"""
        entry = self._providers.get(provider_name)
        if entry is None:
            raise KeyError(f"未配置的厂商: {provider_name}")
        base = entry.get("base_url", "")
        if not base:
            raise ValueError(f"{provider_name} 未配置 base_url")
        async with httpx.AsyncClient(timeout=10) as client:
            headers = {}
            if (env := entry.get("api_key_env")) and os.environ.get(env):
                headers["Authorization"] = f"Bearer {os.environ[env]}"
            resp = await client.get(f"{base}/models", headers=headers)
            resp.raise_for_status()
            return [m["id"] for m in resp.json().get("data", [])]

    # ---------- 人话输出 ----------

    def describe(self) -> str:
        """CLI 用的配置概览。"""
        lines = [f"配置文件: {self._source}", "providers:"]
        for name, entry in self._providers.items():
            model = entry.get("model", "(ref 内指定)")
            key = entry.get("api_key_env", "无需密钥" )
            ok = "✓已设置" if (entry.get("api_key_env") and
                                os.environ.get(entry["api_key_env"])) else \
                 ("-" if not entry.get("api_key_env") else "✗未设置")
            lines.append(f"  {name:12s} {entry.get('provider', 'openai'):10s}"
                         f" {model:20s} key[{key}:{ok}]")
        lines.append("routing:")
        for mode, rule in self._routing.items():
            lines.append(f"  {mode:8s} → {rule.get('ref')}"
                         f" (T={rule.get('temperature')})")
        return "\n".join(lines)


def load_registry(config_path: str | Path | None = None) -> ModelRegistry:
    """工厂：找 models.yaml 并构建注册中心。

    查找顺序：显式路径 → 项目根 config/models.yaml（相对 backend/ 逐级上溯）。
    """
    if config_path is not None:
        path = Path(config_path)
        if not path.exists():
            raise FileNotFoundError(f"配置文件不存在: {path}")
        return _load_yaml(path)

    for path in DEFAULT_CONFIG_PATHS:
        if path.exists():
            return _load_yaml(path)
    raise FileNotFoundError(
        "找不到 config/models.yaml（在下列位置均不存在: "
        f"{', '.join(str(p) for p in DEFAULT_CONFIG_PATHS)}）")


def _load_yaml(path: Path) -> ModelRegistry:
    with path.open(encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    return ModelRegistry(data, path)
