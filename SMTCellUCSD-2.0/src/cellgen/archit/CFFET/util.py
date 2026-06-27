"""CFFET result writer — delegates to CFET writer until postprocess fork (P7)."""

from src.cellgen.archit.CFET.util import write_cfet_result


def write_cffet_result(*args, **kwargs):
    return write_cfet_result(*args, **kwargs)
