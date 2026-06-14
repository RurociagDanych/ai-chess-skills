from dataclasses import dataclass
from enum import StrEnum


class ErrorCode(StrEnum):
    INVALID_INPUT = "invalid_input"
    HTTP_ERROR = "http_error"
    RATE_LIMITED = "rate_limited"
    INVALID_PGN = "invalid_pgn"
    ENGINE_MISSING = "engine_missing"
    UNSUPPORTED_PLATFORM = "unsupported_platform"
    APPROVAL_REQUIRED = "approval_required"
    DOWNLOAD_FAILED = "download_failed"
    UNSAFE_ARCHIVE = "unsafe_archive"
    ENGINE_FAILED = "engine_failed"
    PARTIAL_ANALYSIS = "partial_analysis"


@dataclass(slots=True)
class AppError(Exception):
    code: ErrorCode
    message: str
    remedy: str

    def __post_init__(self) -> None:
        Exception.__init__(self, self.message)

    def __reduce__(self):
        return type(self), (self.code, self.message, self.remedy)

    def to_dict(self) -> dict[str, str]:
        return {
            "code": self.code.value,
            "message": self.message,
            "remedy": self.remedy,
        }
