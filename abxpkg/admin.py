from types import MethodType

from django.contrib import admin


def register_admin_views(admin_site: admin.AdminSite):
    """register the django-admin-data-views defined in settings.ADMIN_DATA_VIEWS"""

    from admin_data_views.admin import (
        get_app_list,
        admin_data_index_view,
        get_admin_data_urls,
        get_urls,
    )

    setattr(admin_site, "get_app_list", MethodType(get_app_list, admin_site))
    setattr(
        admin_site,
        "admin_data_index_view",
        MethodType(admin_data_index_view, admin_site),
    )
    setattr(
        admin_site,
        "get_admin_data_urls",
        MethodType(get_admin_data_urls, admin_site),
    )
    setattr(
        admin_site,
        "get_urls",
        MethodType(get_urls(admin_site.get_urls), admin_site),
    )

    return admin_site


register_admin_views(admin.site)
