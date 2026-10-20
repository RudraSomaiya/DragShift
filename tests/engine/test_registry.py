from pathlib import Path

import pytest

from dragshift.engine import registry
from dragshift.engine.kinds import Kind
from dragshift.engine.registry import Op, get, op, ops_for, register

IMG = frozenset({Kind.IMAGE})
PDF = frozenset({Kind.PDF})


def make(op_id: str, **kw) -> Op:
    fields = {"wheel": "convert", "label": op_id, "kinds": IMG, "func": "m:f"} | kw
    return register(Op(id=op_id, **fields))


@pytest.mark.usefixtures("clean_registry")
class TestRegistry:
    def test_register_and_get(self) -> None:
        o = make("convert.image.png")
        assert get("convert.image.png") is o
        with pytest.raises(KeyError):
            get("nope")

    def test_duplicate_id_rejected(self) -> None:
        make("a")
        with pytest.raises(ValueError, match="duplicate"):
            make("a", label="Other")

    def test_reregistering_the_same_op_is_harmless(self) -> None:
        # A module that failed half-way through loading is imported again on the next call.
        first = make("a")
        assert make("a") == first

    def test_decorator_records_dotted_path_and_returns_function(self) -> None:
        @op(id="x", wheel="tools", label="X", kinds=IMG)
        def worker_fn(paths, params, ctx):
            return []

        module, _, qualname = get("x").func.partition(":")
        assert module == __name__
        assert qualname.endswith(".worker_fn")
        assert callable(worker_fn)

    def test_filters_by_wheel_and_keeps_registration_order(self) -> None:
        make("c1")
        make("t1", wheel="tools")
        make("c2")
        assert [o.id for o in ops_for([Path("a.png")], "convert")] == ["c1", "c2"]
        assert [o.id for o in ops_for([Path("a.png")], "tools")] == ["t1"]

    def test_intersection_over_mixed_kinds(self) -> None:
        make("img_only")
        make("both", kinds=IMG | PDF)
        make("pdf_only", kinds=PDF)
        mixed = [Path("a.png"), Path("b.pdf")]
        assert [o.id for o in ops_for(mixed, "convert")] == ["both"]

    def test_min_files(self) -> None:
        make("merge", multi="all", min_files=2)
        assert ops_for([Path("a.png")], "convert") == []
        assert [o.id for o in ops_for([Path("a.png"), Path("b.png")], "convert")] == ["merge"]

    def test_unavailable_ops_hidden(self) -> None:
        make("office", available=lambda: False)
        assert ops_for([Path("a.png")], "convert") == []

    def test_hide_if_same_ext_including_aliases(self) -> None:
        make("jpg", hide_if_same_ext="jpg")
        make("png", hide_if_same_ext="png")
        assert [o.id for o in ops_for([Path("a.JPEG")], "convert")] == ["png"]
        assert [o.id for o in ops_for([Path("a.png")], "convert")] == ["jpg"]
        # Valid for all files means hidden if any input already has that format.
        assert ops_for([Path("a.png"), Path("b.jpg")], "convert") == []

    def test_no_paths(self) -> None:
        make("a")
        assert ops_for([], "convert") == []

    def test_accepts(self) -> None:
        merge = make("merge", multi="all", min_files=2)
        assert registry.accepts(merge, [Path("a.png"), Path("b.png")])
        assert not registry.accepts(merge, [Path("a.png")])
        assert not registry.accepts(merge, [Path("a.pdf"), Path("b.png")])
        assert not registry.accepts(merge, [])


def test_failed_op_module_import_is_retried(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(registry, "_OPS", {})
    monkeypatch.setattr(registry, "_loaded", False)
    monkeypatch.setattr(registry, "_OP_PACKAGES", ("dragshift.engine.no_such_package",))
    for _ in range(2):  # raised every time, never a silently partial registry
        with pytest.raises(ModuleNotFoundError):
            ops_for([Path("a.png")], "convert")
    assert not registry._loaded


def test_real_ops_are_auto_loaded() -> None:
    assert get("convert.image.jpg").func == "dragshift.engine.converters.images:to_jpg"
    assert "convert.image.jpg" in [o.id for o in ops_for([Path("a.png")], "convert")]
    assert "convert.image.jpg" not in [o.id for o in ops_for([Path("a.jpg")], "convert")]
    assert registry._loaded
