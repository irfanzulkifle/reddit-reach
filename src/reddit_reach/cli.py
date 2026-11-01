"""Command line interface for reddit-reach.

Every subcommand prints JSON by default, because the usual caller is an agent that wants to parse
the result. Use --format text when a human is reading.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from typing import Any

from . import __version__
from .client import (
    DEFAULT_BASE,
    DEFAULT_UA,
    BlockedError,
    NotFoundError,
    RateLimitError,
    ReachError,
    RedditReach,
    summarise,
)


def _client(args: argparse.Namespace) -> RedditReach:
    return RedditReach(
        base_url=args.base_url,
        user_agent=args.user_agent,
        timeout=args.timeout,
        retries=args.retries,
    )


def _emit(args: argparse.Namespace, payload: dict[str, Any], text: str | None = None) -> None:
    if args.format == "json" or text is None:
        print(json.dumps(payload, indent=2, ensure_ascii=False))
    else:
        print(text)


def cmd_posts(args: argparse.Namespace) -> int:
    rr = _client(args)
    posts = rr.posts(args.subreddit, limit=args.limit, before=args.before, sort=args.sort)
    _emit(
        args,
        {
            "ok": True,
            "subreddit": rr._clean_subreddit(args.subreddit),
            "returned": len(posts),
            "source": "arctic-shift",
            "posts": [p.to_dict() for p in posts],
        },
        summarise(posts),
    )
    return 0


def cmd_latest(args: argparse.Namespace) -> int:
    rr = _client(args)
    post = rr.latest(args.subreddit)
    lag = rr.lag_minutes(args.subreddit)
    if post is None:
        _emit(args, {"ok": False, "reason": "archive has no posts for this subreddit"})
        return 1
    _emit(
        args,
        {"ok": True, "lag_minutes": lag, "post": post.to_dict()},
        f"{post.created_iso}  {post.title}\narchive lag: {lag} minutes",
    )
    return 0


def cmd_comments(args: argparse.Namespace) -> int:
    rr = _client(args)
    comments = rr.comments(post_id=args.post_id, subreddit=args.subreddit, limit=args.limit)
    _emit(
        args,
        {
            "ok": True,
            "returned": len(comments),
            "comments": [c.to_dict() for c in comments],
        },
        "\n\n".join(f"[{c.created_iso}] {c.author}: {c.body[:300]}" for c in comments),
    )
    return 0


def cmd_tree(args: argparse.Namespace) -> int:
    rr = _client(args)
    data = rr.tree(args.post_id, depth=args.depth, limit=args.limit)
    _emit(args, {"ok": True, "post_id": args.post_id, "tree": data})
    return 0


def cmd_check(args: argparse.Namespace) -> int:
    rr = _client(args)
    health = rr.health()
    _emit(
        args,
        health,
        f"archive reachable: {health['ok']}  latency {health['latency_ms']} ms  "
        f"newest item {health['newest_age_min']} min old",
    )
    return 0 if health["ok"] else 1


def build_parser() -> argparse.ArgumentParser:
    # `reddit-reach --format text posts X` and `reddit-reach posts X --format text` must both work.
    # The subparsers therefore carry a SUPPRESS-default copy of every shared option, so an option
    # given before the subcommand is not overwritten by the subparser's own default.
    #
    # They get their copy from `parents=[common]`, and the main parser defines its own separate
    # actions rather than reusing that parent. argparse parents share the ACTION OBJECTS, so
    # parser.set_defaults() on one would mutate the default of every subparser as well, which is
    # exactly the bug this shape avoids.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--base-url", default=argparse.SUPPRESS, help=argparse.SUPPRESS)
    common.add_argument("--user-agent", default=argparse.SUPPRESS, help=argparse.SUPPRESS)
    common.add_argument("--timeout", type=float, default=argparse.SUPPRESS, help=argparse.SUPPRESS)
    common.add_argument("--retries", type=int, default=argparse.SUPPRESS, help=argparse.SUPPRESS)
    common.add_argument(
        "--format", choices=("json", "text"), default=argparse.SUPPRESS, help=argparse.SUPPRESS
    )

    parser = argparse.ArgumentParser(
        prog="reddit-reach",
        description="Read public Reddit posts and comments from a host that Reddit blocks.",
    )
    parser.add_argument("--version", action="version", version=f"reddit-reach {__version__}")
    parser.add_argument("--base-url", default=DEFAULT_BASE, help="override the archive base URL")
    parser.add_argument(
        "--user-agent",
        default=DEFAULT_UA,
        help="sent on every request; a missing or generic value is refused with 403",
    )
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument("--retries", type=int, default=3)
    parser.add_argument(
        "--format", choices=("json", "text"), default="json", help="output format"
    )

    sub = parser.add_subparsers(dest="command", required=True)

    posts = sub.add_parser("posts", help="newest posts in a subreddit", parents=[common])
    posts.add_argument("subreddit")
    posts.add_argument("--limit", type=int, default=25)
    posts.add_argument(
        "--before", type=int, default=None, help="created_utc to walk backwards from"
    )
    posts.add_argument("--sort", choices=("desc", "asc"), default="desc")
    posts.set_defaults(func=cmd_posts)

    latest = sub.add_parser(
        "latest", help="newest archived post plus the archive lag", parents=[common]
    )
    latest.add_argument("subreddit")
    latest.set_defaults(func=cmd_latest)

    comments = sub.add_parser(
        "comments", help="comments for a post, or newest in a subreddit", parents=[common]
    )
    comments.add_argument("--post-id", default=None)
    comments.add_argument("--subreddit", default=None)
    comments.add_argument("--limit", type=int, default=100)
    comments.set_defaults(func=cmd_comments)

    tree = sub.add_parser("tree", help="nested comment tree for one post", parents=[common])
    tree.add_argument("post_id")
    tree.add_argument("--depth", type=int, default=10)
    tree.add_argument("--limit", type=int, default=100)
    tree.set_defaults(func=cmd_tree)

    check = sub.add_parser(
        "check", help="probe the archive; exit 1 when unreachable", parents=[common]
    )
    check.set_defaults(func=cmd_check)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except BlockedError as exc:
        print(json.dumps({"ok": False, "error": "blocked", "detail": str(exc)}), file=sys.stderr)
        return 2
    except RateLimitError as exc:
        payload = {"ok": False, "error": "unavailable", "detail": str(exc)}
        print(json.dumps(payload), file=sys.stderr)
        return 3
    except NotFoundError as exc:
        print(json.dumps({"ok": False, "error": "not_found", "detail": str(exc)}), file=sys.stderr)
        return 4
    except (ReachError, ValueError) as exc:
        print(json.dumps({"ok": False, "error": "error", "detail": str(exc)}), file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
