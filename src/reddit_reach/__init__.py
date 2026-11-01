"""reddit-reach — read public Reddit data from an IP that Reddit blocks."""

from .client import (
    MAX_LIMIT,
    BlockedError,
    Comment,
    Post,
    RateLimitError,
    ReachError,
    RedditReach,
)

__version__ = "0.1.0"
__all__ = [
    "RedditReach",
    "Post",
    "Comment",
    "ReachError",
    "BlockedError",
    "RateLimitError",
    "MAX_LIMIT",
    "__version__",
]
