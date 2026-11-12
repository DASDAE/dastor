"""dastor: an experimental DAS data server built on DASCore."""

__version__ = "0.0.1"

from dastor.client import Client


def view(source, **kwargs):
    """Show a spool, patch or directory in the viewer; see `dastor.notebook.view`."""
    # Imported here: the client alone needs none of the server.
    from dastor.notebook import view  # noqa: PLC0415

    return view(source, **kwargs)


__all__ = ["Client", "view"]
