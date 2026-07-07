"""Internal test-support helpers shipped with the package.

These modules exist so distributed executors (e.g. Ray) can import the node
functions used by the test suite from an installed module. They are not part
of the public API and must not be re-exported from ``avalanche.__init__``.
"""
