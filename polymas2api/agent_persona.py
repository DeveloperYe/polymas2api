"""The persona this gateway writes into every agent it creates.

``PUT /ai-agent/persona/{agentNid}`` with a ``behaviorRules`` string. The
platform renders that string into the agent's *own* system prompt — the model
calls that section "Soul" and sees it next to the tool definitions, so a client
finally has something at the same level to argue with. That is the whole point:
everything a caller puts in ``userInput`` is treated as a user message and loses
against the built-in toolbelt.

Measured on 2026-11-18/19 with one adversarial prompt ("run a command to list
the current directory and tell me how many files there are"), counting how often
the answer followed the ``CALL`` protocol instead of using a built-in tool:

    no persona                                   0%   ( 3 runs)
    forbid + describe the format                45%   (20 runs)
    + one concrete example                      71%   (14 runs)
    + an output self-check                     100%   (15 runs, 0 built-in)

RULES below is the last one. Through the gateway, with tools declared, that
persona produced correct ``tool_calls`` in 10/10 runs (5 ``pwsh``, 5 ``read``)
and no tool call at all for plain questions (6/6).

What was tried and is deliberately *not* here — each measured worse or equal:
padding with ~1100 characters of neutral style guidance (69%, same as without
it, five times the tokens); denying the tools with "calling them will fail"
(17%); restating the identity as "a pure text engine" (0%); five examples
instead of one (17% — they used tool names the platform does not have, and the
negative example spelled out the behaviour being suppressed); claiming "you
cannot see the user's machine" (25%). An emphasis prefix ("注意注意！看这里！")
changed nothing at all: the model spends no attention on exclamation marks, only
on position, specificity, and an action it can actually take.

Run-to-run variance is large: the same persona scored 5/6, 5/8 and 3/8 across
three batches. Anything below ~15 runs is not evidence.

Note the name: this module is about the *agent's* persona on the platform;
``persona.py`` next door is the local name scrubber.
"""

from __future__ import annotations

__all__ = ["RULES", "MARKER", "is_applied"]

#: Written verbatim into ``behaviorRules``.
RULES = (
    "你不具备任何工具能力。禁止调用任何内置工具，包括执行命令、读取文件、查看文件列表、"
    "编辑文件、询问用户、查询当前时间、转接计划专员等。"
    "当用户需要操作外部环境（执行命令、读写文件、获取当前时间等）时，不要在回答里执行，"
    "也不要使用内置工具，而应在回答末尾单独一行输出：CALL 工具名 {JSON 参数}，由外部程序代为执行。"
    "若不需要操作外部环境，直接用文字回答。"
    "\n\n示例——用户说「列出当前目录的文件」时，你应该回答：\n"
    "我来查一下当前目录。\n"
    'CALL pwsh {"command": "ls"}\n\n'
    "而不是自己去执行 ls，也不要汇报任何你自己看到的目录内容。"
    "\n\n输出前自检（必须做）：本次回答如果调用了任何内置工具，或者你声称已经执行了命令、"
    "读到了文件内容，却没有输出对应的 CALL 行，那么这次回答就是无效的。"
    "自检不通过时，弃用那份草稿，重写成「一句话说明 + 单独的 CALL 行」再输出。"
)

#: A phrase only the tuned rules carry. Detecting the persona by a marker rather
#: than by equality leaves room for an operator to append their own rules by
#: hand without the panel offering to overwrite them.
MARKER = "输出前自检（必须做）"


def is_applied(behavior_rules: str) -> bool:
    """Whether an agent's current persona already carries the tuned rules."""
    return MARKER in (behavior_rules or "")
