# -*- coding=utf-8 -*-
import glob
import logging
import os

import middlewared.plugins.interface.netif_linux.interface as interface

from .utils import run

logger = logging.getLogger(__name__)

__all__ = ["create_vlan", "VlanMixin"]


def create_vlan(name, parent, tag, qos):
    run(["ip", "link", "add", "link", parent, "name", name, "type", "vlan", "id", str(tag), "qos", str(qos)])
    interface = interface.Interface(name)
    interface.up()


class VlanMixin:
    @property
    def parent(self):
        return os.path.basename(os.readlink(glob.glob(f"/sys/devices/virtual/net/{self.name}/lower_*")[0]))

    def configure(self, parent, tag, pcp):
        run(["ip", "link", "delete", self.name])
        create_vlan(self.name, parent, tag, pcp)
