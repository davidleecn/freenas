from middlewared.service import private, ServicePartBase


class InterfaceControlBase(ServicePartBase):
    @private
    async def apply_configuration(self):
        raise NotImplementedError

    @private
    def destroy(self, name):
        raise NotImplementedError
