"""Shared assertions for the test suite."""


def assert_error(response, status_code, code=None):
    """Every error response must follow the standard JSON error schema."""
    assert response.status_code == status_code, response.content
    body = response.json()
    assert set(body) == {"error"}
    error = body["error"]
    assert set(error) == {
        "status",
        "code",
        "message",
        "details",
        "path",
        "method",
        "request_id",
        "timestamp",
    }
    assert error["status"] == status_code
    assert error["message"]
    assert error["request_id"] == response["X-Request-ID"]
    if code is not None:
        assert error["code"] == code, error
    return error
