"""Cleaning the SDK store removes its example projects and the task answers in it, and nothing else."""

import hashlib
import inspect
import io
import tarfile
import zipfile
from pathlib import Path

import pytest

from daml_agent_benchmark import sdk_store


def test_prune_removes_example_projects_but_keeps_dpm_component(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(sdk_store, "SDK_STORE_ROOT", tmp_path)
    (tmp_path / "daml/sdk/2.10.0/templates/daml-intro-7/daml").mkdir(parents=True)
    (tmp_path / "daml/sdk/2.10.0/templates/daml-intro-7/daml/Intro.daml").write_text("module Intro where\n")
    (tmp_path / "daml/sdk/2.10.0/daml-sdk").mkdir()
    component = tmp_path / "dpm/cache/components/daml-new/3.5.2"
    (component / "daml-new-dpm/resources/daml-intro-test/asset/daml").mkdir(parents=True)
    (component / "daml-new-dpm/resources/daml-intro-test/asset/daml/Asset.daml").write_text("module Asset where\n")
    (component / "daml-new-dpm/lib").mkdir()
    (component / "daml-new-dpm/daml-new").write_text("#!/bin/sh\n")
    (component / "component.yaml").write_text("kind: Component\n")

    removed = sdk_store.prune_project_templates()

    assert [str(p.relative_to(tmp_path)) for p in removed] == [
        "daml/sdk/2.10.0/templates",
        "dpm/cache/components/daml-new/3.5.2/daml-new-dpm/resources/daml-intro-test",
    ]
    assert not (tmp_path / "daml/sdk/2.10.0/templates").exists()
    assert (tmp_path / "daml/sdk/2.10.0/daml-sdk").exists()
    # The component itself, its manifest and its binary stay: dpm needs them to build at all.
    assert (component / "component.yaml").exists()
    assert (component / "daml-new-dpm/daml-new").exists()
    assert (component / "daml-new-dpm/resources").is_dir()
    assert list((component / "daml-new-dpm/resources").iterdir()) == []
    # A second call has nothing left to do.
    assert sdk_store.prune_project_templates() == []


def _zip_bytes(entries: list[tuple[str, bytes, int]]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, data, method in entries:
            info = zipfile.ZipInfo(name, (2024, 1, 1, 0, 0, 0))
            archive.writestr(info, data, compress_type=method)
            # Jar tools write files with no attributes. zipfile would record owner-only permissions.
            info.external_attr = 0
    return buffer.getvalue()


def _zip_headers(data: bytes) -> list[tuple[str, tuple, int]]:
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        return [(info.filename, info.date_time, info.external_attr) for info in archive.infolist()]


def _zip_contents(data: bytes) -> list[tuple[str, int, bytes]]:
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        return [(info.filename, info.compress_type, archive.read(info)) for info in archive.infolist()]


_IOU_SOURCE = b"module Iou where\n\ntemplate Iou\n  with\n    payer: Party\n    owner: Party\n  where\n    signatory payer\n"
_PKG = "CantonExamples-1.0.0-abc"
_ANSWER_DAR_ENTRIES = [
    (f"{_PKG}/{_PKG}.dalf", b"\x00compiled package", zipfile.ZIP_DEFLATED),
    (f"{_PKG}/Iou.daml", _IOU_SOURCE, zipfile.ZIP_DEFLATED),
    (f"{_PKG}/Iou.hi", b"interface", zipfile.ZIP_DEFLATED),
    (f"{_PKG}/Iou.hie", b"hie embeds " + _IOU_SOURCE, zipfile.ZIP_DEFLATED),
    (f"{_PKG}/Swap.daml", b"module Swap where\n", zipfile.ZIP_DEFLATED),
    ("META-INF/MANIFEST.MF", b"Main-Dalf: x\n", zipfile.ZIP_STORED),
]
_OTHER_PKG = "admin-workflow-1.0.0-def"
_OTHER_DAR_ENTRIES = [
    (f"{_OTHER_PKG}/{_OTHER_PKG}.dalf", b"\x00another package", zipfile.ZIP_DEFLATED),
    (f"{_OTHER_PKG}/Ping.daml", b"module Ping where\n", zipfile.ZIP_DEFLATED),
    (f"{_OTHER_PKG}/Ping.hi", b"interface", zipfile.ZIP_DEFLATED),
    ("META-INF/MANIFEST.MF", b"Main-Dalf: y\n", zipfile.ZIP_STORED),
]
# A DAR that depends on the answer package bundles its compiled code under the package's file name.
_DEPENDENT_PKG = "dependent-1.0.0-ghi"
_DEPENDENT_DAR_ENTRIES = [
    (f"{_DEPENDENT_PKG}/{_DEPENDENT_PKG}.dalf", b"\x00dependent package", zipfile.ZIP_DEFLATED),
    (f"{_DEPENDENT_PKG}/{_PKG}.dalf", b"\x00compiled package", zipfile.ZIP_DEFLATED),
    (f"{_DEPENDENT_PKG}/Main.daml", b"module Main where\nimport Iou\n", zipfile.ZIP_DEFLATED),
]
# A zip of sources holds no compiled package, so only the files in it that hold an answer are cut out.
_SOURCES_ZIP_ENTRIES = [
    ("CantonExamples/Iou.daml", _IOU_SOURCE, zipfile.ZIP_DEFLATED),
    ("CantonExamples/Swap.daml", b"module Swap where\n", zipfile.ZIP_STORED),
]


def _synthetic_store(tmp_path: Path, monkeypatch) -> tuple[Path, Path, Path]:
    """A test store: a jar that bundles the answer package among other DARs, a tarball holding
    that jar, and plain answer files outside any archive."""
    store = tmp_path / "store"
    monkeypatch.setattr(sdk_store, "SDK_STORE_ROOT", store)
    answer = tmp_path / "sources/CantonExamples/Iou.daml"
    answer.parent.mkdir(parents=True)
    answer.write_bytes(_IOU_SOURCE)
    jar_bytes = _zip_bytes(
        [
            ("META-INF/MANIFEST.MF", b"Manifest-Version: 1.0\n", zipfile.ZIP_STORED),
            ("com/example/Iou.class", b"\xca\xfe\xba\xbe", zipfile.ZIP_DEFLATED),
            ("CantonExamples.dar", _zip_bytes(_ANSWER_DAR_ENTRIES), zipfile.ZIP_DEFLATED),
            ("admin-workflow.dar", _zip_bytes(_OTHER_DAR_ENTRIES), zipfile.ZIP_DEFLATED),
            ("dependent.dar", _zip_bytes(_DEPENDENT_DAR_ENTRIES), zipfile.ZIP_DEFLATED),
            ("examples-sources.zip", _zip_bytes(_SOURCES_ZIP_ENTRIES), zipfile.ZIP_STORED),
            ("lib/Iou.daml", b"module Lib.Other where\n", zipfile.ZIP_DEFLATED),
            (f"deps/{_PKG}.dalf", b"\x00compiled package", zipfile.ZIP_DEFLATED),
        ]
    )
    jar = store / "daml/sdk/3.4.9/canton/canton.jar"
    jar.parent.mkdir(parents=True)
    jar.write_bytes(jar_bytes)
    blob = store / "dpm/cache/oci-layout/blobs/sha256/0123abcd"
    blob.parent.mkdir(parents=True)
    with tarfile.open(blob, "w:gz") as tarball:
        for name, data in [("lib/canton.jar", jar_bytes), ("lib/CantonExamples.dar", _zip_bytes(_ANSWER_DAR_ENTRIES))]:
            member = tarfile.TarInfo(name)
            member.size = len(data)
            tarball.addfile(member, io.BytesIO(data))
    (store / "dpm/cache/oci-layout/blobs/sha256/unrelated").write_text("{}")
    pkg_db = store / "daml/sdk/3.4.9/pkg-db"
    pkg_db.mkdir()
    for name, data in [("Iou.daml", _IOU_SOURCE), ("Iou.hi", b"interface"), ("Iou.hie", b"hie"), ("Other.daml", b"module Other where\n")]:
        (pkg_db / name).write_bytes(data)
    (store / f"dpm/cache/{_PKG}.dalf").write_bytes(b"\x00compiled package")
    return answer, jar, blob


# Plain files in the store: a copy of the answer with its compiled forms, and the answer package's `.dalf`.
_PLAIN_ANSWERS = {"daml/sdk/3.4.9/pkg-db/Iou.daml", "daml/sdk/3.4.9/pkg-db/Iou.hi", "daml/sdk/3.4.9/pkg-db/Iou.hie", f"dpm/cache/{_PKG}.dalf"}


def _answers_in_jar(prefix: str) -> set[str]:
    return {
        # The answer package is one item: it goes whole, so the files inside it get no items of their own.
        f"{prefix}CantonExamples.dar",
        # A DAR that depends on it goes whole too. Cutting the answer's `.dalf` out of it would break it.
        f"{prefix}dependent.dar",
        f"{prefix}examples-sources.zip!CantonExamples/Iou.daml",
        f"{prefix}deps/{_PKG}.dalf",
        # `lib/Iou.daml` is at a path ending in the target's module path, but holds another module, so it stays.
    }


def test_strip_removes_only_the_answer_and_its_package_and_the_check_follows(tmp_path: Path, monkeypatch) -> None:
    answer, jar, blob = _synthetic_store(tmp_path, monkeypatch)
    original_bytes = jar.read_bytes()
    original = _zip_contents(original_bytes)

    with pytest.raises(RuntimeError, match=r"canton\.jar!CantonExamples\.dar\n"):
        sdk_store.assert_store_archives_free_of_answers([str(answer)])
    found = {(str(f["path"].relative_to(tmp_path / "store")), f["entry"]) for f in sdk_store.find_answers_in_store_archives([str(answer)])}
    assert found == {
        *(("daml/sdk/3.4.9/canton/canton.jar", entry) for entry in _answers_in_jar("")),
        *(("dpm/cache/oci-layout/blobs/sha256/0123abcd", entry) for entry in _answers_in_jar("lib/canton.jar!")),
        ("dpm/cache/oci-layout/blobs/sha256/0123abcd", "lib/CantonExamples.dar"),
        *((path, "") for path in _PLAIN_ANSWERS),
    }

    stripped = sdk_store.strip_answers_from_archives([str(answer)])

    store = tmp_path / "store"
    assert sorted(str(s["path"]) for s in stripped) == sorted([str(jar), str(blob), *(str(store / p) for p in _PLAIN_ANSWERS)])
    assert not any((store / p).exists() for p in _PLAIN_ANSWERS)
    assert (store / "daml/sdk/3.4.9/pkg-db/Other.daml").exists()
    kept = _zip_contents(jar.read_bytes())
    assert [(name, method) for name, method, _ in kept] == [
        ("META-INF/MANIFEST.MF", zipfile.ZIP_STORED),
        ("com/example/Iou.class", zipfile.ZIP_DEFLATED),
        ("admin-workflow.dar", zipfile.ZIP_DEFLATED),
        ("examples-sources.zip", zipfile.ZIP_STORED),
        ("lib/Iou.daml", zipfile.ZIP_DEFLATED),
    ]
    removed = {"CantonExamples.dar", "dependent.dar", f"deps/{_PKG}.dalf"}
    # Every file in the jar that holds no answer is kept byte for byte.
    assert [entry for entry in kept if entry[0] != "examples-sources.zip"] == [
        entry for entry in original if entry[0] not in removed and entry[0] != "examples-sources.zip"
    ]
    assert _zip_headers(jar.read_bytes()) == [h for h in _zip_headers(original_bytes) if h[0] not in removed]
    sources = next(entry[2] for entry in original if entry[0] == "examples-sources.zip")
    assert _zip_contents(kept[3][2]) == [entry for entry in _zip_contents(sources) if not entry[0].endswith("Iou.daml")]
    with tarfile.open(blob) as tarball:
        assert tarball.getnames() == ["lib/canton.jar"]
        jar_in_blob = tarball.extractfile("lib/canton.jar").read()  # type: ignore[union-attr]
    assert _zip_contents(jar_in_blob) == kept
    sdk_store.assert_store_archives_free_of_answers([str(answer)])

    # A second run finds nothing and leaves the archives untouched.
    before = {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in (jar, blob)}
    assert sdk_store.strip_answers_from_archives([str(answer)]) == []
    assert {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in (jar, blob)} == before


def test_the_check_refuses_a_store_archive_that_is_an_answer_dar(tmp_path: Path, monkeypatch) -> None:
    answer, _jar, _blob = _synthetic_store(tmp_path, monkeypatch)
    dar = tmp_path / "store/daml/sdk/3.4.9/daml-libs/CantonExamples.dar"
    dar.parent.mkdir(parents=True)
    dar.write_bytes(_zip_bytes(_ANSWER_DAR_ENTRIES))
    original = dar.read_bytes()
    sdk_store.strip_answers_from_archives([str(answer)])

    # Nothing can be cut out of a DAR that is itself the answer, so it is left as it is and the check refuses it.
    assert dar.read_bytes() == original
    with pytest.raises(RuntimeError, match=r"(?m)daml-libs/CantonExamples\.dar$"):
        sdk_store.assert_store_archives_free_of_answers([str(answer)])
    before = (dar.read_bytes(), dar.stat().st_mtime_ns)
    assert sdk_store.strip_answers_from_archives([str(answer)]) == []
    assert (dar.read_bytes(), dar.stat().st_mtime_ns) == before


def test_the_archive_scan_is_cached_by_size_and_mtime(tmp_path: Path, monkeypatch) -> None:
    answer, jar, _blob = _synthetic_store(tmp_path, monkeypatch)
    assert sdk_store.find_answers_in_store_archives([str(answer)])
    assert (tmp_path / "store" / "archive-module-entries.json").exists()

    def unexpected_scan(path: Path) -> dict:
        raise AssertionError(f"rescanned {path}")

    monkeypatch.setattr(sdk_store, "_archive_module_entries", unexpected_scan)
    assert len(sdk_store.find_answers_in_store_archives([str(answer)])) == 13
    # A changed file is scanned again.
    jar.write_bytes(_zip_bytes([("Other.daml", b"module Other where\n", zipfile.ZIP_DEFLATED)]))
    with pytest.raises(AssertionError, match="rescanned .*canton.jar"):
        sdk_store.find_answers_in_store_archives([str(answer)])


def _scan_code_digest() -> str:
    """A digest of the code whose output the scan cache stores: its constants and the four functions."""
    parts = [repr((sdk_store.MODULE_SUFFIXES, sdk_store.ZIP_MAGIC, sdk_store.GZIP_MAGIC, sdk_store._MAX_ARCHIVE_NESTING, sdk_store._CACHED_SOURCE_SUFFIX))]
    functions = (sdk_store._source_text, sdk_store._zip_module_entries, sdk_store._tar_module_entries, sdk_store._archive_module_entries)
    parts += [inspect.getsource(f) for f in functions]
    return hashlib.sha256("\0".join(parts).encode("utf-8")).hexdigest()[:16]


def test_the_scan_version_is_raised_when_the_scan_changes() -> None:
    # On failure: raise `_ARCHIVE_SCAN_VERSION` in sdk_store.py and pin the new pair here.
    assert (sdk_store._ARCHIVE_SCAN_VERSION, _scan_code_digest()) == (3, "03062423001d376b")


def test_a_strip_that_fails_leaves_no_temporary_file(tmp_path: Path, monkeypatch) -> None:
    answer, jar, _blob = _synthetic_store(tmp_path, monkeypatch)
    original = jar.read_bytes()

    def failing_strip(source: object, destination: Path, remove: set[str]) -> None:
        destination.write_bytes(b"partial")
        raise OSError("disk full")

    monkeypatch.setattr(sdk_store, "_strip_zip", failing_strip)
    with pytest.raises(OSError, match="disk full"):
        sdk_store.strip_answers_from_archives([str(answer)])
    assert jar.read_bytes() == original
    assert not list((tmp_path / "store").rglob("*.strip-tmp"))
