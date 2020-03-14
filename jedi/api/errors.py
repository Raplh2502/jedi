"""
This file is about errors in Python files and not about exception handling in
Jedi.
"""


def parso_to_jedi_errors(grammar, module_node):
    return [SyntaxError(e) for e in grammar.iter_errors(module_node)]


class SyntaxError(object):
    def __init__(self, parso_error):
        self._parso_error = parso_error

    @property
    def line(self):
        """The line where the error starts (starting with 1)."""
        return self._parso_error.start_pos[0]

    @property
    def column(self):
        """The column where the error starts (starting with 0)."""
        return self._parso_error.start_pos[1]

    @property
    def until_line(self):
        """The line where the error ends (starting with 1)."""
        return self._parso_error.end_pos[0]

    @property
    def until_column(self):
        """The column where the error ends (starting with 0)."""
        return self._parso_error.end_pos[1]

    def __repr__(self):
        return '<%s from=%s to=%s>' % (
            self.__class__.__name__,
            self._parso_error.start_pos,
            self._parso_error.end_pos,
        )
