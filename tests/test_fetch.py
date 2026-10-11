"""Offline regression tests for forrest_type.fetch (network stubbed).

Covers the retry-after-runtime-error-remark path: every POST attempt must
send the Overpass query itself, never a previously parsed response body.
"""

import forrest_type
from forrest_type import fetch

QUERY = "[out:json];node(around:1000,0,0);out;"


class FakeResponse:
    def __init__(self, payload, status_code=200):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        if isinstance(self._payload, bytes):
            raise ValueError("not JSON")
        return self._payload


def test_fetch_retries_send_query_body(monkeypatch):
    """After a runtime-error remark the retry must POST the query again."""
    bodies = []
    responses = [
        # First attempt: HTTP 200 but a runtime-error remark -> retry.
        FakeResponse({"remark": "runtime error: Query timed out", "elements": []}),
        # Second attempt: a valid Overpass response -> return it.
        FakeResponse({"elements": [], "remark": ""}),
    ]

    def fake_post(url, data=None, headers=None, timeout=None):
        bodies.append(data)
        return responses[len(bodies) - 1]

    monkeypatch.setattr(forrest_type.requests, "post", fake_post)
    monkeypatch.setattr(forrest_type.time, "sleep", lambda s: None)

    result = fetch(QUERY)

    assert len(bodies) == 2
    for data in bodies:
        assert data == {"data": QUERY}
    assert result == {"elements": [], "remark": ""}


def test_fetch_non_dict_json(monkeypatch):
    """HTTP 200 with a non-dict JSON root must not raise AttributeError."""
    calls = []

    def fake_post(url, data=None, headers=None, timeout=None):
        calls.append(data)
        return FakeResponse([1, 2, 3])  # JSON list, not an object

    monkeypatch.setattr(forrest_type.requests, "post", fake_post)
    monkeypatch.setattr(forrest_type.time, "sleep", lambda s: None)

    # The custom --endpoint path returns instead of sys.exit(1).
    try:
        fetch(QUERY, "https://example.org/api/interpreter")
    except SystemExit:
        pass  # acceptable: fetch reports failure and exits
