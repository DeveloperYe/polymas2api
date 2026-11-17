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
