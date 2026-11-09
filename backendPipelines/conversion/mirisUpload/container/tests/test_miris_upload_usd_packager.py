#  Copyright 2026 Amazon.com, Inc. or its affiliates. All Rights Reserved.
#  SPDX-License-Identifier: Apache-2.0
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest

from usd_packager import choose_roots, local_download_plan, root_candidates, should_skip_packaging

pytestmark = pytest.mark.unit


ASSET_ID = "x86487e0b-9f50-4b7b-9b59-8963c6791b56"


ROOT = f"{ASSET_ID}/"


def test_local_download_plan_maps_keys_to_relative_paths():
    keys = [
        f"{ASSET_ID}/",  # folder marker -> skipped
        f"{ASSET_ID}/Red Armchair.usda",
        f"{ASSET_ID}/textures/wood.jpg",
        f"{ASSET_ID}/textures/wood_bump.jpg",
    ]
    plan = local_download_plan(keys, ROOT)
    assert plan == [
        (f"{ASSET_ID}/Red Armchair.usda", "Red Armchair.usda"),
        (f"{ASSET_ID}/textures/wood.jpg", "textures/wood.jpg"),
        (f"{ASSET_ID}/textures/wood_bump.jpg", "textures/wood_bump.jpg"),
    ]


def test_local_download_plan_skips_keys_outside_the_root():
    keys = ["someOther/thing.usda", f"{ASSET_ID}/root.usda"]
    plan = local_download_plan(keys, ROOT)
    assert plan == [(f"{ASSET_ID}/root.usda", "root.usda")]


def test_local_download_plan_requires_the_root_at_the_start():
    # the asset id appearing mid-key (not as the leading root) must be rejected
    keys = [f"someOtherPrefix/{ASSET_ID}/file.usda", f"{ASSET_ID}/root.usda"]
    plan = local_download_plan(keys, ROOT)
    assert plan == [(f"{ASSET_ID}/root.usda", "root.usda")]


def test_local_download_plan_under_a_prefixed_root():
    root = f"vams/data/{ASSET_ID}/"
    plan = local_download_plan([f"{root}scene.usda", f"{ASSET_ID}/scene.usda"], root)
    assert plan == [(f"{root}scene.usda", "scene.usda")]


@pytest.mark.parametrize("rel", ["../escape.usda", "a/../../escape.usda", "/etc/passwd"])
def test_local_download_plan_skips_paths_that_leave_the_download_directory(rel):
    assert local_download_plan([f"{ROOT}{rel}"], ROOT) == []


def test_local_download_plan_normalizes_dot_segments():
    keys = [f"{ROOT}./scene.usda", f"{ROOT}geo/x/../geo.usd", f"{ROOT}a//b.usdc"]
    assert [rel for _, rel in local_download_plan(keys, ROOT)] == ["scene.usda", "geo/geo.usd", "a/b.usdc"]


def test_root_candidates_are_distinct():
    assert root_candidates(["scene.usda", "scene.usda"], [".usda"]) == ["scene.usda"]


def test_root_candidates_match_the_extension_exactly_and_case_insensitively():
    paths = ["Scene.USDA", "geo.usd", "pump.usdz", "notes.usda.bak", "wood.png", "x.us"]
    assert root_candidates(paths, [".usd", ".usda", ".usdz"]) == ["Scene.USDA", "geo.usd", "pump.usdz"]


def _refs(graph):
    """references_of over a {layer: {referenced layers}} graph, recording each call."""
    calls = []

    def references_of(layer):
        calls.append(layer)
        return set(graph.get(layer, ()))

    references_of.calls = calls
    return references_of


def test_the_unreferenced_layer_is_the_root_whichever_file_triggered():
    graph = {"scene.usda": {"geo/geo.usd", "mat.usdc"}, "geo/geo.usd": {"mat.usdc"}}
    candidates = ["geo/geo.usd", "mat.usdc", "scene.usda"]

    assert choose_roots(candidates, _refs(graph)) == ["scene.usda"]


