from middlewared.service import private, ServicePartBase


class InterfaceLaggBase(ServicePartBase):
    @private
    async def lagg_supported_modes(self):
        return ['LACP', 'FAILOVER', 'LOADBALANCE']

    @private
    def lagg_setup(self, lagg, members, disable_capabilities, parent_interfaces, sync_interface_opts):
        raise NotImplementedError
