from middlewared.service import Service
from middlewared.utils import run

from .control_base import InterfaceControlBase


class InterfaceService(Service, InterfaceControlBase):

    class Config:
        namespace_alias = 'interfaces'

    async def apply_configuration(self):
        await run(["systemctl", "restart", "systemd-networkd"])

    def destroy(self, name):
        pass
