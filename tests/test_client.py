"""Tests for reddit_reach.client. No network: every test injects a fake transport."""

from __future__ import annotations

import json
import time
import urllib.parse

import pytest

from reddit_reach import MAX_LIMIT, BlockedError, RateLimitError, ReachError, RedditReach
from reddit_reach.client import Comment, Post, summarise


def post_row(pid: str, ts: int, title: str = "hello") -> dict:
    return {
        "id": pid,
        "title": title,
        "author": "someone",
        "subreddit": "LocalLLaMA",
        "created_utc": ts,
        "score": 5,
        "num_comments": 2,
        "permalink": f"/r/LocalLLaMA/comments/{pid}/x/",
        "selftext": "body text",
    }


class FakeTransport:
    """Records every request and answers from a callback."""

    def __init__(self, handler):
        self.handler = handler
        self.calls: list[str] = []
        self.headers: list[dict] = []

    def __call__(self, url, headers, timeout):
        self.calls.append(url)
        self.headers.append(dict(headers))
        return self.handler(url)


def ok(rows):
    return 200, json.dumps({"data": rows}).encode()


def test_posts_sends_expected_query_and_parses_rows():
    t = FakeTransport(lambda url: ok([post_row("a1", 1_700_000_000)]))

    rr = RedditReach(transport=t)
    posts = rr.posts("r/LocalLLaMA", limit=10)

    assert len(posts) == 1
    assert posts[0].id == "a1"
    assert posts[0].reddit_url == "https://www.reddit.com/r/LocalLLaMA/comments/a1/x/"
    query = urllib.parse.parse_qs(urllib.parse.urlparse(t.calls[0]).query)
    assert query["subreddit"] == ["LocalLLaMA"]  # the r/ prefix is stripped
    assert query["limit"] == ["10"]
    assert query["sort"] == ["desc"]


def test_user_agent_is_always_sent():
    t = FakeTransport(lambda url: ok([]))
    RedditReach(transport=t, user_agent="my-bot/1.2 (me@example.com)").posts("test", limit=1)
    assert t.headers[0]["User-Agent"] == "my-bot/1.2 (me@example.com)"


def test_limit_above_100_is_refused_because_the_archive_returns_zero_rows():
    rr = RedditReach(transport=FakeTransport(lambda url: ok([])))
    with pytest.raises(ValueError, match="silently returns zero rows"):
        rr.posts("test", limit=MAX_LIMIT + 1)


@pytest.mark.parametrize("bad", [0, -1])
def test_limit_must_be_positive(bad):
    rr = RedditReach(transport=FakeTransport(lambda url: ok([])))
    with pytest.raises(ValueError):
        rr.posts("test", limit=bad)


def test_403_is_reported_as_a_user_agent_problem_not_a_network_problem():
    rr = RedditReach(transport=FakeTransport(lambda url: (403, b"blocked")))
    with pytest.raises(BlockedError, match="User-Agent"):
        rr.posts("test", limit=1)


def test_5xx_is_retried_then_raised():
    calls = []

    def handler(url):
        calls.append(url)
        return 503, b"nope"

    rr = RedditReach(transport=FakeTransport(handler), retries=2, backoff=0)
    with pytest.raises(RateLimitError):
        rr.posts("test", limit=1)
    assert len(calls) == 2


def test_429_then_success_is_transparent():
    responses = [(429, b""), (200, json.dumps({"data": [post_row("z", 5)]}).encode())]

    rr = RedditReach(transport=FakeTransport(lambda url: responses.pop(0)), retries=3, backoff=0)
    assert [p.id for p in rr.posts("test", limit=1)] == ["z"]


def test_200_with_html_instead_of_json_is_an_error_not_empty_data():
    rr = RedditReach(transport=FakeTransport(lambda url: (200, b"<html>challenge</html>")))
    with pytest.raises(ReachError, match="not JSON"):
        rr.posts("test", limit=1)


def test_pagination_walks_backwards_and_dedupes():
    pages = {
        None: [post_row("p1", 100), post_row("p2", 90), post_row("p3", 80)],
        80: [post_row("p3", 80), post_row("p4", 70), post_row("p5", 60)],
    }

    def handler(url):
        before = urllib.parse.parse_qs(urllib.parse.urlparse(url).query).get("before", [None])[0]
        key = int(before) if before is not None else None
        return ok(pages.get(key, []))

    rr = RedditReach(transport=FakeTransport(handler))
    ids = [p.id for p in rr.iter_posts("test", total=5, page_size=3)]
    assert ids == ["p1", "p2", "p3", "p4", "p5"]


def test_latest_and_lag_minutes():
    ts = int(time.time()) - 600
    rr = RedditReach(transport=FakeTransport(lambda url: ok([post_row("new", ts)])))
    newest = rr.latest("test")
    assert newest is not None and newest.id == "new"
    lag = rr.lag_minutes("test")
    assert lag is not None and 9.0 <= lag <= 12.0


def test_comments_require_a_target():
    rr = RedditReach(transport=FakeTransport(lambda url: ok([])))
    with pytest.raises(ValueError, match="post_id or subreddit"):
        rr.comments()


def test_comments_parse_bodies():
    row = {
        "id": "c1",
        "author": "a",
        "body": "hello",
        "created_utc": 10,
        "score": 1,
        "link_id": "t3_x",
    }
    rr = RedditReach(transport=FakeTransport(lambda url: ok([row])))
    comments = rr.comments(post_id="x")
    assert isinstance(comments[0], Comment)
    assert comments[0].body == "hello"


def test_tree_returns_nested_data():
    body = json.dumps({"data": [{"id": "t"}]}).encode()
    rr = RedditReach(transport=FakeTransport(lambda url: (200, body)))
    assert rr.tree("x") == [{"id": "t"}]


def test_health_reports_reachability():
    rr = RedditReach(transport=FakeTransport(lambda url: ok([post_row("h", int(time.time()))])))
    health = rr.health()
    assert health["ok"] is True
    assert health["rows"] == 1
    assert health["newest_age_min"] is not None


def test_post_to_dict_is_json_safe():
    p = Post.from_api(post_row("a", 1_700_000_000))
    json.dumps(p.to_dict())


def test_summarise_renders_one_line_per_post():
    rows = [post_row("a", 1_700_000_000, "short title")]
    text = summarise([Post.from_api(r) for r in rows])
    assert text.count("\n") == 1
    assert "short title" in text
