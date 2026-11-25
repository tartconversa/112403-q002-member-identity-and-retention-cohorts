"""Domain exceptions."""


class RepurchaseAppError(Exception):
    """Base error for user-facing application failures."""


class DataValidationError(RepurchaseAppError):
    """Raised when source CSV data does not satisfy the documented schema."""

    def __init__(self, path: str, line: int | None, message: str):
        location = f"{path}"
        if line is not None:
            location += f":{line}"
        super().__init__(f"{location}: {message}")
        self.path = path
        self.line = line


class CampaignStateError(RepurchaseAppError):
    """Raised when a frozen or sent campaign record would be improperly changed."""
