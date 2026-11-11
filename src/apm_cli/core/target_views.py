"""Read-only target projections that do not snapshot a provider's import cycle."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Iterator, Set


class TargetSetView(Set[str]):
    """Resolve catalogue values on use, after entry-point discovery completes."""

    def __init__(self, values: Callable[[], Iterable[str]]) -> None:
        self._values = values

    def __contains__(self, value: object) -> bool:
        return value in frozenset(self._values())

    def __iter__(self) -> Iterator[str]:
        return iter(frozenset(self._values()))

    def __len__(self) -> int:
        return len(frozenset(self._values()))

    @classmethod
    def _from_iterable(cls, values: Iterable[str]) -> frozenset[str]:
        return frozenset(values)
