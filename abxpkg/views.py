# pip install django-admin-data-views

from django.http import HttpRequest

from admin_data_views.typing import NestedDict, SectionData, TableContext, ItemContext
from admin_data_views.utils import (
    render_with_table_view,
    render_with_item_view,
    ItemLink,
)

from .binary import Binary


def get_all_binaries() -> list[Binary]:
    """Override this function to implement getting the list of binaries to render"""
    return []


def get_binary(name: str) -> Binary | None:
    """Override this function to implement getting the list of binaries to render"""

    from . import settings

    for binary in settings.get_all_abxpkg_binaries():
        if binary.name == name:
            return binary
    return None


@render_with_table_view
def binaries_list_view(request: HttpRequest, **kwargs) -> TableContext:

    assert getattr(request.user, "is_superuser", False), (
        "Must be a superuser to view configuration settings."
    )

    from . import settings

    rows = {
        "Binary": [],
        "Found Version": [],
        "Provided By": [],
        "Found Abspath": [],
        "Overrides": [],
        "Description": [],
    }

    for binary in settings.get_all_abxpkg_binaries():
        binary = binary.install()

        rows["Binary"].append(ItemLink(binary.name, key=binary.name))
        rows["Found Version"].append(binary.loaded_version)
        rows["Provided By"].append(binary.loaded_binprovider)
        rows["Found Abspath"].append(binary.loaded_abspath)
        rows["Overrides"].append(str(binary.overrides))
        rows["Description"].append(binary.description)

    return TableContext(
        title="Binaries",
        table=rows,
    )


@render_with_item_view
def binary_detail_view(request: HttpRequest, key: str, **kwargs) -> ItemContext:

    assert getattr(request.user, "is_superuser", False), (
        "Must be a superuser to view configuration settings."
    )

    from . import settings

    binary = settings.get_abxpkg_binary(key)

    assert binary, f"Could not find a binary matching the specified name: {key}"

    binary = binary.install()

    fields: NestedDict = {
        "binprovider": str(binary.loaded_binprovider),
        "abspath": str(binary.loaded_abspath),
        "version": str(binary.loaded_version),
        "is_script": str(binary.is_script),
        "is_executable": str(binary.is_executable),
        "is_valid": str(binary.is_valid),
        "overrides": str(binary.overrides),
        "providers": str(binary.binproviders),
    }
    data: list[SectionData] = [
        {
            "name": str(binary.name),
            "description": str(binary.description),
            "fields": fields,
            "help_texts": {},
        },
    ]

    return ItemContext(
        slug=key,
        title=key,
        data=data,
    )
