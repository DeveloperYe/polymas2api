"""Tests for the tool-calling bridge.

The platform will not accept tool definitions on the wire, so the bridge
describes the caller's tools in the prompt and parses the answer back into
``tool_calls``.

The wire format is deliberately *not* a "tool call": the assistant refuses
to emit anything that claims it is invoking a tool (it calls that forging a
system action), and a ```` ```tool_call ```` fence was rejected verbatim on
2026-11-16. So the model is asked to hand back parameters on a ``CALL`` line
for the caller to execute, and these tests pin that format, the parser's
tolerance, and the round-trip through ``extract_prompt``.
"""

from __future__ import annotations

import json

from polymas2api.adapter import OpenAIAdapter
from polymas2api.toolbridge import (
    ToolCallStream,
    build_tool_prompt,
    has_unterminated_call,
    parse_tool_calls,
    render_tool_result,
)

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "get_weather",
            "description": "查天气",
            "parameters": {
                "type": "object",
                "properties": {"city": {"type": "string"}},
                "required": ["city"],
            },
        },
    }
]


# -- prompt building ---------------------------------------------------------
def _prompt(*args, **kwargs):
    """The prompt text only; the alias map has its own tests."""
    text, _aliases = build_tool_prompt(*args, **kwargs)
    return text


def test_no_tools_means_no_instruction():
    """A request without tools must be byte-identical to before."""
    assert _prompt([]) == ""
    assert _prompt(None) == ""


def test_tool_prompt_names_the_tool_and_the_marker():
    prompt = _prompt(TOOLS)
    assert "get_weather" in prompt
    assert "查天气" in prompt
    assert "properties" in prompt  # the schema must survive
    assert "CALL" in prompt


def test_tool_prompt_does_not_claim_a_tool_call():
    """The framing is what makes the assistant comply."""
    prompt = _prompt(TOOLS)
    assert "由那个程序去真正执行" in prompt or "外部程序执行" in prompt
    # Saying "you are calling a tool" is exactly what it refuses.
    assert "你正在调用工具" not in prompt


def test_tool_prompt_asks_for_an_english_name():
    """The model echoes the schema name; telling it to keep it verbatim stops
    it translating ``run_command`` into Chinese."""
    prompt = _prompt(TOOLS)
    assert "逐字照抄" in prompt
    assert "工具名也保持英文" in prompt


def test_tool_prompt_accepts_the_flattened_shape():
    flat = [{"name": "ping", "description": "ping", "parameters": {"type": "object"}}]
    assert "ping" in _prompt(flat)


def test_tool_choice_none_removes_the_instruction():
    assert _prompt(TOOLS, "none") == ""


def test_tool_choice_required_demands_a_call():
    assert "必须给出一行 CALL" in _prompt(TOOLS, "required")


# -- tool name style ---------------------------------------------------------
def test_verbatim_style_keeps_an_english_name():
    prompt, aliases = build_tool_prompt(TOOLS)
    assert aliases == {}
    assert "- get_weather" in prompt


def test_verbatim_style_keeps_a_chinese_name():
    chinese = [{"type": "function", "function": {
        "name": "执行命令", "description": "跑 shell",
        "parameters": {"type": "object"}}}]
    prompt, aliases = build_tool_prompt(chinese)
    assert aliases == {}
    assert "- 执行命令" in prompt


def test_ascii_style_aliases_a_chinese_name():
    """A strict OpenAI client only accepts ``^[a-zA-Z0-9_-]{1,64}$``."""
    chinese = [{"type": "function", "function": {
        "name": "执行命令", "description": "跑 shell",
        "parameters": {"type": "object"}}}]
    prompt, aliases = build_tool_prompt(chinese, name_style="ascii")
    assert aliases == {"run_command": "执行命令"}
    assert "- run_command" in prompt
    # Only the tool list must be free of the Chinese name; rule 8 mentions it
    # on purpose, as an example of the agent's own tools.
    tool_list = prompt.split("可用工具：", 1)[1].split("要求：", 1)[0]
    assert "执行命令" not in tool_list


def test_ascii_style_leaves_an_english_name_alone():
    prompt, aliases = build_tool_prompt(TOOLS, name_style="ascii")
    assert aliases == {}
    assert "- get_weather" in prompt


def test_ascii_style_falls_back_for_an_unknown_name():
    odd = [{"type": "function", "function": {
        "name": "自定义工具", "description": "x", "parameters": {"type": "object"}}}]
    _prompt_text, aliases = build_tool_prompt(odd, name_style="ascii")
    assert len(aliases) == 1
    emitted = next(iter(aliases))
    assert emitted.isascii() and emitted


# -- parsing -----------------------------------------------------------------
def test_parses_the_documented_line():
    text = '北京天气我查一下。\n\nCALL get_weather {"city": "北京"}\n'
    content, calls = parse_tool_calls(text)
    assert len(calls) == 1
    assert calls[0].name == "get_weather"
    assert json.loads(calls[0].arguments) == {"city": "北京"}
    assert "查一下" in content
    assert "CALL" not in content


