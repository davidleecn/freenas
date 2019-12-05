# -*- coding=utf-8 -*-
import logging

logger = logging.getLogger(__name__)

__all__ = ["bitmask_to_set"]


def bitmask_to_set(n, enumeration):
    return {e for e in enumeration if n & e.value}
