"""Constants and endpoint tables for the polymas (智慧树 / Polymas) platform."""

from __future__ import annotations

__all__ = [
    "CLOUD_API",
    "CHAT_STREAM_PATH",
    "MODEL_PROPERTIES_PATH",
    "CHAT_NEW_PATH",
    "USER_SIG_PATH",
    "SDK_INFO_PATH",
    "DEFAULT_MODEL",
    "DEFAULT_APP_CODE",
    "DEFAULT_FRIEND_NID",
    "DEFAULT_BIZ_CODE",
    "SSE_EVENT_ANSWER",
    "SSE_EVENT_REASONING",
    "SSE_EVENT_FINISHED",
    "SSE_EVENT_ERROR",
    "SSE_EVENT_TOOL_REQUEST",
    "SSE_EVENT_TOOL_RESPONSE",
    "SSE_EVENT_DEEP_RAG",
    "MODEL_CONTEXT_LENGTHS",
    "DEFAULT_CONTEXT_LENGTH",
]

#: Base URL of every business endpoint.
#:
#: Measured 2026-11-17: ``openapi.polymas.com`` serves nothing (every path
#: answers ``404 No static resource``). ``cloudapi.polymas.com/v1/chat/completions``
#: *does* answer in OpenAI shape, but it is not a usable model endpoint — it
#: forces a tool call (inventing ``no_useful_tool`` when none is declared),
#: echoes the prompt back as the answer, and **drops images**: a 256x256 PNG
#: in an ``image_url`` part leaves ``prompt_tokens`` unchanged at 15. So the
#: assistant SSE route below remains the only usable surface.
CLOUD_API = "https://cloudapi.polymas.com"

#: The v5 assistant SSE endpoint. Its URL is normally handed to the client
#: inside an IM message (``callbackUrl``), but it is a stable public path.
CHAT_STREAM_PATH = "/ai-agent/assistant/v1/chat/stream"

#: Starts a fresh assistant session (clears the long-term memory window).
CHAT_NEW_PATH = "/ai-agent/assistant/v1/chat/new"

#: One agent's persona. ``GET`` reads it, ``PUT`` writes it; the written
#: ``behaviorRules`` ends up in that agent's own system prompt, which is the only
#: place a client can argue with the built-in toolbelt (see ``persona.py``).
PERSONA_PATH = "/ai-agent/persona/{}"

#: Model catalogue for one agent. ``appCode`` is the agent nid.
MODEL_PROPERTIES_PATH = "/chatim/v1/robot/model/properties"

#: IM login signature; ``userSig`` is the query parameter of the IM websocket.
USER_SIG_PATH = "/chatim-user/v1/query/user-sig"

#: IM application id.
SDK_INFO_PATH = "/chatim/v1/system/sdk/info"

#: Creates an agent under the signed-in account. Measured 2026-11-16: a
#: STUDENT account may call it, and the reply's ``data`` is the new agent nid.
#: The result is an unpublished shell (``isPublish: 0``) — it does not appear
#: in the account's agent list, but it answers chat normally, which is all this
#: gateway needs.
SAVE_ASSISTANT_PATH = "/llmOps/application/saveAssistant"

#: The model the 小真 agent is pinned to (DB-configured, ``source: "DB"``).
DEFAULT_MODEL = "deepseek-v4.1-flash"

#: 小真's agent nid. Doubles as the ``appCode`` of the model catalogue.
DEFAULT_APP_CODE = "AgEnTnId01"

#: The ``toNid`` of every request: the assistant you are talking to.
DEFAULT_FRIEND_NID = "AgEnTnId01"

#: Business code that scopes the assistant to the teaching centre.
DEFAULT_BIZ_CODE = "hike_teach_center"

#: The platform's canned reply when a request is too large or the prefill times
#: out. It arrives as a *normal* ``ANSWER`` with no error code and no HTTP
#: failure, so anything that trusts the status code treats a dead turn as a
#: successful one (docs/LIMITS.md 1: measured at ~1M tokens, 54-80 s).
PLATFORM_BUSY_MARKERS = ("服务器开小差了", "服务暂时开小差了")

SSE_EVENT_ANSWER = "ANSWER"
SSE_EVENT_REASONING = "REASONING"
SSE_EVENT_FINISHED = "ALL_FINISHED"
SSE_EVENT_ERROR = "ERROR"
#: The agent calling one of its own server-side tools, and that tool
#: answering. Verified in the platform bundle
#: (ChatPanel-BRoWSWEi-BM439MPO.js) where the handler reads
#: ``{name, id, input}`` / ``{name, id, output}`` out of ``data``.
SSE_EVENT_TOOL_REQUEST = "TOOL_REQUEST"
SSE_EVENT_TOOL_RESPONSE = "TOOL_RESPONSE"
#: Knowledge-base retrieval progress; carries no content of its own.
SSE_EVENT_DEEP_RAG = "DEEP_RAG_REASONING"

#: Declared context window per model, in tokens.
#:
#: The platform's catalogue does **not** carry this: its rows are
#: ``modelCode / modelName / alias / default / reasoningEffort / multimodal /
#: source / limitedFree`` and nothing else (measured 2026-11-18). So the number
#: has to come from the vendor documentation, keyed by model code, and it is a
#: declaration rather than something this gateway can verify.
#:
#: Sources, checked 2026-11-18:
#:   deepseek-v4.1-flash  DeepSeek API docs, "CONTEXT LENGTH: 1M" for
#:                        DeepSeek-V4.1-Flash-1017 (MAX OUTPUT 384K);
#:                        Volcengine Ark lists the same model at 1024K.
#:   deepseek-v3          DeepSeek-V3, 128K (historical; the model is retired
#:                        from DeepSeek's own price list but still selectable here).
#:   qwen3.8-max          Alibaba Cloud Model Studio: 1M (max output 128K).
#:   qwen3.8-flash        1M (max output 128K).
#:   qwen3.7-max          1M.
#:   qwen3.6-plus         1M (max output 64K).
#:   Doubao-Seed-2.1-pro  Volcengine Ark: 1024K.
#:   Doubao-Seed-2.1-turbo  256K.
#:   Doubao-Seed-2.0-mini   256K.
#:   Doubao-Seed-2.0-pro    256K.
#:   doubao-seed-evolving   1024K.
#:   Doubao-Seed-1.8      not found - the model is off the Ark listing, so the
#:                        256K of its generation is assumed.
#:
#: A wrong value here does not corrupt anything; it moves the point where a
#: client decides to compact. Keys are matched case-insensitively.
MODEL_CONTEXT_LENGTHS = {
    "deepseek-v4.1-flash": 1000000,
    "deepseek-v3": 131072,
    "qwen3.8-max": 1000000,
    "qwen3.8-flash": 1000000,
    "qwen3.7-max": 1000000,
    "qwen3.6-plus": 1000000,
    "doubao-seed-2.1-pro": 1048576,
    "doubao-seed-2.1-turbo": 262144,
    "doubao-seed-2.0-mini": 262144,
    "doubao-seed-2.0-pro": 262144,
    "doubao-seed-1.8": 262144,
    "doubao-seed-evolving": 1048576,
}

#: Used when the platform does not declare a length and the table above has no
#: entry. The platform accepted a 1,500,000-character prompt and failed at
#: 1,600,000 (docs/LIMITS.md §1), so 1M is the declared working figure.
DEFAULT_CONTEXT_LENGTH = 1000000