def test_parses_the_line_without_arguments():
    """A tool with no parameters still gets a usable call."""
    content, calls = parse_tool_calls("CALL ping {}")
    assert calls and calls[0].name == "ping"


def test_parses_a_quoted_name_form():
    content, calls = parse_tool_calls('CALL <get_weather> {"city": "上海"}')
    assert calls and calls[0].name == "get_weather"


def test_parses_a_tool_call_fence_for_other_agents():
    """Other agents do produce this; the parser accepts it."""
    text = '```tool_call\n{"name": "get_weather", "arguments": {"city": "北京"}}\n```'
    content, calls = parse_tool_calls(text)
    assert calls and calls[0].name == "get_weather"
    assert content == ""


def test_parses_a_json_fence_with_action_shape():
    text = '```json\n{"action": "get_weather", "input": {"city": "北京"}}\n```'
    content, calls = parse_tool_calls(text)
    assert calls and calls[0].name == "get_weather"
    assert json.loads(calls[0].arguments) == {"city": "北京"}


def test_parses_a_bare_function_object():
    content, calls = parse_tool_calls('{"function": "get_weather", "args": {"city": "深圳"}}')
    assert calls and calls[0].name == "get_weather"


def test_leaves_ordinary_text_alone():
    text = "今天北京 25 度，晴。"
    content, calls = parse_tool_calls(text)
    assert calls == []
    assert content == text


def test_leaves_an_ordinary_code_block_alone():
    text = "```javascript\nconsole.log(1)\n```"
    content, calls = parse_tool_calls(text)
    assert calls == []
    assert "console.log(1)" in content


def test_a_data_answer_without_arguments_is_not_a_call():
    """A data answer must not be mistaken for a call."""
    text = '{"name": "张三", "score": 90}'
    content, calls = parse_tool_calls(text)
    assert calls == []
    assert "张三" in content


def test_a_call_inside_a_long_answer_needs_both_keys():
    text = 'CALL get_weather {"city":"北京"}'
    _, calls = parse_tool_calls(text)
    assert calls


def test_arguments_are_json_text_not_an_object():
    """OpenAI sends ``arguments`` as a string."""
    _, calls = parse_tool_calls('CALL f {"a": 1}')
    assert isinstance(calls[0].arguments, str)
    assert json.loads(calls[0].arguments) == {"a": 1}


def test_missing_id_is_generated():
    _, calls = parse_tool_calls("CALL f {}")
    assert calls[0].id.startswith("call_")


def test_the_openai_shape_is_produced():
    _, calls = parse_tool_calls('CALL f {"a": 1}')
    payload = calls[0].to_openai(0)
    assert payload["index"] == 0
    assert payload["type"] == "function"
    assert payload["function"]["name"] == "f"
    assert json.loads(payload["function"]["arguments"]) == {"a": 1}


# -- streaming ---------------------------------------------------------------
def test_a_marker_split_across_chunks_is_still_found():
    parser = ToolCallStream(enabled=True)
    pieces = ["先说一句。", "\n\nCA", "LL get_weather ", '{"city": "北京"}']
    emitted = "".join(parser.feed(piece) for piece in pieces)
    tail, calls = parser.flush()
    assert "先说一句" in emitted
    assert "CALL" not in emitted
    assert "CALL" not in tail
    assert len(calls) == 1
    assert calls[0].name == "get_weather"


def test_plain_text_streams_through_unchanged():
    parser = ToolCallStream(enabled=True)
    emitted = "".join(parser.feed("你好，") + parser.feed("今天不错。"))
    tail, calls = parser.flush()
    assert calls == []
    assert (emitted + tail) == "你好，今天不错。"


def test_disabled_stream_is_a_passthrough():
    parser = ToolCallStream(enabled=False)
    assert parser.feed("CALL f {}") == "CALL f {}"
    assert parser.flush() == ("", [])


# -- extract_prompt round-trip ----------------------------------------------
def test_a_tool_result_after_the_question_is_kept():
    """This is what a tool round-trip looks like, and it used to be dropped."""
    messages = [
        {"role": "user", "content": "北京天气怎么样"},
        {"role": "assistant", "content": "",
         "tool_calls": [{"id": "call_0001", "type": "function",
                         "function": {"name": "get_weather",
                                      "arguments": '{"city": "北京"}'}}]},
        {"role": "tool", "tool_call_id": "call_0001", "name": "get_weather",
         "content": "25 度，晴"},
    ]
    prompt = OpenAIAdapter.extract_prompt(messages)
    assert "北京天气怎么样" in prompt
    assert "25 度，晴" in prompt
    assert "get_weather" in prompt


def test_the_rendered_earlier_call_uses_the_same_wire_format():
    messages = [
        {"role": "user", "content": "查天气"},
        {"role": "assistant", "content": "",
         "tool_calls": [{"id": "c1", "type": "function",
                         "function": {"name": "get_weather",
                                      "arguments": '{"city": "北京"}'}}]},
        {"role": "tool", "tool_call_id": "c1", "content": "晴"},
    ]
    prompt = OpenAIAdapter.extract_prompt(messages)
    assert "CALL get_weather" in prompt


