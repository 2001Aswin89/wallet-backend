"""Consistent hashing with virtual nodes, for routing a user to a database shard.

Shard key: user_id. A user's wallets, and the ledger entries on them, live on one shard, so a
balance or a statement is a single-shard query.

Each physical shard is placed on the ring many times ("virtual nodes"), which evens out the load.
Adding a shard takes over only the ring segments right before its virtual nodes, so about 1/N of
the keys move, versus almost all of them with `hash(key) % N`.
"""
import bisect
import hashlib


def _point(value: str) -> int:
    """A stable position on the ring (Python's built-in hash() changes between runs)."""
    return int.from_bytes(hashlib.md5(value.encode()).digest()[:8], "big")


class HashRing:
    def __init__(self, nodes: list[str] | None = None, vnodes: int = 100):
        self.vnodes = vnodes
        self._points: list[int] = []          # sorted ring positions
        self._owner: dict[int, str] = {}      # position -> physical node
        for n in nodes or []:
            self.add_node(n)

    @property
    def nodes(self) -> set[str]:
        return set(self._owner.values())

    def add_node(self, node: str) -> None:
        for i in range(self.vnodes):
            p = _point(f"{node}#{i}")
            if p not in self._owner:
                bisect.insort(self._points, p)
                self._owner[p] = node

    def remove_node(self, node: str) -> None:
        self._points = [p for p in self._points if self._owner[p] != node]
        self._owner = {p: n for p, n in self._owner.items() if n != node}

    def get_node(self, key: int | str) -> str:
        """The first virtual node clockwise from the key's position (wrapping around)."""
        if not self._points:
            raise LookupError("ring is empty")
        i = bisect.bisect(self._points, _point(str(key))) % len(self._points)
        return self._owner[self._points[i]]
