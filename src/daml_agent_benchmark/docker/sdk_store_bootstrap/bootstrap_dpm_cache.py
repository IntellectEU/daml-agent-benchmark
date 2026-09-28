"""Make an SDK that was installed with the old `daml` tool usable by the new `dpm` tool.

Background: Daml has two generations of tooling for installing SDKs and building
packages. The classic `daml` assistant can install any SDK straight from a GitHub
release. Its successor `dpm` instead downloads SDKs from Digital Asset's online
registry — and some SDK versions (like canton's snapshot builds) were never
published there, so `dpm` alone cannot install them.

This script bridges the gap. Both tools ultimately run the same programs (the
`damlc` compiler, the script runner, the code generators); they just keep them in
different folder layouts with different metadata files. So we install the SDK
once with the classic `daml` tool, and then run this script to register it with
dpm: it creates the folders and metadata files dpm expects in its cache, with
symlinks pointing back at the already-installed files. Afterwards `dpm build`
and `dpm damlc test` work with that SDK version as if dpm had installed it —
including fully offline.

Used when building the benchmark's Docker eval image (see the Dockerfile in this
directory's parent). It is a standalone copy of
canton_sandbox._import_classic_sdk_into_dpm_cache with the dpm location made
configurable.
"""
import argparse
from pathlib import Path


def symlink_force(source: Path, destination: Path) -> None:
    if destination.exists() or destination.is_symlink():
        destination.unlink()
    destination.symlink_to(source)