def test_two_independent_layers_are_two_roots():
    graph = {"a.usda": {"shared.usd"}, "b.usda": {"shared.usd"}}

    assert choose_roots(["a.usda", "b.usda", "shared.usd"], _refs(graph)) == ["a.usda", "b.usda"]


def test_a_single_candidate_is_its_root_without_a_scan():
    references_of = _refs({})

    assert choose_roots(["pump.usdz"], references_of) == ["pump.usdz"]
    assert references_of.calls == []


def test_a_usdz_is_never_scanned_but_can_be_referenced():
    references_of = _refs({"scene.usda": {"props/chair.usdz"}})

    assert choose_roots(["props/chair.usdz", "scene.usda"], references_of) == ["scene.usda"]
    assert references_of.calls == ["scene.usda"]


def test_a_candidate_referenced_through_a_non_candidate_layer_is_not_a_root():
    """scene.usda references cache.usdc, which is not a candidate (its extension does not trigger
    the pipeline), and cache.usdc references geo.usda."""
    references_of = _refs({"scene.usda": {"cache.usdc"}, "cache.usdc": {"geo.usda"}})

    assert choose_roots(["geo.usda", "scene.usda"], references_of) == ["scene.usda"]


def test_each_layer_is_read_once():
    graph = {f"l{i}.usda": {f"l{i + 1}.usda"} for i in range(50)}
    references_of = _refs(graph)
    candidates = sorted(f"l{i}.usda" for i in range(51))

    assert choose_roots(candidates, references_of) == ["l0.usda"]
    assert sorted(references_of.calls) == candidates


def test_non_layer_files_are_never_read():
    references_of = _refs({"scene.usda": {"tex/wood.png", "props/chair.usdz"}})

    choose_roots(["props/chair.usdz", "scene.usda"], references_of)
    assert references_of.calls == ["scene.usda"]


def test_a_layer_cycle_leaves_no_root():
    graph = {"a.usda": {"b.usda"}, "b.usda": {"a.usda"}}

    assert choose_roots(["a.usda", "b.usda"], _refs(graph)) == []


def test_should_skip_packaging_true_when_no_dependencies():
    # one layer (the root itself), no asset deps
    assert should_skip_packaging(1, 0) is True


def test_should_skip_packaging_false_with_sublayer_or_textures():
    assert should_skip_packaging(2, 0) is False   # has a sublayer/reference
    assert should_skip_packaging(1, 3) is False    # has texture assets


import zipfile


def _write_usda_with_texture(dir_path: str, texture_name: str = "tex.png") -> str:
    """Create root.usda referencing a texture asset + the texture file itself.
    Returns the root .usda path."""
    tex_path = os.path.join(dir_path, texture_name)
    with open(tex_path, "wb") as f:
        f.write(b"\x89PNG\r\n\x1a\n" + b"0" * 64)  # dummy but present
    root = os.path.join(dir_path, "root.usda")
    with open(root, "w") as f:
        f.write(
            '#usda 1.0\n'
            'def Material "M" {\n'
            '    def Shader "T" {\n'
            '        uniform token info:id = "UsdUVTexture"\n'
            f'        asset inputs:file = @./{texture_name}@\n'
            '    }\n'
            '}\n'
        )
    return root


def test_compute_dependencies_finds_texture(tmp_path):
    pytest.importorskip("pxr")
    from usd_packager import compute_dependencies

    root = _write_usda_with_texture(str(tmp_path))
    layers, assets, unresolved = compute_dependencies(root)

    assert unresolved == []
    assert any(a.endswith("tex.png") for a in assets)


def test_compute_dependencies_reports_unresolved_for_missing_ref(tmp_path):
    pytest.importorskip("pxr")
    from usd_packager import compute_dependencies

    # reference a texture that does not exist on disk
    root = os.path.join(str(tmp_path), "root.usda")
    with open(root, "w") as f:
        f.write(
            '#usda 1.0\n'
            'def Material "M" {\n'
            '    def Shader "T" {\n'
            '        uniform token info:id = "UsdUVTexture"\n'
            '        asset inputs:file = @/nonexistent/abs/missing.png@\n'
            '    }\n'
            '}\n'
        )
    layers, assets, unresolved = compute_dependencies(root)
    assert unresolved, "expected the missing absolute reference to be unresolved"


