#  Copyright 2026 Amazon.com, Inc. or its affiliates. All Rights Reserved.
#  SPDX-License-Identifier: Apache-2.0
"""The entry point lists the asset under its resolved root, chooses the root USD file whichever
file triggered the run, checks the size from the listing, and tags and names the Miris asset."""
import json
import os

import pytest

from miris_upload_container_harness import ASSET_ID, METADATA_PREFIX, definition

pytestmark = pytest.mark.unit

ROOT = f"{ASSET_ID}/"


def _fake_usd(container, monkeypatch, graph):
    """Stand in for pxr: `graph` maps each layer to the asset-relative files it names directly."""
    monkeypatch.setattr(container.main, "direct_references",
                        lambda path, base: set(graph.get(path[len(base) + 1:], ())))
    monkeypatch.setattr(container.main, "compute_dependencies",
                        lambda path: ([path, os.path.join(os.path.dirname(path), "dep.usda")], [], []))
    monkeypatch.setattr(container.main, "package_usdz",
                        lambda root_path, out: open(out, "wb").write(b"packaged"))


def _layered_asset(container, root=ROOT):
    for rel in ("scene.usda", "geo/geo.usd", "mat.usdc", "textures/wood.png"):
        container.s3.add("asset-bucket", f"{root}{rel}", "x" * 10)


LAYER_GRAPH = {"scene.usda": {"geo/geo.usd", "mat.usdc"}, "geo/geo.usd": {"mat.usdc"}}


def _metadata_key(container):
    return container.s3.put_object.call_args.kwargs["Key"]


@pytest.mark.parametrize("trigger", ["scene.usda", "geo/geo.usd", "mat.usdc"])
def test_the_root_layer_is_uploaded_whichever_layer_triggered(container, monkeypatch, trigger):
    _layered_asset(container)
    _fake_usd(container, monkeypatch, LAYER_GRAPH)

    container.run(definition(trigger_rel=trigger))

    start = container.miris.start_upload.call_args.kwargs
    assert start["content_path"] == "scene.usdz"
    assert _metadata_key(container) == f"{METADATA_PREFIX}scene.usda.metadata.json"
    body = json.loads(container.s3.put_object.call_args.kwargs["Body"])
    version = {e["metadataKey"]: e["metadataValue"] for e in body["metadata"]}["miris_source_version_id"]
    assert version == f"v-{ROOT}scene.usda"


def test_two_independent_roots_fail_as_ambiguous(container, monkeypatch, capsys):
    for rel in ("a.usda", "b.usda", "shared.usd"):
        container.s3.add("asset-bucket", f"{ROOT}{rel}", "x")
    _fake_usd(container, monkeypatch, {"a.usda": {"shared.usd"}, "b.usda": {"shared.usd"}})

    with pytest.raises(SystemExit) as exited:
        container.run(definition(trigger_rel="a.usda"))

    assert exited.value.code == container.main.EXIT_AMBIGUOUS_ROOT == 10
    assert '"ambiguous_root"' in capsys.readouterr().out
    container.miris_cls.assert_not_called()
    container.s3.put_object.assert_not_called()


def test_several_usdz_files_fail_as_ambiguous_before_anything_downloads(container):
    container.s3.add("asset-bucket", f"{ROOT}a.usdz", "x")
    container.s3.add("asset-bucket", f"{ROOT}b.usdz", "x")

    with pytest.raises(SystemExit) as exited:
        container.run(definition(trigger_rel="a.usdz"))

    assert exited.value.code == 10
    assert container.s3.downloads == []


def test_a_single_usdz_is_uploaded_as_is_and_alone(container):
    container.s3.add("asset-bucket", f"{ROOT}scenes/pump.usdz", "usdz")
    container.s3.add("asset-bucket", f"{ROOT}preview/pump.png", "png")

    container.run(definition(trigger_rel="scenes/pump.usdz"))

    assert [key for _, key, _ in container.s3.downloads] == [f"{ROOT}scenes/pump.usdz"]
    assert container.miris.start_upload.call_args.kwargs["content_path"] == "pump.usdz"


def test_an_asset_without_a_usd_file_fails_with_root_not_found(container):
    container.s3.add("asset-bucket", f"{ROOT}notes.txt", "x")

    with pytest.raises(SystemExit) as exited:
        container.run(definition(trigger_rel="model.usdz"))

    assert exited.value.code == container.main.EXIT_ROOT_NOT_FOUND == 6


def test_only_configured_root_extensions_are_candidates(container):
    container.s3.add("asset-bucket", f"{ROOT}pump.usdz", "usdz")
    container.s3.add("asset-bucket", f"{ROOT}draft.usda", "x")

    container.run(definition(trigger_rel="pump.usdz", rootExtensions=[".usdz"]))

    assert container.miris.start_upload.call_args.kwargs["content_path"] == "pump.usdz"


