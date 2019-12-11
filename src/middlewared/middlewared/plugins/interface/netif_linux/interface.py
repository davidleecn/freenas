# -*- coding=utf-8 -*-
import logging

from .address import AddressFamily, AddressMixin
from .bridge import BridgeMixin
from .bits import InterfaceFlags, InterfaceLinkState
from .lagg import LaggMixin
from .utils import bitmask_to_set, run
from .vlan import VlanMixin

logger = logging.getLogger(__name__)

__all__ = ["Interface"]

CLONED_PREFIXES = [
    'lo', 'tun', 'tap', 'br', 'epair', 'carp', 'vlan', 'bond', 'pflog', 'pfsync',
]


class Interface(AddressMixin, BridgeMixin, LaggMixin, VlanMixin):
    def __init__(self, name):
        self.name = name

    def _read(self, name, type=str):
        return self._sysfs_read(f"/sys/class/net/{self.name}/{name}", type)

    def _sysfs_read(self, path, type=str):
        with open(path, "r") as f:
            value = f.read().strip()

        return type(value)

    @property
    def orig_name(self):
        return self.name

    @property
    def description(self):
        return self.name

    @description.setter
    def description(self, value):
        pass

    @property
    def mtu(self):
        return self._read("mtu", int)

    @mtu.setter
    def mtu(self, mtu):
        run(["ip", "link", "set", "dev", self.name, "mtu", str(mtu)])

    @property
    def cloned(self):
        for i in CLONED_PREFIXES:
            if self.orig_name.startswith(i):
                return True

        return False

    @property
    def flags(self):
        return bitmask_to_set(self._read("flags", lambda s: int(s, base=16)), InterfaceFlags)

    @property
    def nd6_flags(self):
        return set()

    @nd6_flags.setter
    def nd6_flags(self, value):
        pass

    @property
    def capabilities(self):
        return set()

    @property
    def link_state(self):
        operstate = self._read("operstate")

        return {
            "down": InterfaceLinkState.LINK_STATE_DOWN,
            "up": InterfaceLinkState.LINK_STATE_UP,
        }.get(operstate, InterfaceLinkState.LINK_STATE_UNKNOWN)

    @property
    def link_address(self):
        return list(filter(lambda x: x.af == AddressFamily.LINK, self.addresses)).pop()

    def __getstate__(self, address_stats=False):
        return {
            'name': self.name,
            'orig_name': self.orig_name,
            'description': self.description,
            'mtu': self.mtu,
            'cloned': self.cloned,
            'flags': [i.name for i in self.flags],
            'nd6_flags': [i.name for i in self.nd6_flags],
            'capabilities': [i.name for i in self.capabilities],
            'link_state': self.link_state.name,
            'media_type': '',
            'media_subtype': '',
            'active_media_type': '',
            'active_media_subtype': '',
            'supported_media': [],
            'media_options': None,
            'link_address': self.link_address.address.address,
            'aliases': [i.__getstate__(stats=address_stats) for i in self.addresses],
            'carp_config': None,
        }

    def up(self):
        run(["ip", "link", "set", self.name, "up"])

    def down(self):
        run(["ip", "link", "set", self.name, "down"])