def test_the_extra_system_block_is_included():
    messages = [{"role": "user", "content": "hi"}]
    prompt = OpenAIAdapter.extract_prompt(messages, extra_system="[调用参数整理]\nfoo")
    assert "调用参数整理" in prompt
    assert "hi" in prompt


def test_system_message_becomes_an_instruction_block():
    messages = [
        {"role": "system", "content": "你是助手"},
        {"role": "user", "content": "hi"},
    ]
    prompt = OpenAIAdapter.extract_prompt(messages)
    assert "你是助手" in prompt
    assert "系统指令" in prompt


def test_render_tool_result_is_labelled():
    text = render_tool_result("get_weather", "25 度", "call_1")
    assert "get_weather" in text
    assert "25 度" in text


# -- adapter -----------------------------------------------------------------
class _StubConfig:
    model = "deepseek-v4.1-flash"
    expose_upstream_tools = True


class _Events:
    def __init__(self, events):
        self.events = events

    def chat(self, prompt, *, model="", session_id=""):
        for event, payload in self.events:
            yield event, payload


def test_complete_returns_tool_calls():
    client = _Events([
        ("ANSWER", {"text": '我查一下。\n\nCALL get_weather {"city": "北京"}'}),
        ("ALL_FINISHED", {"text": None}),
    ])
    adapter = OpenAIAdapter(_StubConfig())
    result = adapter.complete(client, "hi", tools=TOOLS)
    choice = result["choices"][0]
    assert choice["finish_reason"] == "tool_calls"
    message = choice["message"]
    assert message["tool_calls"][0]["function"]["name"] == "get_weather"
    assert "CALL" not in (message.get("content") or "")


def test_complete_without_tools_never_looks_for_a_call():
    """Without tools the line is the model's own text, not a call."""
    client = _Events([
        ("ANSWER", {"text": 'CALL get_weather {"city": "北京"}'}),
        ("ALL_FINISHED", {"text": None}),
    ])
    adapter = OpenAIAdapter(_StubConfig())
    result = adapter.complete(client, "hi")
    assert result["choices"][0]["finish_reason"] == "stop"
    assert "tool_calls" not in result["choices"][0]["message"]


def test_stream_emits_tool_calls_and_the_right_finish_reason():
    client = _Events([
        ("ANSWER", {"text": "我查一下。\n"}),
        ("ANSWER", {"text": "CALL get_weather "}),
        ("ANSWER", {"text": '{"city": "北京"}'}),
        ("ALL_FINISHED", {"text": None}),
    ])
    adapter = OpenAIAdapter(_StubConfig())
    chunks = list(adapter.stream(client, "hi", tools=TOOLS))
    text = "".join(chunk["choices"][0]["delta"].get("content") or "" for chunk in chunks)
    calls = [chunk["choices"][0]["delta"]["tool_calls"]
             for chunk in chunks if chunk["choices"][0]["delta"].get("tool_calls")]
    assert "CALL" not in text
    assert "我查一下" in text
    assert calls and calls[0][0]["function"]["name"] == "get_weather"
    assert chunks[-1]["choices"][0]["finish_reason"] == "tool_calls"


def test_stream_without_tools_still_ends_with_stop():
    client = _Events([
        ("ANSWER", {"text": "你好"}),
        ("ALL_FINISHED", {"text": None}),
    ])
    adapter = OpenAIAdapter(_StubConfig())
    chunks = list(adapter.stream(client, "hi"))
    assert chunks[-1]["choices"][0]["finish_reason"] == "stop"


def test_upstream_tool_events_are_reported():
    """The agent's own tools surface as metadata, never as tool_calls."""
    client = _Events([
        ("TOOL_REQUEST", {"data": {"id": "t1", "name": "search_course",
                                   "input": {"q": "作业"}}}),
        ("TOOL_RESPONSE", {"data": {"id": "t1", "name": "search_course",
                                    "output": "3 条结果"}}),
        ("ANSWER", {"text": "查到了。"}),
        ("ALL_FINISHED", {"text": None}),
    ])
    adapter = OpenAIAdapter(_StubConfig())
    result = adapter.complete(client, "hi")
    tools = result["polymas"]["upstream_tools"]
    assert [item["event"] for item in tools] == ["TOOL_REQUEST", "TOOL_RESPONSE"]
    assert tools[0]["name"] == "search_course"
    assert tools[1]["output"] == "3 条结果"
    assert result["choices"][0]["finish_reason"] == "stop"
    assert "tool_calls" not in result["choices"][0]["message"]


def test_upstream_tool_stream_emits_a_polymas_field():
    client = _Events([
        ("TOOL_REQUEST", {"data": {"id": "t1", "name": "search_course",
                                   "input": {}}}),
        ("ANSWER", {"text": "好"}),
        ("ALL_FINISHED", {"text": None}),
    ])
    adapter = OpenAIAdapter(_StubConfig())
    chunks = list(adapter.stream(client, "hi"))
    reported = [chunk["polymas"]["upstream_tool"] for chunk in chunks
                if chunk.get("polymas")]
    assert reported and reported[0]["name"] == "search_course"


