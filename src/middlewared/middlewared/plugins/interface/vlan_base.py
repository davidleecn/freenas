from middlewared.service import private, ServicePartBase


class InterfaceVlanBase(ServicePartBase):
    @private
    def vlan_setup(self, vlan, disable_capabilities, parent_interfaces):
        raise NotImplementedError
