from middlewared.service import Service

from .vlan_base import InterfaceVlanBase
from .systemd_network import write_systemd_network_file


class InterfaceService(Service, InterfaceVlanBase):

    class Config:
        namespace_alias = 'interfaces'

    def vlan_setup(self, vlan, disable_capabilities, parent_interfaces):
        write_systemd_network_file(f'{vlan["vlan_vint"]}.netdev', f'''\
            [NetDev]
            Name={vlan["vlan_vint"]}
            Kind=vlan
            
            [VLAN]
            Id={vlan['vlan_tag']}
        ''')
