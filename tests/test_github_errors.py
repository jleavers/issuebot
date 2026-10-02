"""The one classification of a failed gh invocation, shared by the adapter and the billing reads."""

import pytest

from issuebot.github.errors import categorise


@pytest.mark.parametrize(
    ("returncode", "stderr", "category"),
    [
        (4, "", "auth"),
        (1, "gh: Bad credentials (HTTP 401)", "auth"),
        (1, "gh: Not Found (HTTP 404)", "not_found"),
        (1, "gh: API rate limit exceeded (HTTP 403)", "rate_limited"),
        (1, "HTTP 429", "rate_limited"),
        (1, "gh: Bad Gateway (HTTP 502)", "transport"),
        (
            1,
            'Post "https://api.github.com/": dial tcp: lookup api.github.com: no such host',
            "transport",
        ),
        (1, "gh: Resource not accessible by personal access token (HTTP 403)", "auth"),
        (1, "gh: I'm a teapot (HTTP 418)", "status"),
    ],
)
def test_categorise(returncode: int, stderr: str, category: str) -> None:
    assert categorise(returncode, stderr) == category
