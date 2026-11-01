# reddit-reach

[![ci](https://github.com/irfanzulkifle/reddit-reach/actions/workflows/ci.yml/badge.svg)](https://github.com/irfanzulkifle/reddit-reach/actions/workflows/ci.yml)
[![license: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![python](https://img.shields.io/badge/python-3.10%2B-blue.svg)](https://www.python.org/)

Read public Reddit posts and comments from a host that Reddit blocks.

```bash
reddit-reach posts LocalLLaMA --limit 5
reddit-reach latest LocalLLaMA          # newest archived post + how far behind it is
reddit-reach comments --post-id 1xktat3 --limit 50
reddit-reach check                      # exit 1 if the archive is unreachable
```

No account. No API key. No cookie. No browser. No proxy. Standard library only.

## The problem this solves

Reddit answers `403` to datacenter IP ranges, and it does so at the edge, before any token is
validated. Measured on 2026-11-01 from an ordinary cloud VPS in Singapore:

| Attempt | Result |
|---|---|
| `www.reddit.com/r/MachineLearning/.json` | 403, 190,240-byte block page |
| `api.reddit.com/r/MachineLearning/new` | 403, 190,240-byte block page |
| `oauth.reddit.com/r/MachineLearning/new` (no token) | 403 |
| `oauth.reddit.com` with an invalid bearer | 1,522-byte "Blocked" page, not a JSON 401 |
| `old.reddit.com/r/MachineLearning/` | 302 to login, then 403 |
| real headless Chromium | "blocked by network security" |
| Cloudflare WARP egress (a shared Cloudflare address) | still 403 |
| `translate.goog` proxy | still 403 |
| 20 public Redlib instances | 3 answered `200` with an Anubis challenge page, the rest failed |
| Teddit public instances | DNS gone |
| PullPush.io | 522 three times |
| `r.jina.ai` reader | 493-byte stub |
| RSSHub public instance | 403 / 503 |

The 200-with-an-empty-body and 200-with-a-challenge-page cases matter: status codes lie here, so
check the bytes, not the code.

## What works instead

[Arctic Shift](https://arctic-shift.photon-reddit.com) keeps a public archive of Reddit posts and
comments and serves it over plain HTTP with no authentication. This library wraps it.

| Endpoint | Result from the same blocked host |
|---|---|
| `/api/posts/search?subreddit=MachineLearning&limit=5` | 200, 14,187 B, real JSON with `selftext` |
| `/api/comments/search?subreddit=MachineLearning&limit=5` | 200, 13,395 B with comment bodies |
| `/api/comments/tree?link_id=t3_…` | 200, nested replies to depth 10 |
| archive coverage | back to 2009 |

## What it cannot do

Read this before you design anything on top of it.

- **It is not live.** Measured ingest lag: `r/LocalLLaMA` 17 min, `r/selfhosted` 29 min,
  `r/MachineLearning` 121 min. Busy subreddits trail further behind.
- **`score` is frozen at ingest.** It is the score when the archive saw the post, not now. Never
  rank by it.
- **No trending, no rising, no cross-subreddit search.** You get time-ordered posts per subreddit.
- **At most 100 rows per request.** `limit=500` returns **zero rows**, not an error. This library
  raises `ValueError` above 100 so you never get a silent empty result.
- **Pagination is manual.** Pass the oldest `created_utc` you have as `before` to walk backwards.
  `iter_posts()` does this for you.
- **Needs a real User-Agent.** A plain `urllib` request with no `User-Agent` is refused with 403.
  That failure looks exactly like the IP block this library exists to avoid. The header is always
  sent; if you see `BlockedError`, check your user agent first.
- **One volunteer-run archive.** No uptime guarantee, no SLA, no support. Cache what you fetch.
- **The newest archived row can be `[ Removed by moderator ]`.** Observed on 2026-11-01 in
  r/selfhosted. Skip moderation placeholders when summarising rather than reporting them as posts.

## Install

```bash
pip install git+https://github.com/irfanzulkifle/reddit-reach.git
# or, from a clone
pip install -e .
```

Python 3.10+. No runtime dependencies.

## Library

```python
from reddit_reach import RedditReach

rr = RedditReach(user_agent="my-agent/1.0 (you@example.com)")

for post in rr.posts("LocalLLaMA", limit=10):
    print(post.created_iso, post.score, post.title)

# walk backwards past the 100-row page limit
for post in rr.iter_posts("selfhosted", total=250):
    ...

# comments for a thread, and the nested tree
comments = rr.comments(post_id="1xktat3", limit=50)
tree = rr.tree("1xktat3", depth=10)

# how far behind the archive is, right now
print(rr.lag_minutes("MachineLearning"))
```

Every dataclass has `to_dict()`, so results drop straight into JSON output, a database row or an
agent's tool result.

## Exit codes

```
0  ok
1  error (bad argument, unexpected response shape)
2  blocked  (usually a missing or generic User-Agent)
3  unavailable after retries
4  not found
```

## Using it from a Hermes agent

Copy `skills/reddit-reach/` into your agent's skill directory. It teaches the agent when to reach
for the archive instead of `web_fetch`, and which mistakes to avoid.

```
cp -r skills/reddit-reach ~/.hermes/skills/research/reddit-reach
```

## Design notes

- **No dependencies.** `urllib` and `dataclasses` only, so it installs on a locked-down host.
- **Transport injection.** `RedditReach(transport=...)` takes any `(url, headers, timeout) ->
  (status, bytes)` callable, which is how the tests run with no network.
- **Status 200 is not success.** A 200 whose body is HTML or a challenge page raises `ReachError`.
- **Retries** cover 429 and 5xx with exponential backoff. 403 and 404 fail immediately, because a
  bad user agent and a missing post are real answers, not hiccups.

## Legal and etiquette

This reads public posts through a third-party archive. It does not log in, does not use anyone's
account, and does not post. Reddit's terms and the archive operator's terms still apply to you.
Keep request rates low, cache what you fetch, and do not use it to build a profile of an
individual. If you need guaranteed access, use Reddit's official API with your own credentials.

## Tests

```bash
pip install -e ".[dev]"
pytest
ruff check .
```

The suite never touches the network; every test injects a fake transport.

## License

MIT. See [LICENSE](LICENSE).
