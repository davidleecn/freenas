from middlewared.service import private, ServicePartBase


class InterfaceConfigureBase(ServicePartBase):
    @private
    def configure(self, data, aliases, wait_dhcp=False, **kwargs):
        raise NotImplementedError

    @private
    def autoconfigure(self, iface, wait_dhcp):
        raise NotImplementedError

    @private
    def unconfigure(self, iface, cloned_interfaces, parent_interfaces):
        raise NotImplementedError
