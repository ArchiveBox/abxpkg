from django.db import models
from django_pydantic_field import SchemaField

from abxpkg import BinProvider, Binary, SemVer


class Dependency(models.Model):
    """Example model implementing fields that contain BinProvider and Binary data"""

    label = models.CharField(max_length=63)

    default_binprovider: BinProvider = SchemaField(default={"name": "env"})

    binaries: list[Binary] = SchemaField(default=[])

    min_version: SemVer = SchemaField(default=(0, 0, 1))

    class Meta:
        verbose_name_plural = "Dependencies"
