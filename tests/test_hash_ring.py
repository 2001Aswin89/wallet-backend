from collections import Counter

import pytest

from sharding.hash_ring import HashRing

KEYS = range(1, 20_001)


def test_routing_is_stable_and_spread_out():
    ring = HashRing([f"s{i}" for i in range(4)], vnodes=100)
    first = {k: ring.get_node(k) for k in KEYS}
    assert first == {k: ring.get_node(k) for k in KEYS}
    assert HashRing([f"s{i}" for i in range(4)], vnodes=100).get_node(12345) == ring.get_node(12345)
    counts = Counter(first.values())
    assert set(counts) == {f"s{i}" for i in range(4)}
    assert max(counts.values()) < 1.3 * len(KEYS) / 4 and min(counts.values()) > 0.7 * len(KEYS) / 4


def test_adding_a_shard_moves_about_one_nth_and_only_to_the_new_shard():
    ring = HashRing([f"s{i}" for i in range(4)], vnodes=100)
    before = {k: ring.get_node(k) for k in KEYS}
    ring.add_node("s4")
    after = {k: ring.get_node(k) for k in KEYS}
    moved = [k for k in KEYS if before[k] != after[k]]
    assert all(after[k] == "s4" for k in moved)               # nothing shuffles between the old shards
    assert 0.1 < len(moved) / len(KEYS) < 0.3                 # about 1/5
    mod_moved = sum(1 for k in KEYS if k % 4 != k % 5)
    assert mod_moved / len(KEYS) > 0.7                         # plain modulo reshuffles most keys


def test_removing_a_shard_only_moves_that_shards_keys():
    ring = HashRing([f"s{i}" for i in range(5)], vnodes=100)
    before = {k: ring.get_node(k) for k in KEYS}
    ring.remove_node("s2")
    after = {k: ring.get_node(k) for k in KEYS}
    assert all(before[k] == "s2" for k in KEYS if before[k] != after[k])
    assert "s2" not in set(after.values())


def test_empty_ring_is_an_error():
    with pytest.raises(LookupError):
        HashRing().get_node(1)
