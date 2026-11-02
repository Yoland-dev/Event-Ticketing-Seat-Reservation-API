import re
import uuid

_VALID_ID = re.compile(r"^[A-Za-z0-9._-]{1,64}$")


class RequestIDMiddleware:
    """Attach a request ID to every request and response for log/error correlation.

    An upstream proxy's X-Request-ID is reused when it looks safe; otherwise a new
    one is generated.
    """

    header = "X-Request-ID"

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        incoming = request.headers.get(self.header, "")
        request.request_id = incoming if _VALID_ID.match(incoming) else uuid.uuid4().hex
        response = self.get_response(request)
        response[self.header] = request.request_id
        return response
