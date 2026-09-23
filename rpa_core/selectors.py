from typing import Callable, Iterable, TypeVar

T = TypeVar("T")


class SelectorResolutionError(RuntimeError):
    def __init__(self, key, candidates=()):
        self.key = key
        self.candidates = tuple(candidates)
        super().__init__(self._message())

    def _message(self):
        return f"selector {self.key!r} resolution failed"


class SelectorNotFound(SelectorResolutionError):
    def _message(self):
        return f"selector {self.key!r} matched no candidates"


class AmbiguousSelector(SelectorResolutionError):
    def _message(self):
        return (f"selector {self.key!r} matched {len(self.candidates)} candidates; "
                "refusing to choose one")


def require_unique(
    candidates: Iterable[T],
    key: str,
    *,
    describe: Callable[[T], object] | None = None,
) -> T:
    items = list(candidates)
    evidence = [describe(x) for x in items] if describe else items
    if not items:
        raise SelectorNotFound(key, evidence)
    if len(items) != 1:
        raise AmbiguousSelector(key, evidence)
    return items[0]
