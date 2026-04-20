__package__ = "abxpkg"

import inspect
import os
import shutil

from pathlib import Path
from typing import Any, Annotated
from collections.abc import Callable

from platformdirs import user_config_path

from pydantic import TypeAdapter, AfterValidator, BeforeValidator, ValidationError


# Read once at import time. When set, providers that opt into
# ``abxpkg_install_root_default("<provider>")`` default their
# ``install_root`` to ``ABXPKG_LIB_DIR / <provider name>`` (e.g.
# ``<lib>/npm``, ``<lib>/pip``, ``<lib>/gem``). Per-provider
# ``ABXPKG_<NAME>_ROOT`` env vars override this for their own
# provider; explicit constructor kwargs override both.
DEFAULT_LIB_DIR: Path = user_config_path("abx") / "lib"


def abxpkg_install_root_default(provider_name: str) -> Path | None:
    """Resolve a provider's default install root from env vars.

    Precedence (most specific wins):
    1. ``ABXPKG_<PROVIDER>_ROOT`` (e.g. ``ABXPKG_NPM_ROOT``)
    2. ``ABXPKG_LIB_DIR / <provider_name>``
    3. ``None`` — caller's built-in default applies.

    Explicit constructor kwargs (``install_root=`` or the
    provider-specific alias) always override whatever this function
    returns.
    """
    specific = os.environ.get(f"ABXPKG_{provider_name.upper()}_ROOT", "").strip()
    if specific:
        return Path(specific).expanduser().resolve()
    # Read from os.environ directly (not the cached module-level
    # ABXPKG_LIB_DIR) because the CLI sets it at runtime via --lib.
    lib_dir = os.environ.get("ABXPKG_LIB_DIR", "").strip()
    if lib_dir:
        return Path(lib_dir).expanduser().resolve() / provider_name
    return None


def validate_binprovider_name(name: str) -> str:
    assert 1 < len(name) < 16, (
        "BinProvider names must be between 1 and 16 characters long"
    )
    assert name.replace("_", "").isalnum(), (
        "BinProvider names can only contain a-Z0-9 and underscores"
    )
    assert name[0].isalpha(), "BinProvider names must start with a letter"
    return name


BinProviderName = Annotated[str, AfterValidator(validate_binprovider_name)]
# in practice this is essentially BinProviderName: Literal['env', 'pip', 'apt', 'brew', 'npm', 'vendor']
# but because users can create their own BinProviders we can't restrict it to a preset list of literal names


def validate_bin_dir(path: Path) -> Path:
    path = path.expanduser().absolute()
    assert path.resolve()
    assert os.path.isdir(path) and os.access(path, os.R_OK), (
        f"path entries to add to $PATH must be absolute paths to directories {dir}"
    )
    return path


BinDirPath = Annotated[Path, AfterValidator(validate_bin_dir)]


def validate_PATH(PATH: str | list[str]) -> str:
    paths = PATH.split(os.pathsep) if isinstance(PATH, str) else list(PATH)
    assert all(Path(bin_dir) for bin_dir in paths)
    return os.pathsep.join(paths).strip(os.pathsep)


PATHStr = Annotated[str, BeforeValidator(validate_PATH)]


def func_takes_args_or_kwargs(lambda_func: Callable[..., Any]) -> bool:
    """returns True if a lambda func takes args/kwargs of any kind, otherwise false if it's pure/argless"""
    signature = inspect.signature(lambda_func)
    return any(
        parameter.kind
        in (
            inspect.Parameter.POSITIONAL_ONLY,
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
            inspect.Parameter.VAR_POSITIONAL,
            inspect.Parameter.VAR_KEYWORD,
        )
        for parameter in signature.parameters.values()
    )


# @validate_call
def bin_name(bin_path_or_name: str | Path) -> str:
    """
    - wget -> wget
    - /usr/bin/wget -> wget
    - ~/bin/wget -> wget
    - ~/.local/bin/wget -> wget
    - @postlight/parser -> @postlight/parser
    - @postlight/parser@2.2.3 -> @postlight/parser
    - yt-dlp==2024.05.09 -> yt-dlp
    - postlight/parser^2.2.3 -> postlight/parser
    - @postlight/parser@^2.2.3 -> @postlight/parser
    """
    str_bin_name = (
        str(bin_path_or_name)
        .split("^", 1)[0]
        .split("=", 1)[0]
        .split(">", 1)[0]
        .split("<", 1)[0]
    )
    if str_bin_name.startswith("@"):
        # @postlight/parser@^2.2.3 -> @postlight/parser
        str_bin_name = "@" + str_bin_name[1:].split("@", 1)[0]
    else:
        str_bin_name = str_bin_name.split("@", 1)[0]

    assert len(str_bin_name) > 0, "Binary names must be non-empty"
    name = (
        Path(str_bin_name).name if str_bin_name[0] in (".", "/", "~") else str_bin_name
    )
    assert 1 <= len(name) < 64, "Binary names must be between 1 and 63 characters long"
    assert (
        name.replace("-", "")
        .replace("_", "")
        .replace(".", "")
        .replace(" ", "")
        .replace("@", "")
        .replace("/", "")
        .isalnum()
    ), f"Binary name can only contain a-Z0-9-_.@/ and spaces: {name}"
    assert name.replace("@", "")[0].isalpha(), (
        "Binary names must start with a letter or @"
    )
    # print('PARSING BIN NAME', bin_path_or_name, '->', name)
    return name


BinName = Annotated[str, AfterValidator(bin_name)]


# @validate_call
def path_is_file(path: Path | str) -> Path:
    path = Path(path) if isinstance(path, str) else path
    assert os.path.isfile(path) and os.access(path, os.R_OK), (
        f"Path is not a file or we dont have permission to read it: {path}"
    )
    return path


