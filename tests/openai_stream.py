"""Serve the SDK's streamed Responses API from a fake client's parse() coroutine."""

from types import SimpleNamespace

_TERMINAL = {"completed": "response.completed", "incomplete": "response.incomplete"}


class FakeResponseStream:
    def __init__(self, parse, arguments):
        self.parse, self.arguments, self.response = parse, arguments, None

    async def __aenter__(self):
        self.response = await self.parse(**self.arguments)
        return self

    async def __aexit__(self, *_exc):
        return False

    async def __aiter__(self):
        status = getattr(self.response, "status", None)
        yield SimpleNamespace(type=_TERMINAL.get(status, "response.failed"), response=self.response)

    async def get_final_response(self):
        return self.response


def with_stream(responses):
    """Add responses.stream(**kwargs), recorded through the existing parse mock."""
    responses.stream = lambda **arguments: FakeResponseStream(responses.parse, arguments)
    return responses
