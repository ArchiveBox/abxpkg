"""End-to-end coverage for ``ABXPKG_LIB_DIR``.

Because the constant is read once at module import time in
``abxpkg.base_types``, every assertion has to run inside a fresh
subprocess with the env var pre-set; that matches how a user would
actually invoke abxpkg from their own process.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import textwrap
from pathlib import Path

import pytest


def _run_with_lib_dir(
    lib_dir_value: str,
    script: str,
    *,
    extra_env: dict[str, str] | None = None,
    cwd: Path | str | None = None,
) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["ABXPKG_LIB_DIR"] = lib_dir_value
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        env=env,
        cwd=str(cwd) if cwd is not None else None,
    )


class TestAbxPkgLibDir:
    def test_unset_leaves_library_constant_none(self):
        proc = subprocess.run(
            [
                sys.executable,
                "-c",
                "import os; os.environ.pop('ABXPKG_LIB_DIR', None);"
                "from abxpkg.base_types import abxpkg_install_root_default;"
                "print(abxpkg_install_root_default('pip'))",
            ],
            capture_output=True,
            text=True,
        )
        assert proc.returncode == 0, proc.stderr
        assert proc.stdout.strip() == "None"

    def test_empty_string_is_treated_as_unset(self):
        proc = _run_with_lib_dir(
            "",
            "from abxpkg.base_types import abxpkg_install_root_default; print(abxpkg_install_root_default('pip'))",
        )
        assert proc.returncode == 0, proc.stderr
        assert proc.stdout.strip() == "None"

    @pytest.mark.parametrize(
        "lib_dir_value",
        # ``/tmp/abxlib`` is a POSIX literal; on Windows ``Path(...).resolve()``
        # would anchor it to the system drive (``C:``) while the test runs
        # from the runner's work drive (``D:``), causing a drive-mismatch
        # assertion failure. Pick the OS-appropriate temp dir instead.
        ["./lib", "~/.config/abx/lib", str(Path(tempfile.gettempdir()) / "abxlib")],
    )
    def test_all_path_formats_resolve_across_every_provider(
        self,
        lib_dir_value,
        tmp_path,
    ):
        script = textwrap.dedent(
            """
            import json, os
            from pathlib import Path
            from abxpkg import ALL_PROVIDERS

            _lib = os.environ.get("ABXPKG_LIB_DIR", "").strip()
            lib_dir = Path(_lib).expanduser().resolve() if _lib else None
            payload = {
                "lib_dir": str(lib_dir) if lib_dir else None,
                "fields": {},
            }
            for cls in ALL_PROVIDERS:
                instance = cls()
                if instance.install_root is None:
                    continue
                payload["fields"][instance.name] = str(instance.install_root)
            print(json.dumps(payload))
            """,
        )

        proc = _run_with_lib_dir(lib_dir_value, script, cwd=tmp_path)
        assert proc.returncode == 0, proc.stderr
        payload = json.loads(proc.stdout.strip().splitlines()[-1])

        resolved_lib_dir = Path(payload["lib_dir"])
        assert resolved_lib_dir.is_absolute(), (
            f"ABXPKG_LIB_DIR={lib_dir_value!r} did not resolve to an absolute "
            f"path; got {resolved_lib_dir}"
        )
        if lib_dir_value == "./lib":
            assert resolved_lib_dir == (tmp_path / "lib").resolve()
        elif lib_dir_value == "~/.config/abx/lib":
            assert resolved_lib_dir == Path("~/.config/abx/lib").expanduser().resolve()
        else:
            assert resolved_lib_dir == Path(lib_dir_value).resolve()

        for provider_name, field_value in payload["fields"].items():
            assert Path(field_value) == resolved_lib_dir / provider_name, (
                f"{provider_name}: expected {resolved_lib_dir / provider_name}, got {field_value}"
            )

    def test_explicit_install_root_kwarg_overrides_env_var(self, tmp_path):
        explicit_root = tmp_path / "explicit-override"
        script = textwrap.dedent(
            f"""
            import json
            from pathlib import Path
            from abxpkg import (
                CargoProvider, DenoProvider, NpmProvider, PipProvider,
                PlaywrightProvider, PuppeteerProvider, UvProvider,
            )

            explicit = Path({str(explicit_root)!r})
            payload = {{
                "npm": str(NpmProvider(install_root=explicit).install_root),
                "pip": str(PipProvider(install_root=explicit).install_root),
                "uv": str(UvProvider(install_root=explicit).install_root),
                "cargo": str(CargoProvider(install_root=explicit).install_root),
                "deno": str(DenoProvider(install_root=explicit).install_root),
                "puppeteer": str(PuppeteerProvider(install_root=explicit).install_root),
                "playwright": str(PlaywrightProvider(install_root=explicit).install_root),
            }}
            print(json.dumps(payload))
            """,
        )
        proc = _run_with_lib_dir("/tmp/should-be-ignored", script)
        assert proc.returncode == 0, proc.stderr
        payload = json.loads(proc.stdout.strip().splitlines()[-1])
        for provider_name, value in payload.items():
            assert Path(value) == explicit_root, (
                f"{provider_name}: explicit install_root kwarg did not override "
                f"ABXPKG_LIB_DIR; got {value}"
            )

    def test_provider_specific_alias_kwarg_overrides_env_var(self, tmp_path):
        explicit_npm = tmp_path / "custom-npm"
        explicit_uv = tmp_path / "custom-uv"
        script = textwrap.dedent(
            f"""
            import json
            from pathlib import Path
            from abxpkg import NpmProvider, UvProvider

            print(json.dumps({{
                "npm": str(NpmProvider(install_root=Path({str(explicit_npm)!r})).install_root),
                "uv": str(UvProvider(install_root=Path({str(explicit_uv)!r})).install_root),
            }}))
            """,
        )
        proc = _run_with_lib_dir("/tmp/should-be-ignored", script)
        assert proc.returncode == 0, proc.stderr
        payload = json.loads(proc.stdout.strip().splitlines()[-1])
        assert Path(payload["npm"]) == explicit_npm
        assert Path(payload["uv"]) == explicit_uv

    def test_per_provider_root_env_var_overrides_abxpkg_lib_dir(self, tmp_path):
        lib_dir = tmp_path / "lib"
        script = textwrap.dedent(
            """
            import json
            from abxpkg import ALL_PROVIDERS

            payload = {}
            for cls in ALL_PROVIDERS:
                instance = cls()
                if instance.install_root is None:
                    continue
                payload[instance.name] = str(instance.install_root)
            print(json.dumps(payload))
            """,
        )

        probe = _run_with_lib_dir(str(lib_dir), script)
        assert probe.returncode == 0, probe.stderr
        default_payload = json.loads(probe.stdout.strip().splitlines()[-1])
        per_provider_dirs = {
            name: tmp_path / f"custom-{name}" for name in default_payload
        }
        env_overrides = {
            f"ABXPKG_{name.upper()}_ROOT": str(path)
            for name, path in per_provider_dirs.items()
        }

        proc = _run_with_lib_dir(str(lib_dir), script, extra_env=env_overrides)
        assert proc.returncode == 0, proc.stderr
        payload = json.loads(proc.stdout.strip().splitlines()[-1])

        for name, expected in per_provider_dirs.items():
            assert Path(payload[name]) == expected.resolve(), (
                f"{name}: ABXPKG_{name.upper()}_ROOT did not win over "
                f"ABXPKG_LIB_DIR; expected {expected.resolve()}, got {payload[name]}"
            )

    def test_per_provider_root_alone_resolves_correctly(self, tmp_path):
        explicit_npm = tmp_path / "npm-only"
        script = textwrap.dedent(
            """
            import json
            from abxpkg import NpmProvider, PipProvider

            print(json.dumps({
                "npm": str(NpmProvider().install_root),
                "pip": str(PipProvider().install_root) if PipProvider().install_root else None,
            }))
            """,
        )
        env = os.environ.copy()
        env.pop("ABXPKG_LIB_DIR", None)
        env["ABXPKG_NPM_ROOT"] = str(explicit_npm)
        env.pop("ABXPKG_PIP_ROOT", None)
        proc = subprocess.run(
            [sys.executable, "-c", script],
            capture_output=True,
            text=True,
            env=env,
        )
        assert proc.returncode == 0, proc.stderr
        payload = json.loads(proc.stdout.strip().splitlines()[-1])
        assert Path(payload["npm"]) == explicit_npm.resolve()
        assert payload["pip"] is None

    def test_real_installs_land_under_abxpkg_lib_dir(self, test_machine):
        test_machine.require_tool("node")
        test_machine.require_tool("npm")
        test_machine.require_tool("uv")
        test_machine.require_tool("pnpm")
        test_machine.require_tool("yarn")
        test_machine.require_tool("bun")
        test_machine.require_tool("deno")
        test_machine.require_tool("cargo")
        test_machine.require_tool("gem")

        with tempfile.TemporaryDirectory() as tmp_dir:
            lib_dir = Path(tmp_dir) / "abx-lib"
            script = textwrap.dedent(
                """
                import json, os
                from pathlib import Path
                from abxpkg import (
                    BunProvider, CargoProvider, DenoProvider, GemProvider,
                    NpmProvider, PipProvider, PnpmProvider, UvProvider,
                    YarnProvider,
                )

                _lib = os.environ.get("ABXPKG_LIB_DIR", "").strip()
                results = {"lib_dir": str(Path(_lib).expanduser().resolve()) if _lib else "None"}

                pip = PipProvider(postinstall_scripts=True, min_release_age=0)
                results["pip"] = str(pip.install_root)
                pip.install("cowsay")

                uv = UvProvider(postinstall_scripts=True, min_release_age=0)
                results["uv"] = str(uv.install_root)
                uv.install("cowsay")

                npm = NpmProvider(postinstall_scripts=True, min_release_age=0)
                results["npm"] = str(npm.install_root)
                npm.install("cowsay")

                pnpm = PnpmProvider(postinstall_scripts=True, min_release_age=0)
                results["pnpm"] = str(pnpm.install_root)
                pnpm.install("cowsay")

                yarn = YarnProvider(postinstall_scripts=True, min_release_age=0)
                results["yarn"] = str(yarn.install_root)
                yarn.install("cowsay")

                bun = BunProvider(postinstall_scripts=True, min_release_age=0)
                results["bun"] = str(bun.install_root)
                bun.install("cowsay")

                deno = DenoProvider(postinstall_scripts=True, min_release_age=0)
                results["deno"] = str(deno.install_root)
                deno.install("cowsay")

                cargo = CargoProvider()
                results["cargo"] = str(cargo.install_root)
                cargo.get_provider_with_overrides(
                    overrides={"loc": {"install_args": ["loc"]}},
                ).install("loc")

                gem = GemProvider()
                results["gem"] = str(gem.install_root)
                gem.get_provider_with_overrides(
                    overrides={"lolcat": {"install_args": ["lolcat"]}},
                ).install("lolcat")

                print(json.dumps(results))
                """,
            )

            proc = _run_with_lib_dir(str(lib_dir), script)
            assert proc.returncode == 0, (
                f"Real-install script failed under ABXPKG_LIB_DIR={lib_dir}:\n"
                f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
            )
            payload = json.loads(proc.stdout.strip().splitlines()[-1])

            assert Path(payload["lib_dir"]) == lib_dir.resolve()
            for provider_name in (
                "pip",
                "uv",
                "npm",
                "pnpm",
                "yarn",
                "bun",
                "deno",
                "cargo",
                "gem",
            ):
                reported = Path(payload[provider_name])
                assert reported == lib_dir.resolve() / provider_name, (
                    f"{provider_name}: expected {lib_dir.resolve() / provider_name}, got {reported}"
                )
                assert reported.exists()
                assert reported.is_dir()

            top_level_subdirs = {
                child.name for child in lib_dir.iterdir() if child.is_dir()
            }
            assert {
                "pip",
                "uv",
                "npm",
                "pnpm",
                "yarn",
                "bun",
                "deno",
                "cargo",
                "gem",
            }.issubset(top_level_subdirs)
