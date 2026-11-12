"""dastor: an experimental DAS data server built on DASCore."""

__version__ = "0.0.1"

from dastor.client import Client


def view(source, annotations=None, **kwargs):
    """
    Show a spool, a patch or a directory of DAS files in the viewer.

    The last line of a notebook cell shows it. See `dastor.notebook.Viewer`
    for the parameters.

    Returns
    -------
    dastor.notebook.Viewer
        The viewer, served from a thread of this process.

    Examples
    --------
    >>> import dascore as dc
    >>> import dastor
    >>> viewer = dastor.view(dc.get_example_patch())
    >>> sets = viewer.annotations()  # what was drawn, as DASCore sets
    >>> viewer.close()
    """
    # Imported here: the client alone needs none of the server.
    from dastor.notebook import view  # noqa: PLC0415

    return view(source, annotations, **kwargs)


__all__ = ["Client", "view"]
