from __future__ import annotations

__package__ = "abxpkg"

import re
import subprocess
from collections import namedtuple

from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    from .base_types import HostBinPath


def is_semver_str(semver: Any) -> bool:
    if isinstance(semver, str):
        return semver.count(".") == 2 and semver.replace(".", "").isdigit()
    return False


def semver_to_str(semver: tuple[int, int, int] | str) -> str:
    if isinstance(semver, (list, tuple)):
        return ".".join(str(chunk) for chunk in semver)
    if is_semver_str(semver):
        return semver
    raise ValueError(f"Tried to convert invalid SemVer: {semver}")


SemVerTuple = namedtuple("SemVerTuple", ("major", "minor", "patch"), defaults=(0, 0, 0))
SemVerParsableTypes = bytes | str | tuple[str | int, ...] | list[str | int]


class SemVer(SemVerTuple):
    if TYPE_CHECKING:
        full_text: str | None = ""

    def __new__(cls, *args, full_text=None, **kwargs):
        # '1.1.1'
        if len(args) == 1 and is_semver_str(args[0]):
            result = SemVer.parse(args[0])

        # ('1', '2', '3')
        elif len(args) == 1 and isinstance(args[0], (tuple, list)):
            result = SemVer.parse(args[0])

        # (1, '2', None)
        elif not all(isinstance(arg, (int, type(None))) for arg in args):
            result = SemVer.parse(args)

        # (None)
        elif all(chunk in ("", 0, None) for chunk in (*args, *kwargs.values())):
            result = None

        # 1, 2, 3
        else:
            result = SemVerTuple.__new__(cls, *args, **kwargs)

        if result is not None:
            # add first line as extra hidden metadata so it can be logged without having to re-run version cmd
            result.full_text = full_text or str(result)
        return result

    @classmethod
    def parse(cls, version_stdout: SemVerParsableTypes) -> SemVer | None:
        """
        parses a version tag string formatted like into (major, minor, patch) ints
        'Google Chrome 124.0.6367.208'             -> (124, 0, 6367)
        'GNU Wget 1.24.5 built on darwin23.2.0.'   -> (1, 24, 5)
        'curl 8.4.0 (x86_64-apple-darwin23.0) ...' -> (8, 4, 0)
        '2024.04.09'                               -> (2024, 4, 9)

        """
        # print('INITIAL_VALUE', type(version_stdout).__name__, version_stdout)

        if isinstance(version_stdout, (tuple, list)):
            version_stdout = ".".join(str(chunk) for chunk in version_stdout)
        elif isinstance(version_stdout, bytes):
            version_stdout = version_stdout.decode()
        elif not isinstance(version_stdout, str):
            version_stdout = str(version_stdout)

        # no text to work with, return None immediately
        if not version_stdout.strip():
            # raise Exception('Tried to parse semver from empty version output (is binary installed and available?)')
            return None

        def just_numbers(col):
            return ".".join(
                [
                    chunk
                    for chunk in re.split(r"[\D]", col.lower().strip("v"), maxsplit=10)
                    if chunk.isdigit()
                ][:3],
            )  # split on any non-num character e.g. 5.2.26(1)-release -> ['5', '2', '26', '1', '', '', ...]

        def contains_semver(col):
            return (
                col.count(".") in (1, 2, 3)
                and all(
                    chunk.isdigit() for chunk in col.split(".")[:3]
                )  # first 3 chunks can only be nums
            )

        for line in (
            line.strip() for line in version_stdout.splitlines()[:5] if line.strip()
        ):
            version_columns = list(
                filter(contains_semver, map(just_numbers, line.split()[:10])),
            )
            if version_columns:
                first_version_tuple = version_columns[0].split(".", 3)[:3]
                return cls(
                    *(int(chunk) for chunk in first_version_tuple),
                    full_text=line,
                )

        return None

    def __str__(self):
        return ".".join(str(chunk) for chunk in self)

    # Not needed as long as we dont stray any further from a basic NamedTuple
    # if we start overloading more methods or it becomes a fully custom type, then we probably need this:
    # @classmethod
    # def __get_pydantic_core_schema__(cls, source: Type[Any], handler: GetCoreSchemaHandler) -> core_schema.CoreSchema:
    #     default_schema = handler(source)
    #     return core_schema.no_info_after_validator_function(
    #         cls.parse,
    #         default_schema,
    #         serialization=core_schema.plain_serializer_function_ser_schema(
    #             lambda semver: str(semver),
    #             info_arg=False,
    #             return_schema=core_schema.str_schema(),
    #         ),
    #     )


# @validate_call
def bin_version(bin_path: HostBinPath, args=("--version",)) -> SemVer | None:
    return SemVer(
        subprocess.run(
            [str(bin_path), *args],
            stdout=subprocess.PIPE,
            text=True,
        ).stdout.strip(),
    )
