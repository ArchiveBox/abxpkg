import sys
from pathlib import Path

from abxpkg.semver import SemVer, bin_version, is_semver_str, semver_to_str


class TestSemVer:
    def test_bin_version_reads_live_python_version_with_custom_args(self):
        version = bin_version(Path(sys.executable), args=("-V",))

        assert version is not None
        assert version == SemVer("{}.{}.{}".format(*sys.version_info[:3]))

    def test_parse_reads_exact_live_bash_banner_version(self):
        # ``bash --version`` banner shape, exercised as a string literal
        # rather than a live subprocess so the parse logic is verified on
        # every platform (``bash`` isn't a first-class Windows binary and
        # the Unix-only providers skip in ``conftest.py`` only covers
        # ``test_bashprovider.py``, not semver tests).
        bash_version_output = (
            "GNU bash, version 5.2.26(1)-release (x86_64-pc-linux-gnu)\n"
            "Copyright (C) 2022 Free Software Foundation, Inc.\n"
            "License GPLv3+: GNU GPL version 3 or later "
            "<http://gnu.org/licenses/gpl.html>\n"
            "\n"
            "This is free software; you are free to change and redistribute it.\n"
            "There is NO WARRANTY, to the extent permitted by law.\n"
        )
        first_line = bash_version_output.splitlines()[0].strip()

        parsed = SemVer.parse(bash_version_output)

        assert parsed is not None
        assert parsed == SemVer(first_line)
        assert parsed.full_text == first_line

    def test_parse_falls_back_across_multiline_banners_up_to_five_lines(self):
        multiline_output = "\n".join(
            [
                "ShellCheck - shell script analysis tool",
                "version: v0.11.0-65-gcd41f79",
                "license: GNU General Public License, version 3",
                "website: https://www.shellcheck.net",
            ],
        )

        parsed = SemVer.parse(multiline_output)

        assert parsed == SemVer("0.11.0")
        assert parsed is not None
        assert parsed.full_text == "version: v0.11.0-65-gcd41f79"

    def test_parse_stops_after_five_lines(self):
        multiline_output = "\n".join(
            [
                "line 1",
                "line 2",
                "line 3",
                "line 4",
                "line 5",
                "version: 1.2.3",
            ],
        )

        assert SemVer.parse(multiline_output) is None

    def test_parse_handles_public_edge_cases(self):
        newer_version = SemVer.parse("24.0.0")
        older_version = SemVer.parse("23.1.0")

        assert newer_version is not None
        assert older_version is not None
        assert SemVer.parse(b"v1.2.3") == SemVer("1.2.3")
        assert SemVer.parse("") is None
        assert SemVer.parse("1.2.3.4") == SemVer("1.2.3")
        assert SemVer.parse("Google Chrome 124.0.6367.208") == SemVer("124.0.6367")
        assert SemVer.parse("2024.04.09") == SemVer("2024.4.9")
        assert SemVer(("1", "2", "3")) == SemVer("1.2.3")
        assert SemVer.parse("Google Chrome") is None
        assert newer_version > older_version

    def test_semver_string_helpers_accept_real_public_inputs(self):
        assert is_semver_str("1.2.3")
        assert not is_semver_str("v1.2.3")
        assert semver_to_str((1, 2, 3)) == "1.2.3"
        assert semver_to_str("4.5.6") == "4.5.6"
