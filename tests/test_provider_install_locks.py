"""Real provider installs sharing cache metadata must not deadlock."""

import json
import subprocess
import sys
from pathlib import Path


def test_parallel_host_and_native_postgres_installs(tmp_path: Path) -> None:
    native = "apt" if sys.platform == "linux" else "brew"
    script = r"""
import json
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from abxpkg import AptProvider, Binary, BrewProvider, EnvProvider

lib = Path(sys.argv[1])
os.environ["ABXPKG_LIB_DIR"] = str(lib)
native = sys.argv[2]
provider_class = AptProvider if native == "apt" else BrewProvider
package = "postgresql" if native == "apt" else "postgresql@17"
binaries = [
    Binary(
        name="postgres",
        binproviders=providers,
        overrides={native: {"install_args": [package]}},
    )
    for providers in (
        [EnvProvider(install_root=lib / "env"), provider_class(install_root=lib / native)],
        [provider_class(install_root=lib / native)],
    )
]
with ThreadPoolExecutor(max_workers=2) as executor:
    installed = list(executor.map(lambda binary: binary.install(), binaries))
resolved = []
for binary in installed:
    assert binary.loaded_abspath and binary.loaded_version
    assert binary.loaded_abspath.is_file()
    probe = subprocess.run([str(binary.loaded_abspath), "--version"], capture_output=True, text=True, check=True)
    assert "PostgreSQL" in probe.stdout
    resolved.append({"abspath": str(binary.loaded_abspath), "version": str(binary.loaded_version)})
print(json.dumps(resolved))
"""
    result = subprocess.run(
        [sys.executable, "-c", script, str(tmp_path / "lib"), native],
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert result.returncode == 0, result.stderr
    binaries = json.loads(result.stdout.splitlines()[-1])
    assert len(binaries) == 2
    assert all(Path(binary["abspath"]).is_file() for binary in binaries)
