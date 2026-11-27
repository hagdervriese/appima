"""Pluggable package format handlers for appima."""

from appima.formats.appimage import AppImageFormat
from appima.formats.archive import ArchiveFormat
from appima.formats.base import PackageFormat

__all__ = ["PackageFormat", "AppImageFormat", "ArchiveFormat"]
