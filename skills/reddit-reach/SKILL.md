---
name: reddit-reach
description: Use when a task needs Reddit posts or comments but reddit.com returns 403 to this host. Reads the Arctic Shift archive instead, with no login, key or proxy.
---

# reddit-reach

Reddit blocks datacenter IP ranges at the edge. `web_fetch` on any reddit.com URL returns a 403
block page, and so does `api.reddit.com` and `oauth.reddit.com`. `reddit-reach` reads the same
public posts and comments from the Arctic Shift archive, which does not block this host.

## When to use

- The user asks what a subreddit is saying about something.
- A research job needs community signal (which tool are people actually running) alongside GitHub
  and Hacker News.
- You need the comments of a specific thread, not just the title.
- Anything that would otherwise be a `web_fetch` of a reddit.com URL. That fetch will fail.

## Do not use it for

- Anything that must be current to the minute. The archive lags by 17 to 121 minutes depending on
  the subreddit. Check `reddit-reach latest <sub>` first and report the lag.
- Ranking by `score`. The score is frozen at ingest time.
- Anything that needs a Reddit account, posting, voting or a private subreddit.

## Commands

```bash
reddit-reach check                                   # is the archive reachable, exit 1 if not
reddit-reach latest LocalLLaMA --format text         # newest post + how far behind it is
reddit-reach posts LocalLLaMA --limit 25             # JSON by default
reddit-reach posts selfhosted --limit 100 --before 1793513358
reddit-reach comments --post-id 1xkvp10 --limit 100
reddit-reach tree 1xkvp10 --depth 10
```

`--format json` is the default because the caller is usually an agent. Use `--format text` only
when a human will read it.

## Pitfalls

- **Check bytes, not status codes.** On this host Reddit answers `200` with a zero-byte body and
  `200` with an Anubis challenge page. A `200` that is not JSON raises `ReachError` here on
  purpose; never treat an empty result as "nothing was posted".
- **The newest archived post can be `[ Removed by moderator ]`.** Seen on 2026-11-01 in
  r/selfhosted. When summarising, skip rows whose title is a moderation placeholder and say so
  rather than reporting it as content.
- **`limit` above 100 returns zero rows, not an error.** The client refuses instead of returning
  an empty list. Paginate with `--before <oldest created_utc you have>`.
- **A missing User-Agent is refused with 403.** That is the same symptom as the IP block this
  skill exists to avoid, so pass a real user agent and do not conclude "blocked" before checking.
- **Do not loop this in a cron job without a cache.** It is one volunteer-run archive with no SLA.
  Fetch once per run, store the JSON, and re-read the file on later steps.

## Wiring into a research pipeline

Use it as one more source next to GitHub and Hacker News, not as a replacement:

```bash
reddit-reach posts LocalLLaMA --limit 30 > /tmp/localllama.json
reddit-reach posts selfhosted  --limit 30 > /tmp/selfhosted.json
```

Then filter on titles and `selftext` for the topic, and keep the permalink so the owner can check
the claim himself. A recommendation the owner cannot trace back is worse than no recommendation.
