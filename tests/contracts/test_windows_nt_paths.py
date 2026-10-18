"""The NT handle-relative path API must reject path-looking child names."""

import pytest

from workgate.platform.windows.nt_relative import open_child, rename_child


@pytest.mark.parametrize(
    "name",
    [
        "",
        ".",
        "..",
        r"folder\escape",
        "sub/escape",
        "C:secret",
        "file:ads",
        "NUL",
        "CON.txt",
        "trailing.",
        "trailing ",
        "COM1",
        "LPT9",
        "name" * 10000,
    ],
)
def test_nt_relative_open_rejects_unsafe_leaf_names_without_invoking_windows(
    name,
):
    with pytest.raises(ValueError):
        open_child(42, name, access=0x80, directory=False)


@pytest.mark.parametrize(
    "name",
    ["", ".", "..", r"a\file", "a/file", "file:ads", "CON", "name."],
)
def test_nt_relative_rename_rejects_unsafe_leaf_names_without_invoking_windows(
    name,
):
    with pytest.raises(ValueError):
        rename_child(41, 42, name)
