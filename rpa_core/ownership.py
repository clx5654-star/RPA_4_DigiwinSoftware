from dataclasses import dataclass, field
from typing import Iterable, Set

from .models import WindowIdentity


@dataclass
class WindowOwnership:
    initial: Set[WindowIdentity]
    owned: Set[WindowIdentity] = field(default_factory=set)

    @classmethod
    def start(cls, windows: Iterable[WindowIdentity]):
        return cls(set(windows))

    def observe(self, windows: Iterable[WindowIdentity]):
        current = set(windows)
        newly_created = current - self.initial - self.owned
        self.owned.update(newly_created)
        return newly_created

    def claim(self, identity: WindowIdentity):
        if identity in self.initial:
            raise ValueError(f"window {identity} existed before this run")
        self.owned.add(identity)
        return identity

    def cleanup_targets(self, windows: Iterable[WindowIdentity]):
        return self.owned.intersection(set(windows))
