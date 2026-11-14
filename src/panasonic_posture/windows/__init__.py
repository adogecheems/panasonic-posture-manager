"""Windows platform package.

Responsibilities are split into ``app``, ``hardware``, ``ipc``, ``service``,
``session`` and ``ui`` subpackages.  The package root intentionally contains
no implementation modules so privileged, desktop and UI code cannot be mixed
by path accident.
"""
