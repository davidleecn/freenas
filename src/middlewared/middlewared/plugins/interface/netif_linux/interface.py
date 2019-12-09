# -*- coding=utf-8 -*-
import ipaddress
import logging

from cached_property import cached_property
import netifaces

from .address import AddressFamily, InterfaceAddress, LinkAddress
from .interface_bits import InterfaceFlags, InterfaceLinkState
from .utils import bitmask_to_set

logger = logging.getLogger(__name__)

__all__ = ["Interface"]

CLONED_PREFIXES = [
    'lo', 'tun', 'tap', 'br', 'epair', 'carp', 'vlan', 'bond', 'pflog', 'pfsync',
]


class Interface:
    def __init__(self, name):
        self.name = name

    def _read(self, name, type=str):
        with open(f"/sys/class/net/{self.name}/{name}", "r") as f:
            value = f.read().strip()

        return type(value)

    @cached_property
    def orig_name(self):
        return self.name

    @cached_property
    def description(self):
        return self.name

    @cached_property
    def mtu(self):
        return self._read("mtu", int)

    @cached_property
    def cloned(self):
        for i in CLONED_PREFIXES:
            if self.orig_name.startswith(i):
                return True

        return False

    @cached_property
    def flags(self):
        return bitmask_to_set(self._read("flags", lambda s: int(s, base=16)), InterfaceFlags)

    @cached_property
    def nd6_flags(self):
        return set()

    @cached_property
    def capabilities(self):
        return set()

    @cached_property
    def link_state(self):
        operstate = self._read("operstate")

        return {
            "down": InterfaceLinkState.LINK_STATE_DOWN,
            "up": InterfaceLinkState.LINK_STATE_UP,
        }.get(operstate, InterfaceLinkState.LINK_STATE_UNKNOWN)

    @cached_property
    def link_address(self):
        return list(filter(lambda x: x.af == AddressFamily.LINK, self.addresses)).pop()

    @property
    def addresses(self):
        addresses = []

        for family, family_addresses in netifaces.ifaddresses(self.name).items():
            try:
                af = AddressFamily(family)
            except ValueError:
                logger.warning("Unknown address family %r for interface %r", family, self.name)
                continue

            for addr in family_addresses:
                if af is AddressFamily.LINK:
                    address = LinkAddress(self.name, addr["addr"])
                elif af is AddressFamily.INET:
                    address = ipaddress.IPv4Interface(f'{addr["addr"]}/{addr["netmask"]}')
                elif af is AddressFamily.INET6:
                    bits = bin(ipaddress.IPv6Address._ip_int_from_string(addr["netmask"]))[2:].rstrip("0")
                    if not all(c == "1" for c in bits):
                        logger.warning("Invalid IPv6 netmask %r for interface %r", addr["netmask"], self.name)
                        continue
                    prefixlen = len(bits)
                    address = ipaddress.IPv6Interface(f'{addr["addr"].split("%")[0]}/{prefixlen}')
                else:
                    continue

                addresses.append(InterfaceAddress(af, address))

        return addresses

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
