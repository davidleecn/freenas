# -*- coding=utf-8 -*-
import glob
import logging
import os

logger = logging.getLogger(__name__)

__all__ = ["globunlink"]


def globunlink(s):
    for f in glob.glob(s):
        os.unlink(f)
