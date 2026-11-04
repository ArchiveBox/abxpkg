from abxpkg.binary import Binary


def get_all_binaries() -> list[Binary]:
    """Override this function to implement getting the list of binaries to render"""
    return [
        Binary(name="bash"),
        Binary(name="python"),
        Binary(name="brew"),
        Binary(name="git"),
    ]


def get_binary(name: str) -> Binary | None:
    """Override this function to implement getting the list of binaries to render"""

    from abxpkg import settings

    for binary in settings.get_all_abxpkg_binaries():
        if binary.name == name:
            return binary
    return None
