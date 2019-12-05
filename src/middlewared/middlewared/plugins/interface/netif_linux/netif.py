# -*- coding=utf-8 -*-
import logging
import os

from .interface import Interface

logger = logging.getLogger(__name__)

__all__ = ["list_interfaces"]


def list_interfaces():
    return {name: Interface(name) for name in os.listdir("/sys/class/net")}
