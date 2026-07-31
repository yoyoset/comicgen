"""infra.engine_cache.EngineCache 的纯逻辑测试。"""
from comic_gen.infra.engine_cache import EngineCache


def test_builds_once_for_same_signature():
    cache = EngineCache()
    calls = []

    def factory():
        calls.append(1)
        return object()

    a = cache.get_or_rebuild("sig-a", factory)
    b = cache.get_or_rebuild("sig-a", factory)
    assert a is b
    assert len(calls) == 1


def test_rebuilds_when_signature_changes():
    cache = EngineCache()
    calls = []

    def factory():
        calls.append(1)
        return object()

    a = cache.get_or_rebuild("sig-a", factory)
    b = cache.get_or_rebuild("sig-b", factory)
    assert a is not b
    assert len(calls) == 2


def test_tuple_signatures_compare_by_value():
    cache = EngineCache()
    calls = []

    def factory():
        calls.append(1)
        return object()

    cache.get_or_rebuild(("x", 1, None), factory)
    cache.get_or_rebuild(("x", 1, None), factory)  # 值相同的新元组，不应重建
    assert len(calls) == 1
    cache.get_or_rebuild(("x", 2, None), factory)
    assert len(calls) == 2


def test_first_call_always_builds():
    cache = EngineCache()
    calls = []
    cache.get_or_rebuild(None, lambda: calls.append(1) or object())
    assert len(calls) == 1


def test_independent_caches_do_not_interfere():
    a_cache, b_cache = EngineCache(), EngineCache()
    a_calls, b_calls = [], []
    a_cache.get_or_rebuild("x", lambda: a_calls.append(1) or object())
    b_cache.get_or_rebuild("x", lambda: b_calls.append(1) or object())
    assert len(a_calls) == 1
    assert len(b_calls) == 1
