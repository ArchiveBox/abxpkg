from concurrent.futures import ThreadPoolExecutor
import multiprocessing
import subprocess

from abxpkg import Binary, ChromeWebstoreProvider, EnvProvider, NodeProvider


def _discover_or_project_extension(binary, lib_dir, barrier, publisher):
    for index in range(32):
        provider = EnvProvider(
            install_root=lib_dir / f"env-{index}",
            PATH="/usr/bin:/bin",
        )
        barrier.wait(timeout=30)
        if publisher:
            projected = provider.project_binary(binary, binary.name)
            assert projected is not None and projected.is_file(), projected
        else:
            for _ in range(8):
                provider.load(binary.name, no_cache=True)


def test_concurrent_discovery_preserves_extension_publication(tmp_path, test_machine):
    test_machine.require_tool("node")
    installed = Binary(
        name="ublock",
        binproviders=[ChromeWebstoreProvider(install_root=tmp_path / "chromewebstore")],
        overrides={
            "chromewebstore": {
                "install_args": ["ddkjiahejlhfcafbddmgiahcphecmpfh", "--name=ublock"],
            },
        },
    ).install()
    assert installed.loaded_abspath is not None and installed.loaded_abspath.is_file()

    # Actual separate processes can discover a new readable extension link
    # while its publishing process still holds the owner-metadata lock.
    context = multiprocessing.get_context("spawn")
    barrier = context.Barrier(9)
    processes = [
        context.Process(
            target=_discover_or_project_extension,
            args=(installed, tmp_path, barrier, index == 0),
        )
        for index in range(9)
    ]
    try:
        for process in processes:
            process.start()
        for process in processes:
            process.join(timeout=60)
        assert [process.exitcode for process in processes] == [0] * len(processes)
    finally:
        for process in processes:
            if process.is_alive():
                process.terminate()
                process.join(timeout=10)

    for index in range(32):
        provider = EnvProvider(install_root=tmp_path / f"env-{index}")
        loaded = provider.load("ublock", no_cache=True)
        assert loaded is not None
        assert loaded.loaded_abspath.resolve(
            strict=True,
        ) == installed.loaded_abspath.resolve(strict=True)
        assert loaded.loaded_version == installed.loaded_version
        assert loaded.loaded_binprovider.name == "chromewebstore"


def test_concurrent_host_discovery_preserves_explicit_runtime_projection(tmp_path):
    host = Binary(
        name="node",
        binproviders=[NodeProvider(install_root=tmp_path / "host" / "node")],
    ).install()
    assert host.loaded_abspath is not None
    host_node = host.loaded_abspath.resolve(strict=True)
    managed = Binary(
        name="node",
        binproviders=[NodeProvider(install_root=tmp_path / "managed" / "node")],
    ).install()
    assert managed.loaded_abspath is not None
    assert managed.loaded_abspath.resolve() != host_node

    # Browser dependency installation pins Node while other installers discover
    # their runtime concurrently. Discovery must not replace that published
    # choice with the host Node (22 in CI versus the required managed 24).
    for index in range(8):
        env_root = tmp_path / str(index) / "env"
        projection = EnvProvider(install_root=env_root, PATH=str(host_node.parent))
        discovery = EnvProvider(install_root=env_root, PATH=str(host_node.parent))
        with ThreadPoolExecutor(max_workers=2) as pool:
            projected = pool.submit(projection.project_binary, managed, "node")
            discovered = pool.submit(discovery.load, "node")
            assert projected.result() == env_root / "bin" / "node"
            assert discovered.result() is not None

        node = env_root / "bin" / "node"
        assert node.resolve(strict=True) == managed.loaded_abspath.resolve(strict=True)
        assert (
            subprocess.check_output([str(node), "--version"], text=True).strip()
            == f"v{managed.loaded_version}"
        )
        reloaded = EnvProvider(install_root=env_root, PATH=str(host_node.parent)).load(
            "node",
            no_cache=True,
        )
        assert reloaded is not None
        assert reloaded.loaded_version == managed.loaded_version
        assert reloaded.loaded_binprovider is not None
        assert reloaded.loaded_binprovider.name == "node"