HostExistsPath = Annotated[Path, AfterValidator(path_is_file)]


# @validate_call
def path_is_executable(path: HostExistsPath) -> HostExistsPath:
    assert os.path.isfile(path) and os.access(path, os.X_OK), (
        f"Path is not executable (fix by running chmod +x {path})"
    )
    return path


# @validate_call
def path_is_script(path: HostExistsPath) -> HostExistsPath:
    SCRIPT_EXTENSIONS = (".py", ".js", ".sh")
    assert path.suffix.lower() in SCRIPT_EXTENSIONS, (
        "Path is not a script (does not end in {})".format(", ".join(SCRIPT_EXTENSIONS))
    )
    return path


HostExecutablePath = Annotated[HostExistsPath, AfterValidator(path_is_executable)]


# @validate_call
def path_is_abspath(path: Path) -> Path:
    path = path.expanduser().absolute()  # resolve ~/ -> /home/<username/ and ../../
    assert (
        path.resolve()
    )  # make sure symlinks can be resolved, but dont return resolved link
    return path


HostAbsPath = Annotated[HostExistsPath, AfterValidator(path_is_abspath)]
HostBinPath = Annotated[
    HostExistsPath,
    AfterValidator(path_is_abspath),
]  # removed: AfterValidator(path_is_executable)
# not all bins need to be executable to be bins, some are scripts


# @validate_call
def bin_abspath(
    bin_path_or_name: str | BinName | Path,
    PATH: PATHStr | None = None,
) -> HostBinPath | None:
    assert bin_path_or_name
    if PATH is None:
        PATH = os.environ.get("PATH", "/bin")
    if PATH:
        PATH = str(PATH)
    else:
        return None

    if str(bin_path_or_name).startswith("/"):
        # already a path, get its absolute form
        abspath = Path(bin_path_or_name).expanduser().absolute()
    else:
        # not a path yet, get path using shutil.which
        binpath = shutil.which(bin_path_or_name, mode=os.X_OK, path=PATH)
        # print(bin_path_or_name, PATH.split(':'), binpath, 'GOPINGNGN')
        if not binpath:
            # some bins dont show up with shutil.which (e.g. django-admin.py)
            for path in PATH.split(os.pathsep):
                bin_dir = Path(path)
                # print('BIN_DIR', bin_dir, bin_dir.is_dir())
                if not (os.path.isdir(bin_dir) and os.access(bin_dir, os.R_OK)):
                    # raise Exception(f'Found invalid dir in $PATH: {bin_dir}')
                    continue
                bin_file = bin_dir / bin_path_or_name
                # print(bin_file, path, bin_file.exists(), bin_file.is_file(), bin_file.is_symlink())
                if os.path.isfile(bin_file) and os.access(bin_file, os.R_OK):
                    return bin_file

            return None
        # print(binpath, PATH)
        if str(Path(binpath).parent) not in PATH:
            # print('WARNING, found bin but not in PATH', binpath, PATH)
            # found bin but it was outside our search $PATH
            return None
        abspath = Path(binpath).expanduser().absolute()

    try:
        return TypeAdapter(HostBinPath).validate_python(abspath)
    except ValidationError:
        return None


# @validate_call
def bin_abspaths(
    bin_path_or_name: BinName | Path,
    PATH: PATHStr | None = None,
) -> list[HostBinPath]:
    assert bin_path_or_name

    PATH = PATH or os.environ.get("PATH", "/bin")
    abspaths = []

    if str(bin_path_or_name).startswith("/"):
        # already a path, get its absolute form
        abspaths.append(Path(bin_path_or_name).expanduser().absolute())
    else:
        # not a path yet, get path using shutil.which
        for path in PATH.split(os.pathsep):
            binpath = shutil.which(bin_path_or_name, mode=os.X_OK, path=path)
            if binpath and str(Path(binpath).parent) in PATH:
                abspaths.append(binpath)

    try:
        return TypeAdapter(list[HostBinPath]).validate_python(abspaths)
    except ValidationError:
        return []


################## Types ##############################################

UNKNOWN_SHA256 = "unknown"
UNKNOWN_MTIME = 0
UNKNOWN_EUID = -1


def is_valid_sha256(sha256: str) -> str:
    if sha256 == UNKNOWN_SHA256:
        return sha256
    assert len(sha256) == 64
    assert sha256.isalnum()
    return sha256


Sha256 = Annotated[str, AfterValidator(is_valid_sha256)]


def is_valid_mtime(mtime: int) -> int:
    assert mtime >= 0
    return mtime


MTimeNs = Annotated[int, AfterValidator(is_valid_mtime)]


def is_valid_euid(euid: int) -> int:
    assert euid >= UNKNOWN_EUID
    return euid


EUID = Annotated[int, AfterValidator(is_valid_euid)]


def is_valid_install_args(
    install_args: list[str] | tuple[str, ...] | str,
) -> tuple[str, ...]:
    """Make sure a string is a valid install string for a package manager, e.g. ['yt-dlp', 'ffmpeg']"""
    if isinstance(install_args, str):
        install_args = [install_args]
    assert install_args
    assert all(len(arg) for arg in install_args)
    return tuple(install_args)


def is_name_of_method_on_self(method_name: str) -> str:
    assert (
        method_name.startswith("self.")
        and method_name.replace(".", "").replace("_", "").isalnum()
    )
    return method_name


InstallArgs = Annotated[
    tuple[str, ...] | list[str],
    AfterValidator(is_valid_install_args),
]

SelfMethodName = Annotated[str, AfterValidator(is_name_of_method_on_self)]
