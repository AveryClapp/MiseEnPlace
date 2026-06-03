"""User-facing errors.

A MiseError is something the user can fix (missing key, bad URL, unknown
channel). It is caught at the CLI boundary and printed cleanly. Anything else
is a bug and is allowed to raise a normal traceback.
"""


class MiseError(Exception):
    pass