def test_upstream_tools_can_be_turned_off():
    class _Off(_StubConfig):
        expose_upstream_tools = False

    client = _Events([
        ("TOOL_REQUEST", {"data": {"id": "t1", "name": "x", "input": {}}}),
        ("ANSWER", {"text": "好"}),
        ("ALL_FINISHED", {"text": None}),
    ])
    adapter = OpenAIAdapter(_Off())
    assert "polymas" not in adapter.complete(client, "hi")

# -- non-ASCII tool names ----------------------------------------------------
def test_a_chinese_tool_name_is_parsed():
    """The agent's own tools are Chinese, and a caller may reuse those names.

    The first version matched ASCII only, so ``CALL 执行命令 {...}`` was left
    in the answer as plain text, the client saw no ``tool_calls`` and nothing
    ran. Caught 2026-11-16.
    """
    content, calls = parse_tool_calls('CALL 执行命令 {"command": "echo hi"}')
    assert len(calls) == 1
    assert calls[0].name == "执行命令"
    assert json.loads(calls[0].arguments) == {"command": "echo hi"}
    assert content == ""


def test_chinese_names_with_no_arguments():
    _, calls = parse_tool_calls("CALL 查询当前时间 {}")
    assert calls and calls[0].name == "查询当前时间"


def test_a_quoted_chinese_name_loses_its_brackets():
    for text in ('CALL <执行命令> {"command": "ls"}',
                 'CALL 「执行命令」 {"command": "ls"}',
                 'CALL 【执行命令】 {"command": "ls"}'):
        _, calls = parse_tool_calls(text)
        assert calls and calls[0].name == "执行命令", text


def test_a_chinese_name_streams_across_chunks():
    parser = ToolCallStream(enabled=True)
    emitted = "".join(parser.feed(piece) for piece in
                      ["先看一", "下。\n\nCA", "LL 执行", "命令 ", '{"command": "echo hi"}'])
    tail, calls = parser.flush()
    assert "CALL" not in emitted
    assert calls and calls[0].name == "执行命令"
    assert json.loads(calls[0].arguments) == {"command": "echo hi"}


def test_the_adapter_returns_a_chinese_named_tool_call():
    tool = [{"type": "function", "function": {
        "name": "执行命令", "description": "在容器内执行 shell 命令",
        "parameters": {"type": "object",
                       "properties": {"command": {"type": "string"}},
                       "required": ["command"]}}}]
    client = _Events([
        ("ANSWER", {"text": 'CALL 执行命令 {"command": "echo hi"}'}),
        ("ALL_FINISHED", {"text": None}),
    ])
    adapter = OpenAIAdapter(_StubConfig())
    result = adapter.complete(client, "hi", tools=tool)
    choice = result["choices"][0]
    assert choice["finish_reason"] == "tool_calls"
    assert choice["message"]["tool_calls"][0]["function"]["name"] == "执行命令"
def test_alignment_points_a_translated_name_back():
    """Asked to emit the English identifier, it once answered ``execute``
    for a tool declared as ``执行命令``. A client matches literally."""
    from polymas2api.toolbridge import ToolCall, align_call_names

    calls = [ToolCall(name="execute", arguments="{}")]
    assert align_call_names(calls, ["执行命令"])[0].name == "执行命令"


def test_alignment_is_case_insensitive():
    from polymas2api.toolbridge import ToolCall, align_call_names

    calls = [ToolCall(name="Run_Command", arguments="{}")]
    assert align_call_names(calls, ["run_command"])[0].name == "run_command"


def test_alignment_leaves_an_unknown_name_alone_with_several_tools():
    """Guessing between tools would be worse than passing the name through."""
    from polymas2api.toolbridge import ToolCall, align_call_names

    calls = [ToolCall(name="zzz", arguments="{}")]
    assert align_call_names(calls, ["a", "b"])[0].name == "zzz"
def test_the_prompt_forbids_the_agents_own_tools():
    """The agent's toolbelt runs in the platform's container, not the caller's.

    Measured 2026-11-17: without this rule the assistant called 查看文件列表
    six times and 执行命令 twice for "read your working directory", and never
    emitted a CALL line — the caller saw no tool call at all. With the rule
    the same question produced zero native calls and a correct CALL.
    """
    prompt, _ = build_tool_prompt(TOOLS)
    assert "不要使用你自己附带的任何工具" in prompt
    assert "执行命令" in prompt  # named so the model recognises its own
