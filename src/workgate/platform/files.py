"""Fail closed for native private filesystem security capabilities."""

from .host import HostOS, host_os


def require_private_file_security() -> None:
    """Do not substitute chmod mode bits for a Windows user-only DACL."""
    if host_os() is HostOS.WINDOWS:
        raise PermissionError(
            "Private Windows files require a verified user-only ACL backend "
            "(workgate issue #383); POSIX chmod is not sufficient"
        )
