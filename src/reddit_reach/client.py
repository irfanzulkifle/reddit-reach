"""Client for the Arctic Shift Reddit archive.

Why this exists: reddit.com, api.reddit.com and oauth.reddit.com answer 403 to datacenter
IP ranges, and Cloudflare WARP egress is blocked the same way. The Arctic Shift project keeps a
public archive of Reddit posts and comments and serves it over a plain HTTP API with no account,
no key and no cookie. This module gives that API a small, typed, dependency-free client.

Known limits, all measured on 2026-11-01 from a cloud VPS whose IP Reddit blocks:

* The archive is not live. Ingest lag measured at 17 to 121 minutes depending on the subreddit.
* ``score`` is frozen at ingest time, so it is not a ranking signal.
* One request returns at most 100 rows, and ``limit`` above 100 returns ZERO rows rather than an
  error. MAX_LIMIT is enforced here for that reason.
* Pagination is manual: pass the oldest ``created_utc`` you have as ``before`` to walk backwards.
* A request without a User-Agent header is refused with 403. That looks exactly like the IP block
  this library exists to avoid, so the header is always sent and the 403 handler says so.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

DEFAULT_BASE = "https://arctic-shift.photon-reddit.com"
DEFAULT_UA = "reddit-reach/0.1 (+https://github.com/irfanzulkifle/reddit-reach)"
MAX_LIMIT = 100

Transport = Callable[[str, Mapping[str, str], float], tuple[int, bytes]]


class ReachError(Exception):
    """Base error for every failure this library reports."""


class BlockedError(ReachError):
    """403 from the archive. Almost always a missing or generic User-Agent header."""


class RateLimitError(ReachError):
    """429 or 503 after the retries were exhausted."""


class NotFoundError(ReachError):
    """404 on a post or comment lookup."""


def _utc_iso(ts: float | int | None) -> str | None:
    if ts is None:
        return None
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(float(ts)))


@dataclass(frozen=True)
class Post:
    """One submission, with only the fields that are useful for research work."""

    id: str
    title: str
    author: str | None
    subreddit: str
    created_utc: int
    score: int
    num_comments: int
    permalink: str
    selftext: str = ""
    url: str | None = None
    link_flair: str | None = None
    over_18: bool = False

    @classmethod
    def from_api(cls, raw: Mapping[str, Any]) -> Post:
        return cls(
            id=str(raw.get("id") or ""),
            title=(raw.get("title") or "").strip(),
            author=raw.get("author"),
            subreddit=raw.get("subreddit") or "",
            created_utc=int(raw.get("created_utc") or 0),
            score=int(raw.get("score") or 0),
            num_comments=int(raw.get("num_comments") or 0),
            permalink=raw.get("permalink") or "",
            selftext=raw.get("selftext") or "",
            url=raw.get("url"),
            link_flair=raw.get("link_flair_text"),
            over_18=bool(raw.get("over_18")),
        )

    @property
    def created_iso(self) -> str:
        return _utc_iso(self.created_utc) or ""

    @property
    def reddit_url(self) -> str:
        return f"https://www.reddit.com{self.permalink}" if self.permalink else ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "author": self.author,
            "subreddit": self.subreddit,
            "created_utc": self.created_utc,
            "created_iso": self.created_iso,
            "score": self.score,
            "num_comments": self.num_comments,
            "permalink": self.permalink,
            "url": self.url,
            "link_flair": self.link_flair,
            "over_18": self.over_18,
            "selftext": self.selftext,
        }


@dataclass(frozen=True)
class Comment:
    """One comment body."""

    id: str
    author: str | None
    body: str
    created_utc: int
    score: int
    parent_id: str | None = None
    post_id: str | None = None
    permalink: str | None = None

    @classmethod
    def from_api(cls, raw: Mapping[str, Any]) -> Comment:
        return cls(
            id=str(raw.get("id") or ""),
            author=raw.get("author"),
            body=raw.get("body") or "",
            created_utc=int(raw.get("created_utc") or 0),
            score=int(raw.get("score") or 0),
            parent_id=raw.get("parent_id"),
            post_id=raw.get("link_id") or raw.get("post_id"),
            permalink=raw.get("permalink"),
        )

    @property
    def created_iso(self) -> str:
        return _utc_iso(self.created_utc) or ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "author": self.author,
            "body": self.body,
            "created_utc": self.created_utc,
            "created_iso": self.created_iso,
            "score": self.score,
            "parent_id": self.parent_id,
            "post_id": self.post_id,
        }


def _default_transport(url: str, headers: Mapping[str, str], timeout: float) -> tuple[int, bytes]:
    req = urllib.request.Request(url, headers=dict(headers))
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 - fixed https base
            return int(resp.status), resp.read()
    except urllib.error.HTTPError as exc:  # 4xx/5xx still carry a body and a status
        return int(exc.code), exc.read() or b""


class RedditReach:
    """Read posts and comments from the Reddit archive.

    >>> rr = RedditReach()
    >>> [p.title for p in rr.posts("LocalLLaMA", limit=3)]
    """

    def __init__(
        self,
        base_url: str = DEFAULT_BASE,
        user_agent: str = DEFAULT_UA,
        timeout: float = 30.0,
        retries: int = 3,
        backoff: float = 1.5,
        transport: Transport | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.user_agent = user_agent
        self.timeout = timeout
        self.retries = max(1, retries)
        self.backoff = backoff
        self._transport = transport or _default_transport

    # ---------------------------------------------------------------- transport

    def _get(self, path: str, params: Mapping[str, Any]) -> Any:
        clean = {k: v for k, v in params.items() if v is not None and v != ""}
        url = f"{self.base_url}{path}?{urllib.parse.urlencode(clean)}"
        headers = {"User-Agent": self.user_agent, "Accept": "application/json"}

        last_error: str = ""
        for attempt in range(self.retries):
            status, body = self._transport(url, headers, self.timeout)
            if status == 200:
                try:
                    return json.loads(body.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                    raise ReachError(
                        f"archive returned 200 with {len(body)} bytes that are not JSON: {exc}"
                    ) from exc
            if status == 403:
                raise BlockedError(
                    "archive refused the request with 403. If this is a fresh install, the "
                    "User-Agent header is missing or too generic; set user_agent='name/version "
                    "(contact)'. If the header is set, the archive itself is blocking this host."
                )
            if status == 404:
                raise NotFoundError(f"404 for {url}")
            if status in (429, 500, 502, 503, 504):
                last_error = f"HTTP {status}"
                if attempt + 1 < self.retries:
                    time.sleep(self.backoff ** attempt)
                    continue
                raise RateLimitError(
                    f"archive unavailable after {self.retries} attempts: {last_error}"
                )

            snippet = body[:200].decode("utf-8", "replace")
            raise ReachError(f"unexpected HTTP {status} from {url}: {snippet}")

        raise RateLimitError(f"archive unavailable after {self.retries} attempts: {last_error}")

    @staticmethod
    def _clean_subreddit(subreddit: str) -> str:
        name = subreddit.strip()
        for prefix in ("https://www.reddit.com/r/", "https://reddit.com/r/", "/r/", "r/"):
            if name.startswith(prefix):
                name = name[len(prefix) :]
        return name.strip("/")

    @staticmethod
    def _check_limit(limit: int) -> int:
        if not isinstance(limit, int) or limit < 1:
            raise ValueError("limit must be a positive integer")
        if limit > MAX_LIMIT:
            raise ValueError(
                f"limit must be <= {MAX_LIMIT}; the archive silently returns zero rows above that "
                "instead of an error, so this library refuses rather than mislead you"
            )
        return limit

    # -------------------------------------------------------------------- reads

    def posts(
        self,
        subreddit: str,
        limit: int = 25,
        before: int | None = None,
        after: int | None = None,
        sort: str = "desc",
    ) -> list[Post]:
        """Return up to ``limit`` posts, newest first by default.

        ``before`` walks backwards from a ``created_utc`` you already have; ``after`` walks
        forwards. Both are inclusive-adjacent, so paginate with the last timestamp you saw.
        """
        self._check_limit(limit)
        if sort not in ("asc", "desc"):
            raise ValueError("sort must be 'asc' or 'desc'")
        payload = self._get(
            "/api/posts/search",
            {
                "subreddit": self._clean_subreddit(subreddit),
                "limit": limit,
                "sort": sort,
                "before": before,
                "after": after,
            },
        )
        return [Post.from_api(row) for row in (payload.get("data") or [])]

    def iter_posts(
        self, subreddit: str, total: int = 200, page_size: int = MAX_LIMIT
    ) -> Iterator[Post]:
        """Yield ``total`` posts by walking backwards, de-duplicating by id."""
        self._check_limit(page_size)
        seen: set[str] = set()
        cursor: int | None = None
        fetched = 0
        while fetched < total:
            want = min(page_size, total - fetched)
            page = self.posts(subreddit, limit=want, before=cursor)
            new = [p for p in page if p.id not in seen]
            if not new:
                return
            for post in new:
                seen.add(post.id)
                fetched += 1
                yield post
                if fetched >= total:
                    return
            cursor = min(p.created_utc for p in new)

    def latest(self, subreddit: str) -> Post | None:
        """Return the newest archived post, or None when the archive has nothing."""
        page = self.posts(subreddit, limit=1, sort="desc")
        return page[0] if page else None

    def lag_minutes(self, subreddit: str) -> float | None:
        """How many minutes behind live the archive is for this subreddit right now."""
        newest = self.latest(subreddit)
        if newest is None:
            return None
        return round((time.time() - newest.created_utc) / 60, 1)

    def comments(
        self,
        post_id: str | None = None,
        subreddit: str | None = None,
        limit: int = 100,
        before: int | None = None,
        after: int | None = None,
        sort: str = "desc",
    ) -> list[Comment]:
        """Return comments for one post, or the newest comments in a subreddit."""
        self._check_limit(limit)
        if not post_id and not subreddit:
            raise ValueError("pass post_id or subreddit")
        payload = self._get(
            "/api/comments/search",
            {
                "link_id": f"t3_{post_id}" if post_id else None,
                "subreddit": self._clean_subreddit(subreddit) if subreddit else None,
                "limit": limit,
                "sort": sort,
                "before": before,
                "after": after,
            },
        )
        return [Comment.from_api(row) for row in (payload.get("data") or [])]

    def tree(self, post_id: str, depth: int = 10, limit: int = MAX_LIMIT) -> Any:
        """Return the nested comment tree for a post, as the archive provides it."""
        self._check_limit(limit)
        return self._get(
            "/api/comments/tree",
            {"link_id": f"t3_{post_id}", "depth": depth, "limit": limit},
        ).get("data")

    def health(self) -> dict[str, Any]:
        """Probe the archive and report reachability in a form a cron job can log."""
        started = time.time()
        payload = self._get("/api/posts/search", {"subreddit": "LocalLLaMA", "limit": 1})
        rows = payload.get("data") or []
        return {
            "ok": bool(rows),
            "latency_ms": int((time.time() - started) * 1000),
            "rows": len(rows),
            "newest_age_min": (
                round((time.time() - int(rows[0]["created_utc"])) / 60, 1) if rows else None
            ),
            "base_url": self.base_url,
        }


def summarise(posts: Sequence[Post], width: int = 80) -> str:
    """Render posts as a fixed-width table, for humans rather than agents."""
    lines = [f"{'when (UTC)':<17} {'score':>5} {'comments':>8}  title"]
    for post in posts:
        title = post.title if len(post.title) <= width else post.title[: width - 1] + "…"
        lines.append(
            f"{post.created_iso:<17} {post.score:>5} {post.num_comments:>8}  {title}"
        )
    return "\n".join(lines)