# -- the model fences the CALL line against the rules ------------------------
def test_a_fenced_call_leaves_no_empty_fence_behind():
    """Rule 3 says not to wrap the line in a fence; the model does it anyway.

    Removing the line then left ``` … ``` (or ```json … ```) in the answer,
    and the client rendered that as a stray empty code block (2026-11-17).
    """
    for wrapped in ("```\nCALL get_weather {\"city\": \"北京\"}\n```",
                    "```json\nCALL get_weather {\"city\": \"北京\"}\n```",
                    "```powershell\nCALL get_weather {\"city\": \"北京\"}\n```"):
        content, calls = parse_tool_calls("好的。\n" + wrapped + "\n以上。")
        assert [call.name for call in calls] == ["get_weather"]
        assert "```" not in content, content


def test_a_call_in_inline_code_is_still_parsed():
    """Before this it was not parsed at all and stayed in the answer."""
    content, calls = parse_tool_calls('好的。\n`CALL get_weather {"city": "北京"}`')
    assert [call.name for call in calls] == ["get_weather"]
    assert "CALL" not in content


def test_an_ordinary_code_block_is_left_alone():
    text = "代码如下：\n```python\nprint(1)\n```"
    content, calls = parse_tool_calls(text)
    assert calls == []
    assert content.strip() == text.strip()
# -- nested parameter depth ------------------------------------------------
_NESTED = {"type": "object", "properties": {
    "questions": {"type": "array", "items": {
        "type": "object",
        "properties": {"id": {"type": "string"},
                       "question": {"type": "string"},
                       "options": {"type": "array", "items": {"type": "string"}}},
        "required": ["id", "question"]}}},
    "required": ["questions"]}


def test_depth_two_shows_the_fields_inside_an_array_of_records():
    """Depth 0 rendered this as ``questions:array<object>`` — every inner
    field hidden, leaving the model to guess the shape (2026-11-17)."""
    from polymas2api.toolbridge import _compact_parameters

    text = _compact_parameters(_NESTED, 2)
    assert "id:string*" in text
    assert "question:string*" in text
    assert "options:array<string>" in text


def test_depth_zero_still_renders_the_flat_signature():
    from polymas2api.toolbridge import _compact_parameters

    text = _compact_parameters(_NESTED, 0)
    assert text == "questions:array<object>*"


def test_depth_one_already_shows_the_record_fields():
    """One level in is what reveals ``id``/``question``; the difference
    between depth 1 and 2 only shows up on a third level of nesting."""
    from polymas2api.toolbridge import _compact_parameters

    text = _compact_parameters(_NESTED, 1)
    assert "id:string*" in text
    assert "question:string*" in text


def test_depth_two_reaches_one_level_deeper_than_depth_one():
    from polymas2api.toolbridge import _compact_parameters

    three = {"type": "object", "properties": {"a": {"type": "object", "properties": {
        "b": {"type": "object", "properties": {"c": {"type": "string"}}}}}}}
    assert "c:string" not in _compact_parameters(three, 1)
    assert "c:string" in _compact_parameters(three, 2)


def test_the_default_depth_reaches_a_four_deep_leaf():
    """Depth 2 is complete for DSH's 112 tools (measured 2026-11-17, +7% over
    flat) but not for a tool nesting four deep: its leaf fields disappeared and
    the model filled them 1/10 times. Depth 3 recovered 10/10 for 28 characters
    on an 83-tool block, so the default moved (2026-11-19).
    """
    from polymas2api.toolbridge import SCHEMA_DEPTH, _compact_parameters

    assert SCHEMA_DEPTH >= 3
    four = {"type": "object", "properties": {"a": {"type": "object", "properties": {
        "b": {"type": "object", "properties": {
            "c": {"type": "object", "properties": {"d": {"type": "string"}}}}}}}}}
    assert "d:string" in _compact_parameters(four)


def test_a_tool_with_no_parameters_says_so():
    from polymas2api.toolbridge import _compact_parameters

    assert _compact_parameters({"type": "object", "properties": {}}) == "无参数"
    assert _compact_parameters(None) == ""

# -- delegating web access to the caller -----------------------------------


def _web_tool(name="web_search", description="Search the web for current information."):
    return {"type": "function",
            "function": {"name": name, "description": description,
                         "parameters": {"type": "object", "properties": {}}}}


def test_a_declared_search_tool_is_named_in_the_prompt():
    """Measured 2026-11-18: without naming the caller's search tool, the
    assistant either searched with its own 内容搜索 or handed back a curl
    one-liner. Naming it made 5/5 runs emit CALL web_search instead."""
    prompt, _ = build_tool_prompt([_web_tool(), _web_tool("pwsh", "执行命令")])
    assert "需要联网检索或抓取网页时" in prompt
    assert "web_search 发 CALL" in prompt


def test_the_web_rule_only_appears_when_a_web_tool_exists():
    prompt, _ = build_tool_prompt([_web_tool("pwsh", "在用户本机执行命令")])
    assert "需要联网检索或抓取网页时" not in prompt


def test_web_fetch_counts_as_a_web_tool():
    prompt, _ = build_tool_prompt(
        [_web_tool("web_fetch", "Fetch the content of a specific URL")])
    assert "web_fetch 发 CALL" in prompt


