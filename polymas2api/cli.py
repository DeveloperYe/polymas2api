"""Command line entry point: ``polymas2api --config config.json``."""

from __future__ import annotations

import argparse
import json
import sys

from .config import ConfigError, load_config, write_example
from .server import Logger, serve

__all__ = ["main"]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="polymas2api",
        description="Expose the polymas (智慧树) AI assistant as an OpenAI-compatible API.",
    )
    parser.add_argument("-c", "--config", default="config.json",
                        help="path to the JSON config file (default: config.json)")
    parser.add_argument("--host", help="override the bind address")
    parser.add_argument("--port", type=int, help="override the bind port")
    parser.add_argument("--model", help="override the model sent to the platform")
    parser.add_argument("--print-example", metavar="PATH",
                        help="write a starter config file and exit")
    parser.add_argument("--check", action="store_true",
                        help="validate the config and the platform credentials, then exit")
    return parser


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.print_example:
        write_example(args.print_example)
        print(f"wrote {args.print_example}")
        return 0

    try:
        cfg = load_config(args.config)
    except ConfigError as error:
        parser.error(str(error))
        return 2

    if args.host:
        cfg.host = args.host
    if args.port:
        cfg.port = args.port
    if args.model:
        cfg.model = args.model
    if not cfg.app_code:
        cfg.app_code = cfg.friend_nid

    log = Logger(cfg.path("log_dir", "logs"), console=cfg.log_console)

    if args.check:
        # Credentials live in accounts.json now, not in config.json, so this
        # has to read the pool: checking ``cfg.missing()`` would always report
        # the config as incomplete on a correctly set-up install.
        from .pool import CredentialPool
        from .upstream import PolymasClient, UpstreamError

        pool = CredentialPool(cfg, logger=log)
        pool.load_accounts()
        if not pool.list():
            pool.load_from_config()          # legacy configs still work
        enabled = [item for item in pool.list() if item.get("enabled")]
        if not enabled:
            log("no account configured — add one from the panel "
                "(「＋ 添加账号」) or put one in accounts.json")
            return 1
        account = pool.get(enabled[0]["uid"])
        client = PolymasClient(cfg, account, logger=log)
        try:
            catalogue = client.models()["models"]
        except UpstreamError as error:
            log(f"credential check failed: {error}")
            return 1
        log("credential check passed; the assistant exposes:")
        for item in catalogue:
            log(f"  - {item['modelCode']} (alias={item.get('alias')}, default={item.get('default')})")
        return 0

    serve(cfg, log=log)
    return 0


if __name__ == "__main__":
    sys.exit(main())
