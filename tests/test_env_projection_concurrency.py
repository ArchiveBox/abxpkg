from concurrent.futures import ThreadPoolExecutor
import subprocess

from abxpkg import Binary, EnvProvider, NodeProvider


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