def test_a_chinese_named_search_tool_is_recognised():
    prompt, _ = build_tool_prompt([_web_tool("联网搜索", "按关键词检索网页")])
    assert "需要联网检索或抓取网页时" in prompt


def test_the_native_tool_ban_covers_the_search_tools():
    prompt, _ = build_tool_prompt([_web_tool()])
    assert "「内容搜索」" in prompt and "「联网搜索」" in prompt


def test_the_rule_numbers_stay_sequential():
    """The rules are numbered by hand-built counters; an inserted rule must not
    leave a gap or a duplicate."""
    import re

    prompt, _ = build_tool_prompt([_web_tool(), _web_tool("pwsh", "执行命令")])
    numbers = [int(m.group(1))
               for m in (re.match(r"^(\d+)\. ", line) for line in prompt.splitlines())
               if m]
    assert numbers, "一个编号都没抓到，这条断言会空转"
    assert numbers == list(range(1, len(numbers) + 1))

# -- the call glued to the prose that follows it ----------------------------
def test_a_call_glued_to_following_prose_is_still_a_call():
    """No separator between the closing brace and the next sentence.

    The model wrote ``CALL pwsh {...}已下发本机命令：…`` with nothing between the
    brace and the prose (2026-11-18). A line-anchored pattern dropped the whole
    line, so the call leaked into the answer as plain text and nothing ran.
    """
    line = ('CALL pwsh {"command": "echo hi"}已下发本机命令：核对编译链路真实耗时')
    content, calls = parse_tool_calls(line)

    assert [call.name for call in calls] == ["pwsh"]
    assert json.loads(calls[0].arguments) == {"command": "echo hi"}
    # The sentence the model wrote after the brace is the user's to read.
    assert content.strip() == "已下发本机命令：核对编译链路真实耗时"


def test_a_brace_inside_an_argument_does_not_end_the_object():
    """Brace depth, not "the last } on the line", decides where the JSON ends."""
    line = 'CALL pwsh {"command": "printf \\"}\\" && echo done"}接着说'
    content, calls = parse_tool_calls(line)

    assert len(calls) == 1
    assert json.loads(calls[0].arguments)["command"] == 'printf "}" && echo done'
    assert content.strip() == "接着说"


def test_the_call_line_is_removed_even_with_prose_behind_it():
    """The call itself must not survive into the content the client renders."""
    text = "先跑个命令。\n\nCALL pwsh {\"command\": \"ls\"}跑完了再总结。"
    content, calls = parse_tool_calls(text)

    assert len(calls) == 1
    assert "CALL" not in content
    assert "先跑个命令" in content
    assert "跑完了再总结" in content


def test_a_call_without_arguments_is_left_alone():
    """A bare ``CALL name`` is not a call; the line stays in the text."""
    content, calls = parse_tool_calls("CALL foo")

    assert calls == []
    assert content.strip() == "CALL foo"


# -- a call that never closes -----------------------------------------------
def test_an_unterminated_call_is_reported():
    """The answer stopped inside the argument object: nothing can run."""
    assert has_unterminated_call('CALL pwsh {"command": "echo hi') is True
    assert has_unterminated_call('CALL pwsh {"a": {"b": 1}') is True


def test_a_complete_call_is_not_reported_as_unterminated():
    assert has_unterminated_call('CALL pwsh {"command": "echo hi"}') is False
    assert has_unterminated_call('CALL pwsh {"command": "echo hi"}接着说话') is False


def test_prose_holding_a_brace_is_not_a_truncated_call():
    """Only a real CALL line counts; a stray { in the answer is just text."""
    assert has_unterminated_call('这段 JSON 没闭合：{"a": 1') is False
    assert has_unterminated_call('') is False


# -- a backslash the model wrote for a regex, not for JSON -------------------
def test_a_regex_backslash_does_not_kill_the_call():
    r"""``\.exe`` is not legal JSON, but it is what the model means.

    Seen 2026-11-18: a PowerShell pattern ``bot-([0-9a-f]{12})\.exe`` made the
    strict parse fail, so the whole call was dropped and the CALL line leaked
    into the answer the user reads.
    """
    line = 'CALL pwsh {"command": "Select-String -Pattern \'bot-\\.exe\'"}'
    content, calls = parse_tool_calls(line)

    assert [call.name for call in calls] == ["pwsh"]
    assert "bot-\\.exe" in json.loads(calls[0].arguments)["command"]
    assert content == ""


def test_a_windows_path_with_one_backslash_still_parses():
    r"""JSON has no ``\U`` escape; a path written plainly must not drop the call."""
    line = 'CALL read_file {"path": "C:\\Users\\a.txt"}'
    content, calls = parse_tool_calls(line)

    assert len(calls) == 1
    assert json.loads(calls[0].arguments)["path"] == "C:\\Users\\a.txt"


def test_valid_json_is_never_rewritten():
    r"""The repair only runs after a strict parse fails."""
    line = 'CALL pwsh {"command": "echo a\\nb"}'
    content, calls = parse_tool_calls(line)

    assert len(calls) == 1
    # \n stays an escape, not a literal backslash-n.
    assert json.loads(calls[0].arguments)["command"] == "echo a\nb"