def test_package_usdz_bundles_root_and_texture(tmp_path):
    pytest.importorskip("pxr")
    from usd_packager import package_usdz

    root = _write_usda_with_texture(str(tmp_path))
    out = os.path.join(str(tmp_path), "out.usdz")
    package_usdz(root, out)

    assert os.path.exists(out)
    with zipfile.ZipFile(out) as z:
        names = z.namelist()
    assert any(n.endswith("root.usda") for n in names)
    assert any(n.endswith("tex.png") for n in names)


def _write_layer_tree(dir_path):
    """root.usda sublayers sub/geo.usda (which pays-load mat.usda) and references props/chair.usdz."""
    from pxr import Sdf, UsdUtils

    os.makedirs(os.path.join(dir_path, "sub"))
    os.makedirs(os.path.join(dir_path, "props"))
    with open(os.path.join(dir_path, "root.usda"), "w") as f:
        f.write('#usda 1.0\n(\n    subLayers = [@./sub/geo.usda@]\n)\n'
                'def Xform "W" (prepend references = @./props/chair.usdz@) {}\n')
    with open(os.path.join(dir_path, "sub", "geo.usda"), "w") as f:
        f.write('#usda 1.0\ndef Xform "G" (prepend payload = @../mat.usda@) {}\n')
    with open(os.path.join(dir_path, "mat.usda"), "w") as f:
        f.write('#usda 1.0\ndef "M" {}\n')
    inner = os.path.join(dir_path, "chair_src.usda")
    with open(inner, "w") as f:
        f.write('#usda 1.0\ndef "Chair" {}\n')
    assert UsdUtils.CreateNewUsdzPackage(Sdf.AssetPath(inner), os.path.join(dir_path, "props", "chair.usdz"))
    os.remove(inner)


def test_direct_references_lists_what_a_layer_names_relative_to_the_asset(tmp_path):
    pytest.importorskip("pxr")
    from usd_packager import direct_references

    _write_layer_tree(str(tmp_path))

    assert direct_references(str(tmp_path / "root.usda"), str(tmp_path)) == {
        "sub/geo.usda", "props/chair.usdz"}
    assert direct_references(str(tmp_path / "sub" / "geo.usda"), str(tmp_path)) == {"mat.usda"}
    assert direct_references(str(tmp_path / "missing.usda"), str(tmp_path)) == set()


def _refs_on_disk(base):
    from usd_packager import direct_references

    return lambda rel: direct_references(str(base / rel), str(base))


@pytest.mark.parametrize("trigger", ["root.usda", "sub/geo.usda", "mat.usda", "props/chair.usdz"])
def test_the_root_layer_is_chosen_whichever_file_triggered(tmp_path, trigger):
    pytest.importorskip("pxr")
    _write_layer_tree(str(tmp_path))
    candidates = ["mat.usda", "props/chair.usdz", "root.usda", "sub/geo.usda"]

    assert choose_roots(candidates, _refs_on_disk(tmp_path)) == ["root.usda"]


def test_two_independent_root_layers_are_ambiguous(tmp_path):
    pytest.importorskip("pxr")
    for name in ("a.usda", "b.usda"):
        with open(tmp_path / name, "w") as f:
            f.write('#usda 1.0\n(\n    subLayers = [@./shared.usda@]\n)\n')
    with open(tmp_path / "shared.usda", "w") as f:
        f.write('#usda 1.0\ndef "S" {}\n')

    assert choose_roots(["a.usda", "b.usda", "shared.usda"], _refs_on_disk(tmp_path)) == [
        "a.usda", "b.usda"]


