"""Compare our character-count token estimate with DeepSeek's real tokenizer.

Why this matters: the platform reports no usage at all, so ``usage.prompt_tokens``
is our estimate (``polymas2api/tokens.py``). If a client computes "how full is
the context window" from that number, any bias in the estimate moves the
compaction threshold. This measures the bias.

Method
------
``tokenizer.json`` is DeepSeek's published BPE vocabulary (128,000 entries,
127,741 merges). Python 3.14 has no wheels for ``tokenizers``, ``tiktoken`` or
``regex`` on this machine, so the tokenizer is reproduced here:

* pre-tokenizer split 0: ``\\p{N}{1,3}`` (digits, approximated as ASCII +
  full-width digits)
* pre-tokenizer split 1: CJK runs ``[一-龥぀-ゟ゠-ヿ]+`` — reproduced exactly
* pre-tokenizer split 2: the GPT-2-style Latin/punctuation/space split — only
  approximated (``\\p{L}``/``\\p{M}``/``\\p{P}``/``\\p{S}`` need the ``regex``
  module). Because split 1 already isolates CJK runs, **Chinese samples are
  close to exact** while Latin/code samples may drift a little.
* ByteLevel mapping, then greedy BPE merges in the published rank order.

    python tools/probe_tokenizer.py [path-to-tokenizer.json]
"""

from __future__ import annotations

import json
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from polymas2api.tokens import estimate_tokens      # noqa: E402


# -- GPT-2 byte-level mapping -------------------------------------------------
def bytes_to_unicode() -> dict[int, str]:
    printable = (list(range(33, 127)) + list(range(161, 173)) + list(range(174, 256)))
    mapped = printable[:]
    extra = 0
    for byte in range(256):
        if byte not in printable:
            printable.append(byte)
            mapped.append(256 + extra)
            extra += 1
    return dict(zip(printable, (chr(point) for point in mapped)))


BYTE_ENCODER = bytes_to_unicode()

# Split 0: digits. \p{N} approximated by ASCII + full-width digits.
_DIGIT_RE = re.compile(r"[0-9０-９]{1,3}")
# Split 1: CJK runs, exactly as in the published pre-tokenizer.
_CJK_RE = re.compile(r"[一-龥぀-ゟ゠-ヿ]+")
# Split 2: the GPT-2 pattern (contractions, optional leading space, words,
# numbers, punctuation, trailing whitespace). DeepSeek's published pattern is a
# Unicode-class variant of this; ``\p{L}``/``\p{P}`` need the ``regex`` module,
# which has no wheel for Python 3.14 here, so the ASCII form is used. Keeping a
# leading space attached to its word matters: without it the merges for
# " the" are lost and English token counts inflate by ~40%.
_WORD_RE = re.compile(
    r"'s|'t|'re|'ve|'m|'ll|'d"
    r"| ?[A-Za-z\u00c0-\u024f]+"
    r"| ?[0-9]+"
    r"| ?[^\sA-Za-z0-9\u00c0-\u024f]+"
    r"|\s+(?!\S)|\s+")


def split_keep(text: str, pattern: re.Pattern[str]) -> list[str]:
    out: list[str] = []
    last = 0
    for match in pattern.finditer(text):
        if match.start() > last:
            out.append(text[last:match.start()])
        out.append(match.group(0))
        last = match.end()
    if last < len(text):
        out.append(text[last:])
    return [piece for piece in out if piece]


def pre_tokenize(text: str) -> list[str]:
    pieces = [text]
    for pattern in (_DIGIT_RE, _CJK_RE, _WORD_RE):
        nxt: list[str] = []
        for piece in pieces:
            nxt.extend(split_keep(piece, pattern))
        pieces = nxt
    return pieces


class Tokenizer:
    def __init__(self, path: pathlib.Path):
        data = json.loads(path.read_text(encoding="utf-8"))
        model = data["model"]
        self.vocab: dict[str, int] = model["vocab"]
        merges = model["merges"]
        # Newer files store merges as "a b"; older ones as ["a", "b"].
        pairs = []
        for entry in merges:
            if isinstance(entry, str):
                left, _, right = entry.partition(" ")
                pairs.append((left, right))
            else:
                pairs.append((entry[0], entry[1]))
        self.ranks = {pair: index for index, pair in enumerate(pairs)}

    def _bpe(self, token: str) -> list[str]:
        if not token:
            return []
        word = list(token)
        while len(word) > 1:
            best_rank, best_index = None, None
            for i in range(len(word) - 1):
                rank = self.ranks.get((word[i], word[i + 1]))
                if rank is not None and (best_rank is None or rank < best_rank):
                    best_rank, best_index = rank, i
            if best_index is None:
                break
            word[best_index:best_index + 2] = [word[best_index] + word[best_index + 1]]
        return word

    def count(self, text: str) -> int:
        total = 0
        for piece in pre_tokenize(text):
            # ByteLevel: each byte becomes one printable character first.
            encoded = "".join(BYTE_ENCODER[byte] for byte in piece.encode("utf-8"))
            total += len(self._bpe(encoded))
        return total