# -- a Windows path must survive the JSON escapes that are legal (2026-11-19)


def _path_of(raw: str) -> str:
    from polymas2api.toolbridge import parse_tool_calls

    _body, calls = parse_tool_calls(raw)
    assert calls, "the call should parse"
    arguments = calls[0].arguments
    if isinstance(arguments, str):
        import json
        arguments = json.loads(arguments)
    return str(arguments.get("path"))


def test_a_windows_path_keeps_its_backslashes():
    r"""``\n`` ``\t`` ``\b`` ``\f`` ``\r`` are *legal* JSON, so a path using them
    parses fine and comes out wrong — no error, just a path that no longer
    exists. Measured 2026-11-19: four of five lost characters this way.
    """
    cases = [
        (r'CALL read {"path":"C:\new\test.txt"}', r"C:\new\test.txt"),
        (r'CALL read {"path":"C:\bin\tool.exe"}', r"C:\bin\tool.exe"),
        (r'CALL read {"path":"C:\temp\a.log"}', r"C:\temp\a.log"),
        (r'CALL read {"path":"C:\robots\rules.txt"}', r"C:\robots\rules.txt"),
        (r'CALL read {"path":"C:\file\notes.md"}', r"C:\file\notes.md"),
    ]
    for raw, expected in cases:
        assert _path_of(raw) == expected, raw


def test_a_correctly_escaped_path_is_left_alone():
    r"""Nothing that parses to a sane path may be rewritten."""
    assert _path_of(r'CALL read {"path":"D:\\work\\ok.txt"}') == r"D:\work\ok.txt"


def test_a_real_newline_in_prose_is_not_turned_into_an_escape():
    """Only location-shaped arguments get the double-escaped reading.

    A file body written as ``content`` legitimately carries newlines; rewriting
    them into a literal ``\\n`` would corrupt the file being written.
    """
    from polymas2api.toolbridge import parse_tool_calls

    _body, calls = parse_tool_calls(r'CALL write {"path":"a.txt","content":"l1\nl2"}')
    import json
    arguments = calls[0].arguments
    if isinstance(arguments, str):
        arguments = json.loads(arguments)
    assert arguments["content"] == "l1\nl2", "the newline must stay a newline"


def test_the_tool_block_puts_the_call_where_the_persona_does():
    """Two instructions used to disagree: the block said "reply end", the
    persona says "first line". Whatever wins, the model should not be asked to
    choose (2026-11-19, reported from a sibling project).
    """
    from polymas2api.toolbridge import build_tool_prompt

    specs = [{"type": "function",
              "function": {"name": "pwsh", "description": "执行命令",
                           "parameters": {"type": "object",
                                          "properties": {"command": {"type": "string"}},
                                          "required": ["command"]}}}]
    block = build_tool_prompt(specs)
    text = block[0] if isinstance(block, tuple) else block
    assert "最前面" in text
    assert "放在回复最后" not in text


def test_the_tool_block_allows_independent_calls_in_parallel():
    """The parser and the response layer always handled several calls at once;
    one prompt rule alone was closing the door.
    """
    from polymas2api.toolbridge import build_tool_prompt

    specs = [{"type": "function",
              "function": {"name": "pwsh", "description": "执行命令",
                           "parameters": {"type": "object",
                                          "properties": {"command": {"type": "string"}},
                                          "required": ["command"]}}}]
    block = build_tool_prompt(specs)
    text = block[0] if isinstance(block, tuple) else block
    assert "多行 CALL" in text
    assert "只给出最需要先执行的那一个" not in text


def test_a_four_level_tool_names_its_leaf_fields():
    """Depth 2 names three levels; a four-deep leaf vanished and the model
    filled it 1/10 times (measured 2026-11-19). Depth 3 costs 28 characters.
    """
    from polymas2api.toolbridge import build_tool_prompt

    # plan > strategy > rolling > {batch, pause}, built outward from the leaf so
    # the nesting is readable rather than a wall of closing braces.
    leaf = {"batch": {"type": "integer"}, "pause": {"type": "string"}}
    node = leaf
    for name in ("rolling", "strategy", "plan"):
        node = {name: {"type": "object", "properties": node}}
    deep = {"type": "function",
            "function": {"name": "deploy_job", "description": "部署任务",
                         "parameters": {"type": "object", "required": ["job"],
                                        "properties": {"job": {"type": "string"}, **node}}}}
    pads = [{"type": "function",
             "function": {"name": f"util_{i:02d}", "description": "占位工具，把块撑过压缩阈值",
                          "parameters": {"type": "object", "required": ["arg"],
                                         "properties": {"arg": {"type": "string"},
                                                        "mode": {"type": "string"}}}}}
            for i in range(80)]
    block = build_tool_prompt([deep] + pads)
    text = block[0] if isinstance(block, tuple) else block
    assert "rolling:object{batch:integer pause:string}" in text, \
        "the leaf fields must survive the compacted render"