def main() -> None:
    ap = argparse.ArgumentParser()
    # Folder where dpm keeps everything (its "home"). We write into <dpm-home>/cache.
    ap.add_argument("--dpm-home", required=True)
    # Folder of the SDK that the classic `daml` tool installed, e.g. /opt/daml/sdk/<name>.
    # Watch out: `daml install` names this folder after the GitHub RELEASE TAG, which for
    # snapshot builds is not the same string as the SDK version inside it.
    ap.add_argument("--classic-sdk", required=True)
    # The SDK VERSION under which dpm should know this SDK. This must be the version
    # string that projects put in their daml.yaml, because that is what dpm looks up.
    ap.add_argument("--version", required=True)
    args = ap.parse_args()

    cache = Path(args.dpm_home) / "cache"
    classic = Path(args.classic_sdk)
    v = args.version

    def component_root(component: str) -> Path:
        return cache / "components" / component / v

    # dpm's cache has a simple structure. One file lists what an SDK version consists
    # of (the "manifest"), and each part of the SDK (a "component") gets its own
    # folder with a small metadata file plus the component's actual files:
    #
    #   cache/sdk/open-source/<version>.yaml               <- manifest
    #   cache/components/<component>/<version>/component.yaml
    #   cache/components/<component>/<version>/<files...>
    #
    # We create exactly that, but instead of copying the component files we symlink
    # them from the classic install (same files, no duplication).

    # 1. The manifest. This is what makes dpm consider the SDK version "installed";
    # without it, dpm errors with SDK_NOT_INSTALLED. It declares three components,
    # which we create below.
    manifest = cache / "sdk" / "open-source" / f"{v}.yaml"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(
        "apiVersion: digitalasset.com/v1\n"
        "kind: SdkManifest\n"
        "spec:\n"
        "  components:\n"
        "    codegen:\n"
        f"      version: {v}\n"
        "    daml-script:\n"
        f"      version: {v}\n"
        "    damlc:\n"
        f"      version: {v}\n"
        f"  version: {v}\n"
        "  edition: open-source\n",
        encoding="utf-8",
    )

    # 2. The "damlc" component: the Daml compiler. Its component.yaml tells dpm
    # which commands this component provides (`dpm damlc ...` and `dpm build` run
    # the binary listed here) and exports the binary's location so other components
    # can find it.
    damlc_root = component_root("damlc")
    damlc_dist = damlc_root / "damlc-dist-dpm"
    damlc_dist.mkdir(parents=True, exist_ok=True)
    symlink_force(classic / "damlc" / "damlc", damlc_dist / "damlc")
    symlink_force(classic / "damlc" / "resources", damlc_dist / "resources")
    symlink_force(classic / "damlc" / "lib", damlc_dist / "lib")
    (damlc_root / "component.yaml").write_text(
        "apiVersion: digitalasset.com/v1\n"
        "kind: Component\n"
        "spec:\n"
        "  commands:\n"
        "    - path: damlc-dist-dpm/damlc\n"
        "      name: damlc\n"
        "      desc: Compiler and IDE backend for the Daml programming language\n"
        "    - path: damlc-dist-dpm/damlc\n"
        "      name: build\n"
        "      desc: Build a Daml package or project\n"
        '      exec-args: ["build"]\n'
        "  exports:\n"
        "    damlc-binary:\n"
        "      conflict-strategy: fail\n"
        "      paths:\n"
        "        - damlc-dist-dpm/damlc\n",
        encoding="utf-8",
    )

    # 3. The "daml-script" component: everything needed to run Daml Script tests.
    # That is the script runner (in the classic SDK it lives inside the all-in-one
    # daml-sdk.jar), the script-service used by `dpm damlc test`, and the
    # Daml.Script libraries (DARs) that test code imports — one per supported
    # language version. The "dars" export puts those libraries on the dependency
    # path of any package dpm builds.
    ds_root = component_root("daml-script")
    ds_root.mkdir(parents=True, exist_ok=True)
    symlink_force(classic / "daml-sdk" / "daml-sdk.jar", ds_root / "daml-script-binary_distribute.jar")
    symlink_force(classic / "damlc" / "resources" / "script-service.jar", ds_root / "script-service.jar")
    for dar_name in ["daml-script-2.1.dar", "daml-script-2.2.dar", "daml-script-2.dev.dar", "daml-script-2.3-staging.dar"]:
        symlink_force(classic / "daml-libs" / dar_name, ds_root / dar_name)
    (ds_root / "component.yaml").write_text(
        "apiVersion: digitalasset.com/v1\n"
        "kind: Component\n"
        "spec:\n"
        "  jar-commands:\n"
        "    - path: daml-script-binary_distribute.jar\n"
        "      name: script\n"
        "      desc: Daml Script Binary\n"
        "  exports:\n"
        "    dars:\n"
        "      conflict-strategy: extend\n"
        "      paths:\n"
        "        - ./daml-script-2.1.dar\n"
        "        - ./daml-script-2.2.dar\n"
        "        - ./daml-script-2.dev.dar\n"
        "        - ./daml-script-2.3-staging.dar\n"
        "    script-service:\n"
        "      conflict-strategy: fail\n"
        "      paths:\n"
        "        - ./script-service.jar\n",
        encoding="utf-8",
    )

    # 4. The "codegen" component: generates Java/JavaScript bindings from Daml
    # code. The benchmark never uses it, but the manifest above declares it, and
    # dpm refuses to work with an SDK whose declared components are missing.
    cg_root = component_root("codegen")
    cg_root.mkdir(parents=True, exist_ok=True)
    symlink_force(classic / "daml-sdk" / "daml-sdk.jar", cg_root / "binary.jar")
    (cg_root / "component.yaml").write_text(
        "apiVersion: digitalasset.com/v1\n"
        "kind: Component\n"
        "spec:\n"
        "  jar-commands:\n"
        "    - path: binary.jar\n"
        "      name: codegen-java\n"
        "      desc: Daml to Java compiler\n"
        '      jar-args: ["java"]\n'
        "    - path: binary.jar\n"
        "      name: codegen-js\n"
        "      desc: Daml to Javascript compiler\n"
        '      jar-args: ["js"]\n',
        encoding="utf-8",
    )
    print(f"bootstrapped dpm cache for {v} at {cache}")


if __name__ == "__main__":
    main()
