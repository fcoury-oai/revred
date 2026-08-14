"""Operational errors surfaced by the review reducer."""


class ReviewReducerError(Exception):
    """A safe-to-display failure that prevents a trustworthy review result."""


class CodexInvocationError(ReviewReducerError):
    """A Codex subprocess failed or did not produce its requested artifact."""


class InvalidReviewError(ReviewReducerError):
    """The native reviewer produced findings that could not be parsed safely."""


class SnapshotDriftError(ReviewReducerError):
    """The repository moved while a pinned review was running."""


class BudgetExceededError(ReviewReducerError):
    """A generated repair exceeded an explicit complexity or churn budget."""
