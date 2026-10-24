"""Session IR: restore an archived session into a harness-neutral shape,
then emit it as a native session for another harness.

Naming: opal-bridge called these canonical/ingest/render; here they are
session_ir/restore/emit. Field semantics unchanged (see schema.py note).
"""
from .schema import Session, moment_from_dict
from .restore_zcode import restore_from_archive