SAMPLES = {
    "中文散文": (
        "春天的风是软的，带着一点潮湿的土腥味从窗缝里钻进来。院子里的桃树先是鼓起花苞，"
        "然后某个早晨忽然全开了，粉白的一片压在枝头，像谁把云撕碎了撒下来。"
        "老张照例搬了把竹椅坐在树下，眯着眼晒太阳，手里的收音机咿咿呀呀地唱着戏。"
        "他说这棵树是他父亲种的，那年他还没出生。"
    ),
    "中文技术说明": (
        "网关在收到请求后，先把 messages 数组拼成一段纯文本，再放进 userInput 字段发给上游。"
        "上游只接受单个字符串，所以多轮对话的历史必须由客户端自己拼接。"
        "流式响应使用 SSE，每帧包含事件名与数据，客户端需要按事件名分发。"
        "如果上游返回错误，网关会把它转换成 OpenAI 兼容的错误对象。"
    ),
    "英文散文": (
        "The wind in early spring is soft, carrying a damp smell of turned soil through the "
        "gap under the window. The peach tree in the yard swells its buds first, and then one "
        "morning it simply opens all at once, a pink-white weight pressing on the branches as "
        "if someone had torn a cloud apart and scattered it."
    ),
    "Python 代码": (
        "def estimate_tokens(text: str) -> int:\n"
        "    if not text:\n"
        "        return 0\n"
        "    cjk = len(_CJK_RE.findall(text))\n"
        "    other = max(0, len(text) - cjk)\n"
        "    return int(round(cjk + other / 4.0))\n"
    ),
    "JSON": (
        '{"model":"deepseek-v4.1-flash","messages":[{"role":"user",'
        '"content":"把第 3 批最后一条的编号告诉我。"}],"stream":true,'
        '"temperature":0.7,"top_p":0.9}'
    ),
    "混合（真实对话）": (
        "用户：帮我看看 polymas2api/server.py 里 MAX_BODY_BYTES 是多少？\n"
        "助手：是 1048576（1 MB）。在 server.py 第 56 行定义。\n"
        "用户：那如果我传 300000 个中文字符会怎样？\n"
        "助手：会返回 400，错误信息是 request body exceeds 1048576 bytes。\n"
        "用户：有没有办法绕过？\n"
        "助手：只能用 ensure_ascii=False 降低体积，或者改这个常量。"
    ),
}


def main(argv: list[str]) -> int:
    path = pathlib.Path(argv[1]) if len(argv) > 1 else \
        pathlib.Path.home() / "AppData/Local/Temp/tokprobe/tokenizer.json"
    if not path.exists():
        print(f"找不到 tokenizer.json: {path}\n先下载：\n"
              "  curl -sL -o %TEMP%/tokprobe/tokenizer.json "
              "https://hf-mirror.com/deepseek-ai/DeepSeek-V3/resolve/main/tokenizer.json")
        return 2

    tokenizer = Tokenizer(path)
    print(f"词表 {len(tokenizer.vocab)} 条，merges {len(tokenizer.ranks)} 条\n")
    print(f"{'样本':<14}{'字符':>7}{'真实 BPE':>9}{'我们的估算':>11}{'偏差':>9}  每 token 字符数")
    print("-" * 78)
    ratios = []
    for label, text in SAMPLES.items():
        real = tokenizer.count(text)
        ours = estimate_tokens(text)
        bias = (ours / real - 1) * 100 if real else 0
        ratios.append((label, len(text), real, ours, bias))
        print(f"{label:<14}{len(text):>7}{real:>9}{ours:>11}{bias:>8.1f}%  {len(text) / real:>8.2f}")
    print()
    chinese = [r for r in ratios if "中文" in r[0]]
    if chinese:
        avg = sum(r[4] for r in chinese) / len(chinese)
        per_token = sum(r[1] for r in chinese) / sum(r[2] for r in chinese)
        print(f"中文样本平均：估算偏高 {avg:.1f}%，实际 {per_token:.2f} 字符/token")
    print()
    print("含义：如果客户端用 prompt_tokens ÷ 窗口大小 算使用率，")
    print("      我们的估算偏高就意味着使用率被放大（见上表的偏差列）。")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
