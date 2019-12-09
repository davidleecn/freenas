import netif

from middlewared.service import Service

from .control_base import InterfaceControlBase


class InterfaceService(Service, InterfaceControlBase):

    class Config:
        namespace_alias = 'interfaces'

    async def apply_configuration(self):
        pass

    def destroy(self, name):
        netif.destroy_interface(name)
