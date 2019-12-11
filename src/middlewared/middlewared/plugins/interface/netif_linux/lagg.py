# -*- coding=utf-8 -*-
import enum
import logging

import middlewared.plugins.interface.netif_linux.interface as interface

from .utils import run

logger = logging.getLogger(__name__)

__all__ = ["AggregationProtocol", "create_lagg"]


class AggregationProtocol(enum.Enum):
    LACP = "802.3ad"
    FAILOVER = "active-backup"
    LOADBALANCE = "balance-xor"


def create_lagg(name):
    run(["ip", "link", "add", name, "type", "bond"])
    interface.Interface(name).up()


class LaggMixin:
    @property
    def protocol(self):
        value = self._sysfs_read(f"/sys/devices/virtual/net/{self.name}/bonding/mode")
        for protocol in AggregationProtocol:
            if protocol.value == value:
                return protocol

        return None

    @protocol.setter
    def protocol(self, value):
        run(["ip", "link", "set", self.name, "type", "bond", "mode", value.value])

    @property
    def ports(self):
        return self._sysfs_read(f"/sys/devices/virtual/net/{self.name}/bonding/slaves").split()

    def add_port(self, name):
        interface.Interface(name).down()
        run(["ip", "link", "set", name, "master", self.name])

    def delete_port(self, name):
        run(["ip", "link", "set", name, "nomaster"])
