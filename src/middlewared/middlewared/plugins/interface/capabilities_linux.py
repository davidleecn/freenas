from middlewared.service import Service

from .capabilities_base import InterfaceCapabilitiesBase


class InterfaceService(Service, InterfaceCapabilitiesBase):

    class Config:
        namespace_alias = 'interfaces'

    async def nic_capabilities(self):
        return []

    async def to_disable_evil_nic_capabilities(self, check_iface=True):
        return []

    def enable_capabilities(self, iface, capabilities):
        pass

    def disable_capabilities(self, iface, capabilities):
        pass
