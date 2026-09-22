"""本地 embedding 后端：fastembed + BGE-small-zh（可选依赖，全程 fail-open）。

为什么需要它（2026-09-22 实测结论）
====================================
1. **语义检索的凭证来源是「主对话 provider」**：`semantic_search._call_embedding_api()`
   复用 `llm._resolve_provider()`，而本机 provider 只有火山 Agent Plan / 小米 / 通义。
   实测火山 Agent Plan **不覆盖** `/embeddings`
   （404 `UnsupportedModel: does not support the agent plan feature`），
   换标准 `/api/v3` 又是 401（plan key 不通用）。
2. **关键词兜底对中文自然语言查询实际上是 0 命中**：`ExternalStore.search_items_keyword()`
   按 ``re.split(r"\\s+")`` 拆词，中文长句没有空格 → 整句当一个关键词 →
   ``LIKE '%整句%'`` 必然落空。实测 4 个中文查询（「AI 对写作和创作行业有什么影响」
   「最近的文化交流活动」「中东局势」「股市行情」）**全部 0 命中**，而本地 BGE 的
   top-3 相关度 0.47–0.67 且命中准确（112 条 digested 语料）。
   ⇒ 这条通道不是「锦上添花」，是**让已接线但失效的通道真正工作**。

设计
====
- **可选依赖**：fastembed 不在核心依赖里。没装 → `available()` 返回 False，
  上游 fail-open 回落到 API / 关键词，行为与不引入本模块时**完全一致**。
- **懒加载 + 单例**：模型首次使用时才载入（onnxruntime 常驻约 100MB），进程内复用；
  载入失败**不缓存失败态**（下次仍可重试）。
- **模型缓存目录**：默认 ``<deskmate_base_dir>/models`` —— 与环境隔离口径一致
  （测试 / 多 workspace 不互相污染），也便于随应用数据一起清理。
  ``DESKMATE_EMBEDDING_MODEL_DIR`` 可覆盖。
- **镜像**：首次下载时若 ``HF_ENDPOINT`` 未设置，**临时**指向 ``hf-mirror.com``
  （默认端点在国内常被代理拦：实测 ``httpx.ProxyError: 502 Bad Gateway``），
  载入结束即还原，不留全局副作用。

体积与许可（DMG 相关）
======================
- 依赖增量约 **185MB**（onnxruntime 80MB / numpy 34MB / PIL 14MB / tokenizers 9.5MB …）
- 模型 **约 102MB**（fp32 ONNX，512 维）
- 许可：fastembed Apache-2.0、onnxruntime MIT、BGE 模型 MIT ⇒ **DMG-safe**。
"""

from __future__ import annotations

import logging
import os
import threading
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

#: 默认模型（中文小模型，512 维；实测相关句余弦 0.91 / 无关句 0.22）
DEFAULT_MODEL = "BAAI/bge-small-zh-v1.5"
#: 默认维度（用于写入 external_items.embedding_dim 与混用检测）
DEFAULT_DIM = 512
#: 国内可达的 HF 镜像（仅在 HF_ENDPOINT 未显式设置时临时启用）
DEFAULT_MIRROR = "https://hf-mirror.com"

_ENV_MODEL = "DESKMATE_LOCAL_EMBEDDING_MODEL"
_ENV_DIR = "DESKMATE_EMBEDDING_MODEL_DIR"
_ENV_ENDPOINT = "HF_ENDPOINT"

_lock = threading.Lock()
_model = None
_last_error: Optional[str] = None


def model_name() -> str:
    """当前本地模型名（环境变量可覆盖）。"""
    return (os.environ.get(_ENV_MODEL, "") or "").strip() or DEFAULT_MODEL


def available() -> bool:
    """fastembed 是否可用。**只查包存在**，不加载模型（零成本、零网络）。"""
    try:
        import importlib.util

        return importlib.util.find_spec("fastembed") is not None
    except Exception:
        return False


def cache_dir() -> Path:
    """模型缓存目录。**调用时**解析，遵守 ``DESKMATE_DIR`` 隔离口径。"""
    explicit = (os.environ.get(_ENV_DIR, "") or "").strip()
    if explicit:
        return Path(explicit)
    from deskmate.paths import deskmate_base_dir

    return deskmate_base_dir() / "models"


def last_error() -> Optional[str]:
    """最近一次失败原因（None = 尚未失败）。供诊断 / MCP 查询。"""
    return _last_error


def _load():
    """载入并缓存模型实例；失败返回 None（**不缓存失败态**）。"""
    global _model, _last_error
    if _model is not None:
        return _model
    with _lock:
        if _model is not None:          # 双检：等锁期间可能已被别的线程载入
            return _model
        if not available():
            _last_error = "fastembed 未安装（pip install fastembed）"
            return None
        # 临时启用镜像：fastembed 在**构造时**下载模型（实测会打印 Fetching N files），
        # 所以载入结束即可还原，不会影响后续调用。
        prev_endpoint = os.environ.get(_ENV_ENDPOINT)
        if not prev_endpoint:
            os.environ[_ENV_ENDPOINT] = DEFAULT_MIRROR
        try:
            from fastembed import TextEmbedding

            d = cache_dir()
            d.mkdir(parents=True, exist_ok=True)
            _model = TextEmbedding(model_name(), cache_dir=str(d))
            _last_error = None
            logger.info("本地 embedding 就绪：%s（缓存 %s）", model_name(), d)
            return _model
        except Exception as exc:
            _last_error = f"{type(exc).__name__}: {exc}"
            logger.warning(
                "本地 embedding 模型载入失败（fail-open，将回落 API/关键词）：%s",
                _last_error,
            )
            return None
        finally:
            if not prev_endpoint:
                os.environ.pop(_ENV_ENDPOINT, None)


def embed(texts: list[str]) -> Optional[list[list[float]]]:
    """批量生成向量。

    返回向量列表；fastembed 未安装 / 模型载入失败 / 推理异常一律返回 ``None``
    （fail-open，调用方负责回落）。
    """
    global _last_error
    if not texts:
        return []
    model = _load()
    if model is None:
        return None
    try:
        return [[float(x) for x in vec] for vec in model.embed(texts)]
    except Exception as exc:
        _last_error = f"{type(exc).__name__}: {exc}"
        logger.warning("本地 embedding 推理失败（fail-open）：%s", _last_error)
        return None


def reset_cache() -> None:
    """清空模型单例与错误态（**仅供测试**）。"""
    global _model, _last_error
    with _lock:
        _model = None
        _last_error = None
