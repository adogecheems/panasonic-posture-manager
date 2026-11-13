"""Machine profiles plus Linux hardware discovery.

``profile`` contains the shared profile schema/parser, but its default DMI and
directory lookup are Linux-specific.  ``iio``, ``sysfs`` and ``probe`` are
Linux-only; Windows hardware discovery lives under ``panasonic_posture.windows``.
"""