# -- a closed set must be visible in the compacted signature (2026-11-19) --


def test_a_compacted_signature_names_its_enum_members():
    """Type alone is not enough: the model invents a value.

    Measured 2026-11-19: ``mode`` accepting turbo/standard/thorough rendered as
    ``mode:string`` and the model wrote ``accurate``/``fast``/``quick``, ten of
    ten invalid across two prompts, where the full schema got all ten right.
    Naming the members costs 59 characters and recovered 15/15.
    """
    from polymas2api.toolbridge import _compact_parameters

    schema = {"type": "object", "required": ["mode"],
              "properties": {"mode": {"type": "string",
                                      "enum": ["turbo", "standard", "thorough"]},
                             "text": {"type": "string"}}}
    signature = _compact_parameters(schema)

    assert "mode:string*[turbo|standard|thorough]" in signature
    assert "text:string" in signature


def test_a_long_enum_is_abbreviated_rather_than_pasted_whole():
    """Abbreviation is about width, not count — twelve locales blow the budget."""
    from polymas2api.toolbridge import _compact_parameters

    schema = {"type": "object", "properties": {
        "lang": {"type": "string",
                 "enum": ["zh-CN", "en-US", "ja-JP", "ko-KR", "fr-FR", "de-DE",
                          "es-ES", "ru-RU", "pt-BR", "it-IT", "nl-NL", "sv-SE"]}}}
    signature = _compact_parameters(schema)

    assert signature.endswith("…]")
    assert "sv-SE" not in signature


def test_a_short_enum_is_named_in_full_even_with_many_members():
    """Seven digits cost thirteen characters; hiding four of them was pure loss.

    The first cut counted members and cut a 13-character set down to
    `[0|1|2…]` (reported 2026-11-19).
    """
    from polymas2api.toolbridge import _compact_parameters

    schema = {"type": "object",
              "properties": {"level": {"type": "integer",
                                       "enum": [0, 1, 2, 3, 4, 5, 6]}}}
    assert _compact_parameters(schema) == "level:integer[0|1|2|3|4|5|6]"


def test_enum_members_are_rendered_as_json_literals():
    """``enum: [true, false]`` must not read ``[True|False]``.

    A model copying the Python spelling sends the *string* "True" to a boolean
    parameter — a type error for a field the signature just declared boolean.
    """
    from polymas2api.toolbridge import _enum_hint

    assert _enum_hint({"enum": [True, False]}) == "[true|false]"
    assert _enum_hint({"enum": ["auto", None]}) == "[auto|null]"
    assert _enum_hint({"enum": [1, 0]}) == "[1|0]"


def test_an_open_field_gets_no_enum_hint():
    from polymas2api.toolbridge import _compact_parameters

    assert _compact_parameters({"type": "object",
                                "properties": {"q": {"type": "string"}}}) == "q:string"


# -- tool call ids must not repeat across turns (2026-11-19) ----------------


def test_tool_call_ids_are_unique_across_parses():
    """A repeating id breaks the client's conversation tree.

    Ids used to be ``call_0001``, ``call_0002`` … starting over with every
    response. DSH keys one conversation node per callId, so a later turn's
    ``call_0001`` arrived as an update to a node whose start belonged to an
    earlier turn; the event feed threw "received an update before its start" and
    the conversation stopped rendering — the user's new message disappeared from
    both the conversation and the trajectory (2026-11-19).
    """
    from polymas2api.toolbridge import parse_tool_calls

    first = parse_tool_calls('CALL pwsh {"command": "ls"}')[1][0].id
    second = parse_tool_calls('CALL pwsh {"command": "ls"}')[1][0].id

    assert first and second
    assert first != second


def test_parallel_calls_in_one_response_get_distinct_ids():
    from polymas2api.toolbridge import parse_tool_calls

    calls = parse_tool_calls('CALL pwsh {"command": "ls"}\nCALL pwsh {"command": "date"}')[1]

    assert len(calls) == 2
    assert calls[0].id != calls[1].id


def test_a_repeated_random_draw_still_yields_a_distinct_id(monkeypatch):
    """Uniqueness must not rest on the random source behaving.

    Two ids reaching a client is what breaks its conversation tree, so a
    collision is resolved by bumping a suffix rather than by redrawing — a
    redraw loop would never end if the source ever repeated itself.
    """
    import polymas2api.toolbridge as toolbridge

    class Frozen:
        hex = "0" * 32

    monkeypatch.setattr(toolbridge.uuid, "uuid4", lambda: Frozen())
    issued = [toolbridge.new_call_id() for _ in range(4)]

    assert len(set(issued)) == 4
    assert issued[0] == "call_" + "0" * 20
    assert issued[1].endswith("x1")


def test_parsing_many_responses_never_repeats_an_id():
    from polymas2api.toolbridge import parse_tool_calls

    seen = {parse_tool_calls('CALL pwsh {"command": "ls"}')[1][0].id
            for _ in range(2000)}

    assert len(seen) == 2000