def test_keys_with_dot_segments_name_the_same_layers_as_references(container, monkeypatch):
    """'./scene.usda' and 'geo/x/../geo.usd' are the files a resolved reference names
    'scene.usda' and 'geo/geo.usd'; matching them raw would leave geo.usd looking unreferenced."""
    container.s3.add("asset-bucket", f"{ROOT}./scene.usda", "x")
    container.s3.add("asset-bucket", f"{ROOT}geo/x/../geo.usd", "x")
    _fake_usd(container, monkeypatch, {"scene.usda": {"geo/geo.usd"}})

    container.run(definition(trigger_rel="geo/geo.usd"))

    assert _metadata_key(container) == f"{METADATA_PREFIX}scene.usda.metadata.json"


# --- unreadable layers ------------------------------------------------------------------------


def _raise_unreadable_for(container, monkeypatch, bad_rel):
    from usd_packager import UnreadableLayerError

    def direct_references(path, base):
        if path.endswith(bad_rel):
            raise UnreadableLayerError(path, RuntimeError(f"Error in parsing {path}"))
        return set()

    monkeypatch.setattr(container.main, "direct_references", direct_references)


def test_an_unreadable_layer_fails_with_its_asset_relative_name(container, monkeypatch, capsys):
    container.s3.add("asset-bucket", f"{ROOT}scene.usda", "x")
    container.s3.add("asset-bucket", f"{ROOT}geo/broken.usd", "")
    _raise_unreadable_for(container, monkeypatch, "geo/broken.usd")

    with pytest.raises(SystemExit) as exited:
        container.run(definition(trigger_rel="scene.usda"))

    assert exited.value.code == container.main.EXIT_UNREADABLE_LAYER == 11
    logged = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    entry = next(e for e in logged if e["msg"] == "unreadable_layer")
    assert entry["file"] == "geo/broken.usd"
    assert str(container.workdir) not in json.dumps(entry)
    container.miris_cls.assert_not_called()


def test_an_unreadable_single_root_fails_with_its_name(container, monkeypatch, capsys):
    from usd_packager import UnreadableLayerError

    container.s3.add("asset-bucket", f"{ROOT}scene.usda", "x")

    def compute_dependencies(path):
        raise UnreadableLayerError(path, RuntimeError(f"Error in parsing {path}"))

    monkeypatch.setattr(container.main, "compute_dependencies", compute_dependencies)
    monkeypatch.setattr(container.main, "direct_references", lambda path, base: set())

    with pytest.raises(SystemExit) as exited:
        container.run(definition(trigger_rel="scene.usda"))

    assert exited.value.code == 11
    entry = next(json.loads(line) for line in capsys.readouterr().out.splitlines()
                 if '"unreadable_layer"' in line)
    assert entry["file"] == "scene.usda"
    assert str(container.workdir) not in json.dumps(entry)


def test_with_pxr_an_empty_layer_fails_as_unreadable_and_names_it(container, capsys):
    pytest.importorskip("pxr")
    container.s3.add("asset-bucket", f"{ROOT}scene.usda",
                     '#usda 1.0\n(\n    subLayers = [@./geo/empty.usda@]\n)\n')
    container.s3.add("asset-bucket", f"{ROOT}geo/empty.usda", "")

    with pytest.raises(SystemExit) as exited:
        container.run(definition(trigger_rel="scene.usda"))

    assert exited.value.code == 11
    entry = next(json.loads(line) for line in capsys.readouterr().out.splitlines()
                 if '"unreadable_layer"' in line)
    assert entry["file"] == "geo/empty.usda"


def test_with_pxr_an_unreadable_dependency_of_a_lone_root_is_named(container, capsys):
    """A lone candidate is not scanned for the root choice; its dependency check still names the
    unreadable layer it reaches."""
    pytest.importorskip("pxr")
    container.s3.add("asset-bucket", f"{ROOT}scene.usda",
                     '#usda 1.0\n(\n    subLayers = [@./geo/bad.usdc@]\n)\n')
    container.s3.add("asset-bucket", f"{ROOT}geo/bad.usdc", "not a crate file")

    with pytest.raises(SystemExit) as exited:
        container.run(definition(trigger_rel="scene.usda", rootExtensions=[".usda"]))

    assert exited.value.code == 11
    entry = next(json.loads(line) for line in capsys.readouterr().out.splitlines()
                 if '"unreadable_layer"' in line)
    assert entry["file"] == "geo/bad.usdc"


