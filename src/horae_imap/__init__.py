# SPDX-License-Identifier: Apache-2.0
"""The IMAP Horae plugin: a read-only mail source with rules-first extraction."""

from horae_imap.plugin import COMMAND, NAME, build, manifest

__all__ = ["COMMAND", "NAME", "build", "manifest"]