def test_value_clip_layers_are_referenced_by_their_root(tmp_path):
    """Clip layers, listed or named by a template, are dependencies of the layer that uses them."""
    pytest.importorskip("pxr")
    os.makedirs(tmp_path / "frames")
    for i in (1, 2, 3):
        with open(tmp_path / "frames" / f"f.{i}.usd", "w") as f:
            f.write('#usda 1.0\ndef "C" {}\n')
    with open(tmp_path / "frames" / "manifest.usda", "w") as f:
        f.write('#usda 1.0\ndef "C" {}\n')
    with open(tmp_path / "anim.usda", "w") as f:
        f.write('#usda 1.0\ndef Xform "C" (\n    clips = {\n        dictionary default = {\n'
                '            string templateAssetPath = "./frames/f.#.usd"\n'
                '            double templateStartTime = 1\n            double templateEndTime = 3\n'
                '            double templateStride = 1\n'
                '            asset manifestAssetPath = @./frames/manifest.usda@\n'
                '            string primPath = "/C"\n        }\n    }\n) {}\n')
    candidates = ["anim.usda", "frames/f.1.usd", "frames/f.2.usd", "frames/f.3.usd",
                  "frames/manifest.usda"]

    assert choose_roots(candidates, _refs_on_disk(tmp_path)) == ["anim.usda"]


@pytest.mark.parametrize("content", ["", "#usda 1.0\ndef garbage {{{\n", "not usd at all"],
                         ids=["empty", "malformed", "not-usd"])
def test_an_unreadable_layer_raises_with_its_path(tmp_path, content):
    pytest.importorskip("pxr")
    from usd_packager import UnreadableLayerError, compute_dependencies, direct_references

    bad = tmp_path / "bad.usda"
    bad.write_text(content)

    with pytest.raises(UnreadableLayerError) as raised:
        direct_references(str(bad), str(tmp_path))
    assert raised.value.path == str(bad)
    with pytest.raises(UnreadableLayerError):
        compute_dependencies(str(bad))


@pytest.mark.parametrize("content", ["", "not usd at all", "\x00\x01\x02 random bytes"],
                         ids=["empty", "not-usd", "binary"])
def test_an_unreadable_usd_root_raises_from_the_dependency_scan(tmp_path, content):
    """pxr returns no layers, rather than raising, for an unreadable `.usd` root."""
    pytest.importorskip("pxr")
    from usd_packager import UnreadableLayerError, compute_dependencies

    bad = tmp_path / "bad.usd"
    bad.write_text(content)

    with pytest.raises(UnreadableLayerError) as raised:
        compute_dependencies(str(bad))
    assert raised.value.path == str(bad)


def test_five_hundred_layers_are_scanned_in_linear_time(tmp_path):
    """A chain of layers is the worst case for a per-candidate transitive scan."""
    pytest.importorskip("pxr")
    import time

    count = 500
    for i in range(count):
        sublayer = f'(\n    subLayers = [@./l{i + 1}.usda@]\n)\n' if i + 1 < count else ""
        (tmp_path / f"l{i}.usda").write_text(f'#usda 1.0\n{sublayer}def "P{i}" {{}}\n')
    candidates = sorted(f"l{i}.usda" for i in range(count))

    started = time.monotonic()
    roots = choose_roots(candidates, _refs_on_disk(tmp_path))
    elapsed = time.monotonic() - started

    assert roots == ["l0.usda"]
    assert elapsed < 3.0, f"500-layer root scan took {elapsed:.2f}s"


def test_paths_outside_lists_files_whose_real_path_leaves_the_asset(tmp_path):
    from usd_packager import paths_outside

    asset = tmp_path / "asset"
    (asset / "textures").mkdir(parents=True)
    base = str(asset)
    paths = [
        os.path.join(base, "root.usda"),
        os.path.join(base, "textures", "wood.png"),
        os.path.join(base, "props", "chair.usdz[chair.usdc]"),
        os.path.join(base, "textures", "..", "..", "outside", "secret.png"),
        "/etc/passwd",
    ]

    assert paths_outside(paths, base) == sorted({
        os.path.realpath(str(tmp_path / "outside" / "secret.png")), os.path.realpath("/etc/passwd")})


def test_paths_outside_does_not_accept_a_sibling_sharing_the_prefix(tmp_path):
    from usd_packager import paths_outside

    base = str(tmp_path / "asset")
    sibling = str(tmp_path / "asset-other" / "tex.png")

    assert paths_outside([sibling], base) == [os.path.realpath(sibling)]