# --- paths ------------------------------------------------------------------------------------


def test_a_prefixed_asset_root_is_listed_and_written_relative_to_the_asset(container, monkeypatch):
    """A bucket registered with baseAssetsPrefix "vams/data/" roots the asset there."""
    root = f"vams/data/{ASSET_ID}/"
    _layered_asset(container, root)
    # A same-named asset outside the prefix must not be read.
    container.s3.add("asset-bucket", f"{ASSET_ID}/other.usda", "x")
    _fake_usd(container, monkeypatch, LAYER_GRAPH)

    container.run(definition(asset_root=root, trigger_rel="geo/geo.usd"))

    assert container.s3.list_calls == [("asset-bucket", root)]
    assert all(key.startswith(root) for _, key, _ in container.s3.downloads)
    assert _metadata_key(container) == f"{METADATA_PREFIX}scene.usda.metadata.json"


def test_an_existing_key_asset_root_without_the_asset_id_works(container):
    root = "imports/2026/pump-site/"
    container.s3.add("asset-bucket", f"{root}pump.usdz", "usdz")

    container.run(definition(asset_root=root, trigger_rel="pump.usdz"))

    assert _metadata_key(container) == f"{METADATA_PREFIX}pump.usdz.metadata.json"


@pytest.mark.parametrize("files,trigger", [
    ({"pump.usdz": 2_000_000}, "pump.usdz"),
    ({"scene.usda": 10, "textures/huge.exr": 2_000_000}, "scene.usda"),
], ids=["single-usdz", "layered-asset"])
def test_an_oversized_listing_fails_before_any_download(container, files, trigger):
    for rel, size in files.items():
        container.s3.add("asset-bucket", f"{ROOT}{rel}", b"x" * size)

    with pytest.raises(SystemExit) as exited:
        container.run(definition(trigger_rel=trigger))

    assert exited.value.code == container.main.EXIT_TOO_LARGE == 4
    assert container.s3.downloads == []
    container.miris_cls.assert_not_called()


def test_a_single_usdz_is_measured_alone(container):
    container.s3.add("asset-bucket", f"{ROOT}pump.usdz", b"x" * 10)
    container.s3.add("asset-bucket", f"{ROOT}reference/scan.e57", b"x" * 2_000_000)

    container.run(definition(trigger_rel="pump.usdz"))

    container.miris.start_upload.assert_called_once()


@pytest.mark.parametrize("stage_overrides", [
    {"databaseId": ""}, {"assetId": ""}, {"assetRoot": {}},
    {"assetRoot": {"bucketName": "asset-bucket", "objectDir": "/"}},
], ids=["no-database", "no-asset", "no-root", "bucket-root"])
def test_a_definition_without_identity_or_root_fails_before_listing(container, stage_overrides):
    with pytest.raises(SystemExit) as exited:
        container.run(definition(**stage_overrides))

    assert exited.value.code == container.main.EXIT_USAGE == 2
    assert container.s3.list_calls == []


# --- tags and content path --------------------------------------------------------------------


def test_the_binding_tags_are_always_written_and_reserved_configured_tags_are_dropped(container, caplog):
    container.s3.add("asset-bucket", f"{ROOT}pump.usdz", "usdz")
    container.s3.add("config-bucket", "cfg.json", json.dumps({"mirisTags": [
        "scan", "vams-asset-someone-else", " VAMS-Database-other", "site-7"]}))

    container.run(definition(inputConfigurationS3Location="s3://config-bucket/cfg.json"))

    assert container.miris.start_upload.call_args.kwargs["tags"] == [
        "vams", f"vams-asset-{ASSET_ID}", "vams-database-db1", "scan", "site-7"]
    dropped = [r for r in caplog.records if r.levelname == "WARNING" and "reserved" in r.getMessage()]
    assert len(dropped) == 2


def test_the_content_path_equals_the_uploaded_key_verbatim(container):
    """Miris resolves materials by content_path, so the temporary-upload key must match it exactly."""
    container.s3.add("asset-bucket", f"{ROOT}scenes/Coral House.usdz", "usdz")

    container.run(definition(trigger_rel="scenes/Coral House.usdz"))

    content_path = container.miris.start_upload.call_args.kwargs["content_path"]
    put = container.temp_s3.put_object.call_args.kwargs
    assert content_path == "Coral House.usdz"
    assert put["Key"] == f"prefix/{content_path}"
    assert put["Bucket"] == "miris-temp"


