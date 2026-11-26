"""Download the vendors' tokenizer vocabularies into ``models/tokenizers``.

The gateway counts tokens exactly when these files are present and the
``tokenizers`` package is installed, and estimates with a character heuristic
when they are not (see ``polymas2api/tokencount.py``). Nothing here is required
to run the gateway — it only decides how precise ``usage`` is.

They are not committed: together they are about 30 MB, and each one belongs to
the vendor that published it.

    python tools/fetch_tokenizers.py [--dir models/tokenizers] [--only qwen]

Sources, all verified 2026-11-26:

* deepseek — ``deepseek-ai/DeepSeek-V3`` on Hugging Face (7.5 MB, 128,815 entries)
* qwen     — ``Qwen/Qwen3-8B`` on ModelScope (10.9 MB, 151,669 entries)
* doubao   — ``ByteDance-Seed/Seed-OSS-36B-Instruct`` on Hugging Face (11.3 MB,
  155,121 entries). This one *is* Doubao's own vocabulary: its chat template
  opens with "You are Doubao, a helpful AI assistant".

Hugging Face is blocked from some networks (this one included); the mirror in
``MIRRORS`` is the fallback and the default for the two HF files.
"""

from __future__ import annotations

import argparse
import pathlib
import sys
import urllib.error
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parent.parent

#: ``vendor -> (file name, [urls tried in order])``.
SOURCES = {
    "deepseek": ("deepseek.json", [
        "https://hf-mirror.com/deepseek-ai/DeepSeek-V3/resolve/main/tokenizer.json",
        "https://huggingface.co/deepseek-ai/DeepSeek-V3/resolve/main/tokenizer.json",
    ]),
    "qwen": ("qwen.json", [
        "https://modelscope.cn/models/Qwen/Qwen3-8B/resolve/master/tokenizer.json",
        "https://hf-mirror.com/Qwen/Qwen3-8B/resolve/main/tokenizer.json",
    ]),
    "doubao": ("doubao.json", [
        "https://hf-mirror.com/ByteDance-Seed/Seed-OSS-36B-Instruct/resolve/main/tokenizer.json",
        "https://huggingface.co/ByteDance-Seed/Seed-OSS-36B-Instruct/resolve/main/tokenizer.json",
    ]),
}

#: A tokenizer.json is a few megabytes of JSON; anything far below this is an
#: error page and must not be left on disk looking like a vocabulary.
MIN_BYTES = 1_000_000


def download(url: str, target: pathlib.Path) -> int:
    """Fetch one URL into ``target``; returns the number of bytes written."""
    request = urllib.request.Request(url, headers={"User-Agent": "polymas2api"})
    with urllib.request.urlopen(request, timeout=120) as response:
        data = response.read()
    if len(data) < MIN_BYTES:
        raise ValueError(f"{url} returned only {len(data)} bytes")
    target.write_bytes(data)
    return len(data)


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dir", default=str(ROOT / "models" / "tokenizers"))
    parser.add_argument("--only", action="append", choices=sorted(SOURCES),
                       help="fetch just this vendor (repeatable)")
    parser.add_argument("--force", action="store_true",
                       help="re-download even when the file is already there")
    args = parser.parse_args(argv[1:])

    directory = pathlib.Path(args.dir)
    directory.mkdir(parents=True, exist_ok=True)
    wanted = args.only or sorted(SOURCES)
    failed = 0
    for vendor in wanted:
        name, urls = SOURCES[vendor]
        target = directory / name
        if target.is_file() and not args.force:
            print(f"  {vendor:<9} already there  {target.stat().st_size / 1e6:.1f} MB")
            continue
        for url in urls:
            try:
                size = download(url, target)
            except (urllib.error.URLError, ValueError, OSError) as error:
                print(f"  {vendor:<9} {url} failed: {error}")
                continue
            print(f"  {vendor:<9} {size / 1e6:.1f} MB  <- {url}")
            break
        else:
            failed += 1
            print(f"  {vendor:<9} SKIPPED (no source answered)")
    if failed:
        print(f"\n  {failed} vocabulary/vocabularies missing — the gateway will "
              f"estimate for those models instead of counting.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
