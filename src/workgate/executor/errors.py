"""Executor-local feature error vocabulary."""


class ExecutorOperationFailure(RuntimeError):
    """Feature-owned executor operation failure returned over ordinary RPC."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