def test_a_packaged_root_is_uploaded_under_its_usdz_name(container, monkeypatch):
    _layered_asset(container)
    container.s3.files[("asset-bucket", f"{ROOT}Red Chair.usda")] = container.s3.files.pop(
        ("asset-bucket", f"{ROOT}scene.usda"))
    graph = {"Red Chair.usda": LAYER_GRAPH["scene.usda"], "geo/geo.usd": {"mat.usdc"}}
    _fake_usd(container, monkeypatch, graph)

    container.run(definition(trigger_rel="mat.usdc"))

    content_path = container.miris.start_upload.call_args.kwargs["content_path"]
    assert content_path == "Red Chair.usdz"
    assert container.temp_s3.put_object.call_args.kwargs["Key"] == f"prefix/{content_path}"


# --- with pxr ---------------------------------------------------------------------------------


def test_with_pxr_the_root_layer_is_packaged_when_a_sublayer_triggered(container):
    pytest.importorskip("pxr")
    container.s3.add("asset-bucket", f"{ROOT}scene.usda",
                     '#usda 1.0\n(\n    subLayers = [@./geo/geo.usda@]\n)\n')
    container.s3.add("asset-bucket", f"{ROOT}geo/geo.usda", '#usda 1.0\ndef Xform "G" {}\n')

    container.run(definition(trigger_rel="geo/geo.usda"))

    assert container.miris.start_upload.call_args.kwargs["content_path"] == "scene.usdz"
    assert _metadata_key(container) == f"{METADATA_PREFIX}scene.usda.metadata.json"


def test_with_pxr_two_independent_roots_are_ambiguous(container):
    pytest.importorskip("pxr")
    container.s3.add("asset-bucket", f"{ROOT}a.usda", '#usda 1.0\ndef "A" {}\n')
    container.s3.add("asset-bucket", f"{ROOT}b.usda", '#usda 1.0\ndef "B" {}\n')

    with pytest.raises(SystemExit) as exited:
        container.run(definition(trigger_rel="a.usda"))

    assert exited.value.code == 10


def _outside_file(container, rel):
    """A readable file beside the container's working directory, outside the downloaded asset."""
    path = container.workdir.parent / "outside" / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 64)
    return path


def _texture_layer(asset_path):
    return ('#usda 1.0\ndef Material "M" {\n    def Shader "T" {\n'
            '        uniform token info:id = "UsdUVTexture"\n'
            f'        asset inputs:file = @{asset_path}@\n    }}\n}}\n')


@pytest.mark.parametrize("reference", ["absolute", "parent"])
def test_with_pxr_a_texture_outside_the_asset_is_never_packaged(container, capsys, reference):
    pytest.importorskip("pxr")
    secret = _outside_file(container, "secret.png")
    path = str(secret) if reference == "absolute" else "../../outside/secret.png"
    container.s3.add("asset-bucket", f"{ROOT}scene.usda", _texture_layer(path))

    with pytest.raises(SystemExit) as exited:
        container.run(definition(trigger_rel="scene.usda"))

    assert exited.value.code == container.main.EXIT_REFERENCE_OUTSIDE_ASSET == 12
    logged = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    outside = [entry for entry in logged if entry["msg"] == "reference_outside_asset"]
    assert len(outside) == 1 and outside[0]["paths"] == [str(secret.resolve())]
    container.miris_cls.assert_not_called()
    container.temp_s3.put_object.assert_not_called()
    assert not (container.workdir / "scene.usdz").exists()


def test_with_pxr_a_sublayer_outside_the_asset_is_never_packaged(container):
    pytest.importorskip("pxr")
    outside = container.workdir.parent / "outside" / "geo.usda"
    outside.parent.mkdir(parents=True, exist_ok=True)
    outside.write_text('#usda 1.0\ndef Xform "G" {}\n')
    container.s3.add("asset-bucket", f"{ROOT}scene.usda",
                     '#usda 1.0\n(\n    subLayers = [@../../outside/geo.usda@]\n)\n')

    with pytest.raises(SystemExit) as exited:
        container.run(definition(trigger_rel="scene.usda"))

    assert exited.value.code == 12
    container.miris_cls.assert_not_called()


def test_with_pxr_a_texture_inside_the_asset_is_packaged(container):
    pytest.importorskip("pxr")
    import zipfile

    container.s3.add("asset-bucket", f"{ROOT}scene.usda", _texture_layer("./textures/wood.png"))
    container.s3.add("asset-bucket", f"{ROOT}textures/wood.png", b"\x89PNG\r\n\x1a\n" + b"0" * 64)

    container.run(definition(trigger_rel="scene.usda"))

    assert container.miris.start_upload.call_args.kwargs["content_path"] == "scene.usdz"
    with zipfile.ZipFile(container.workdir / "scene.usdz") as packaged:
        assert sorted(packaged.namelist()) == ["scene.usda", "textures/wood.png"]
