from middlewared.service import Service

from .lagg_base import InterfaceLaggBase
from .systemd_network import globunlink_systemd_network, write_systemd_network_file


class InterfaceService(Service, InterfaceLaggBase):

    class Config:
        namespace_alias = 'interfaces'

    async def lagg_supported_modes(self):
        return ['LACP', 'FAILOVER', 'LOADBALANCE']

    def lagg_setup(self, lagg, members, disable_capabilities, parent_interfaces, sync_interface_opts):
        name = lagg['lagg_interface']['int_interface']

        mode = {
            'LACP': '802.3ad',
            'FAILOVER': 'active-backup',
            'LOADBALANCE': 'balance-xor',
        }.get(lagg['lagg_protocol'].upper())

        write_systemd_network_file(f'{name}.netdev', f'''\
            [NetDev]
            Name={name}
            Kind=bond

            [Bond]
            Mode={mode}
        ''')

        globunlink_systemd_network(f'{name}-*.network')

        for member in members:
            member = member['lagg_physnic']
            write_systemd_network_file(f'{name}-{member}.network', f'''\
                [Match]
                Name={member}

                [Network]
                Bond={name}
            ''')
