from middlewared.service import private, ServicePartBase


class InterfaceBridgeBase(ServicePartBase):
    @private
    def bridge_setup(self, bridge):
        raise NotImplementedError
