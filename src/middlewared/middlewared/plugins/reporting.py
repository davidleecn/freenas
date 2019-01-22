from collections import defaultdict
import copy
import errno
import glob
import humanfriendly
import itertools
import json
import math
import os
import pandas
import psutil
import queue
import re
import select
import shutil
import socketserver
import subprocess
import sysctl
import syslog
import tarfile
import tempfile
import textwrap
import threading
import time

from middlewared.event import EventSource
from middlewared.schema import Bool, Dict, Int, List, Ref, Str, accepts
from middlewared.service import CallError, ConfigService, ValidationErrors, filterable, private
from middlewared.utils import filter_list, run, start_daemon_thread
from middlewared.validators import Range

PERSIST_FILE = '/data/rrd_dir.tar.bz2'
RE_COLON = re.compile('(.+):(.+)$')
RE_DISK = re.compile(r'^[a-z]+[0-9]+$')
RE_NAME = re.compile(r'(%name_(\d+)%)')
RE_NAME_NUMBER = re.compile(r'(.+?)(\d+)$')
RE_RRDPLUGIN = re.compile(r'^(?P<name>.+)Plugin$')
RE_SPACES = re.compile(r'\s{2,}')
RRD_BASE_PATH = '/var/db/collectd/rrd/localhost'
RRD_PLUGINS = {}
RRD_TYPE_QUEUES_EVENT = defaultdict(lambda: defaultdict(set))
RRD_TYPE_QUEUES_EVENT_LOCK = threading.Lock()
SYSRRD_SENTINEL = '/data/sentinels/sysdataset-rrd-disable'


def remove(path, link_only=False):
    if os.path.exists(path):
        if os.path.islink(path):
            os.unlink(path)
        elif os.path.isdir(path) and not link_only:
            shutil.rmtree(path)
        elif not link_only:
            os.remove(path)


def get_members(tar, prefix):
    for tarinfo in tar.getmembers():
        if tarinfo.name.startswith(prefix):
            tarinfo.name = tarinfo.name[len(prefix):]
            yield tarinfo


def rename_tarinfo(tarinfo):
    name = tarinfo.name.split('/', maxsplit=4)
    tarinfo.name = f'collectd/rrd/{"" if len(name) < 5 else name[-1]}'
    return tarinfo


class RRDMeta(type):

    def __new__(cls, name, bases, dct):
        klass = type.__new__(cls, name, bases, dct)
        reg = RE_RRDPLUGIN.search(name)
        if reg and not hasattr(klass, 'plugin'):
            klass.plugin = reg.group('name').lower()
        elif name != 'RRDBase' and not hasattr(klass, 'plugin'):
            raise ValueError(f'Could not determine plugin name for {name!r}')

        if reg and not hasattr(klass, 'name'):
            klass.name = reg.group('name').lower()
            RRD_PLUGINS[klass.name] = klass
        elif hasattr(klass, 'name'):
            RRD_PLUGINS[klass.name] = klass
        elif name != 'RRDBase':
            raise ValueError(f'Could not determine class name for {name!r}')
        return klass


class RRDBase(object, metaclass=RRDMeta):

    aggregations = ('min', 'mean', 'max')
    base_path = None
    title = None
    vertical_label = None
    identifier_plugin = True
    rrd_types = None
    rrd_data_extra = None

    def __init__(self, middleware):
        self.middleware = middleware
        self._base_path = RRD_BASE_PATH
        self.base_path = os.path.join(self._base_path, self.plugin)

    def __repr__(self):
        return f'<RRD:{self.plugin}>'

    def get_title(self):
        return self.title

    def get_vertical_label(self):
        return self.vertical_label

    def get_rrd_types(self, identifier=None):
        return self.rrd_types

    def __getstate__(self):
        return {
            'name': self.name,
            'title': self.get_title(),
            'vertical_label': self.get_vertical_label(),
            'identifiers': self.get_identifiers(),
        }

    @staticmethod
    def _sort_ports(entry):
        if entry == 'ha':
            pref = '0'
            body = entry
        else:
            reg = RE_COLON.search(entry)
            if reg:
                pref = reg.group(1)
                body = reg.group(2)
            else:
                pref = ''
                body = entry
        reg = RE_NAME_NUMBER.search(body)
        if not reg:
            return (pref, body, -1)
        return (pref, reg.group(1), int(reg.group(2)))

    @staticmethod
    def _sort_disks(entry):
        reg = RE_NAME_NUMBER.search(entry)
        if not reg:
            return (entry, )
        if reg:
            return (reg.group(1), int(reg.group(2)))

    def get_identifiers(self):
        return None

    def encode(self, identifier):
        return identifier

    def get_defs(self, identifier):

        rrd_types = self.get_rrd_types(identifier)
        if not rrd_types:
            raise RuntimeError(f'rrd_types not defined for {self.name!r}')

        args = []
        defs = {}
        for i, rrd_type in enumerate(rrd_types):
            _type, dsname, transform = rrd_type
            direc = self.plugin
            if self.identifier_plugin and identifier:
                identifier = self.encode(identifier)
                direc += f'-{identifier}'
            path = os.path.join(self._base_path, direc, f'{_type}.rrd')
            path = path.replace(':', r'\:')
            name = f'{_type}_{dsname}'
            defs[i] = {
                'name': name,
                'transform': transform,
            }
            args += [
                f'DEF:{name}={path}:{dsname}:AVERAGE',
            ]

        for i, attrs in defs.items():
            if attrs['transform']:
                transform = attrs['transform']
                if '%name%' in transform:
                    transform = transform.replace('%name%', attrs['name'])
                for orig, number in RE_NAME.findall(transform):
                    transform = transform.replace(orig, defs[int(number)]['name'])
                args += [
                    f'CDEF:c{attrs["name"]}={transform}',
                    f'XPORT:c{attrs["name"]}:{attrs["name"]}',
                ]
            else:
                args += [f'XPORT:{attrs["name"]}:{attrs["name"]}']

        if self.rrd_data_extra:
            extra = textwrap.dedent(self.rrd_data_extra)
            for orig, number in RE_NAME.findall(extra):
                def_ = defs[int(number)]
                name = def_['name']
                if def_['transform']:
                    name = 'c' + name
                extra = extra.replace(orig, name)
            args += extra.split()

        return args

    def export(self, identifier, starttime, endtime, aggregate=True):
        args = [
            'rrdtool',
            'xport',
            '--daemon', 'unix:/var/run/rrdcached.sock',
            '--json',
            '--end', endtime,
            '--start', starttime,
        ]
        args.extend(self.get_defs(identifier))
        cp = subprocess.run(args, capture_output=True)
        if cp.returncode != 0:
            raise RuntimeError(f'Failed to export RRD data: {cp.stderr.decode()}')

        data = json.loads(cp.stdout)
        data = dict(
            name=self.name,
            identifier=identifier,
            data=data['data'],
            **data['meta'],
            aggregations=dict(),
        )

        if self.aggregations and aggregate:
            df = pandas.DataFrame(data['data'])
            for agg in self.aggregations:
                if agg in ('max', 'mean', 'min'):
                    data['aggregations'][agg] = list(getattr(df, agg)())
                else:
                    raise RuntimeError(f'Aggregation {agg!r} is invalid.')

        return data


class CPUPlugin(RRDBase):

    plugin = 'aggregation-cpu-sum'
    title = 'CPU Usage'
    vertical_label = '%CPU'

    def get_defs(self, identifier):
        if self.middleware.call_sync('system.advanced.config')['cpu_in_percentage']:
            cpu_idle = os.path.join(self.base_path, 'percent-idle.rrd')
            cpu_nice = os.path.join(self.base_path, 'percent-nice.rrd')
            cpu_user = os.path.join(self.base_path, 'percent-user.rrd')
            cpu_system = os.path.join(self.base_path, 'percent-system.rrd')
            cpu_interrupt = os.path.join(self.base_path, 'percent-interrupt.rrd')

            args = [
                f'DEF:idle={cpu_idle}:value:AVERAGE',
                f'DEF:nice={cpu_nice}:value:AVERAGE',
                f'DEF:user={cpu_user}:value:AVERAGE',
                f'DEF:system={cpu_system}:value:AVERAGE',
                f'DEF:interrupt={cpu_interrupt}:value:AVERAGE',
                'CDEF:cinterrupt=interrupt,UN,0,interrupt,IF',
                'CDEF:csystem=system,UN,0,system,IF,cinterrupt,+',
                'CDEF:cuser=user,UN,0,user,IF,csystem,+',
                'CDEF:cnice=nice,UN,0,nice,IF,cuser,+',
                'CDEF:cidle=idle,UN,0,idle,IF,cnice,+',
                'XPORT:cinterrupt:interrupt',
                'XPORT:csystem:system',
                'XPORT:cuser:user',
                'XPORT:cnice:nice',
                'XPORT:cidle:idle',
            ]

            return args

        else:
            cpu_idle = os.path.join(self.base_path, 'cpu-idle.rrd')
            cpu_nice = os.path.join(self.base_path, 'cpu-nice.rrd')
            cpu_user = os.path.join(self.base_path, 'cpu-user.rrd')
            cpu_system = os.path.join(self.base_path, 'cpu-system.rrd')
            cpu_interrupt = os.path.join(self.base_path, 'cpu-interrupt.rrd')

            args = [
                f'DEF:idle={cpu_idle}:value:AVERAGE',
                f'DEF:nice={cpu_nice}:value:AVERAGE',
                f'DEF:user={cpu_user}:value:AVERAGE',
                f'DEF:system={cpu_system}:value:AVERAGE',
                f'DEF:interrupt={cpu_interrupt}:value:AVERAGE',
                'CDEF:total=idle,nice,user,system,interrupt,+,+,+,+',
                'CDEF:idle_p=idle,total,/,100,*',
                'CDEF:nice_p=nice,total,/,100,*',
                'CDEF:user_p=user,total,/,100,*',
                'CDEF:system_p=system,total,/,100,*',
                'CDEF:interrupt_p=interrupt,total,/,100,*',
                'CDEF:cinterrupt=interrupt_p,UN,0,interrupt_p,IF',
                'CDEF:csystem=system_p,UN,0,system_p,IF,cinterrupt,+',
                'CDEF:cuser=user_p,UN,0,user_p,IF,csystem,+',
                'CDEF:cnice=nice_p,UN,0,nice_p,IF,cuser,+',
                'CDEF:cidle=idle_p,UN,0,idle_p,IF,cnice,+',
                'XPORT:cinterrupt:interrupt',
                'XPORT:csystem:system',
                'XPORT:cuser:user',
                'XPORT:cnice:nice',
                'XPORT:cidle:idle',
            ]

            return args


class CPUTempPlugin(RRDBase):

    title = 'CPU Temperature'
    vertical_label = '\u00b0C'

    def __get_cputemp_file__(self, n):
        cputemp_file = os.path.join(self._base_path, f'cputemp-{n}', 'temperature.rrd')
        if os.path.isfile(cputemp_file):
            return cputemp_file

    def __get_number_of_cores__(self):
        try:
            return sysctl.filter('kern.smp.cpus')[0].value
        except Exception:
            return 0

    def __check_cputemp_avail__(self):
        n_cores = self.__get_number_of_cores__()
        if n_cores > 0:
            for n in range(0, n_cores):
                if self.__get_cputemp_file__(n) is None:
                    return False
        else:
            return False
        return True

    def get_identifiers(self):
        if not self.__check_cputemp_avail__():
            return []
        return None

    def get_defs(self, identifier):
        args = []
        for n in range(0, self.__get_number_of_cores__()):
            cputemp_file = self.__get_cputemp_file__(n)
            a = [
                f'DEF:s_avg{n}={cputemp_file}:value:AVERAGE',
                f'CDEF:avg{n}=s_avg{0},10,/,273.15,-',
                f'XPORT:avg{n}:cputemp{n}'
            ]
            args.extend(a)
        return args


class DiskTempPlugin(RRDBase):

    vertical_label = '\u00b0C'
    rrd_types = (
        ('temperature', 'value', None),
    )

    def get_title(self):
        return 'Disk Temperature {identifier}'

    def get_identifiers(self):
        ids = []
        for entry in glob.glob(f'{self._base_path}/disktemp-*'):
            ident = entry.rsplit('-', 1)[-1]
            if os.path.exists(os.path.join(entry, 'temperature.rrd')):
                ids.append(ident)
        ids.sort(key=RRDBase._sort_disks)
        return ids


class InterfacePlugin(RRDBase):

    vertical_label = 'Bits/s'
    rrd_types = (
        ('if_octets', 'rx', '%name%,8,*'),
        ('if_octets', 'tx', '%name%,8,*'),
    )
    rrd_data_extra = """
        CDEF:overlap=%name_0%,%name_1%,LT,%name_0%,%name_1%,IF
        XPORT:overlap:overlap
    """

    def get_title(self):
        return 'Interface Traffic ({identifier})'

    def get_identifiers(self):
        ids = []
        ifaces = [i['name'] for i in self.middleware.call_sync('interface.query')]
        for entry in glob.glob(f'{self._base_path}/interface-*'):
            ident = entry.rsplit('-', 1)[-1]
            if ident not in ifaces:
                continue
            if os.path.exists(os.path.join(entry, 'if_octets.rrd')):
                ids.append(ident)
        ids.sort(key=RRDBase._sort_disks)
        return ids


class MemoryPlugin(RRDBase):

    title = 'Physical memory utilization'
    vertical_label = 'Bytes'
    rrd_types = (
        ('memory-wired', 'value', '%name%,UN,0,%name%,IF'),
        ('memory-inactive', 'value', '%name%,UN,0,%name%,IF,%name_0%,+'),
        ('memory-laundry', 'value', '%name%,UN,0,%name%,IF,%name_1%,+'),
        ('memory-active', 'value', '%name%,UN,0,%name%,IF,%name_2%,+'),
        ('memory-free', 'value', '%name%,UN,0,%name%,IF,%name_3%,+'),
    )


class LoadPlugin(RRDBase):

    title = 'System Load'
    vertical_label = 'Processes'
    rrd_types = (
        ('load', 'shortterm', None),
        ('load', 'midterm', None),
        ('load', 'longterm', None),
    )


class ProcessesPlugin(RRDBase):

    title = 'Processes'
    vertical_label = 'Processes'
    rrd_types = (
        ('ps_state-wait', 'value', '%name%,UN,0,%name%,IF'),
        ('ps_state-idle', 'value', '%name%,UN,0,%name%,IF,%name_0%,+'),
        ('ps_state-sleeping', 'value', '%name%,UN,0,%name%,IF,%name_1%,+'),
        ('ps_state-running', 'value', '%name%,UN,0,%name%,IF,%name_2%,+'),
        ('ps_state-stopped', 'value', '%name%,UN,0,%name%,IF,%name_3%,+'),
        ('ps_state-zombies', 'value', '%name%,UN,0,%name%,IF,%name_4%,+'),
        ('ps_state-blocked', 'value', '%name%,UN,0,%name%,IF,%name_5%,+'),
    )


class SwapPlugin(RRDBase):

    title = 'Swap Utilization'
    vertical_label = 'Bytes'
    rrd_types = (
        ('swap-used', 'value', '%name%,UN,0,%name%,IF'),
        ('swap-free', 'value', '%name%,UN,0,%name%,IF,%name_0%,+'),
    )


class DFPlugin(RRDBase):

    vertical_label = 'Bytes'
    rrd_types = (
        ('df_complex-free', 'value', None),
        ('df_complex-used', 'value', None),
    )
    rrd_data_extra = """
        CDEF:both=%name_0%,%name_1%,+
        XPORT:both:both
    """

    def get_title(self):
        return 'Disk space ({identifier})'

    def encode(self, path):
        if path == '/':
            return 'root'
        return path.strip('/').replace('/', '-')

    def get_identifiers(self):
        ids = []
        cp = subprocess.run(['df', '-t', 'zfs'], capture_output=True, text=True)
        for line in cp.stdout.strip().split('\n'):
            entry = RE_SPACES.split(line)[-1]
            if entry != '/' and not entry.startswith('/mnt'):
                continue
            path = os.path.join(self._base_path, 'df-' + self.encode(entry), 'df_complex-free.rrd')
            if os.path.exists(path):
                ids.append(entry)
        return ids


class UptimePlugin(RRDBase):

    title = 'Uptime'
    vertical_label = 'Days'
    rrd_types = (
        ('uptime', 'value', '%name%,86400,/'),
    )


class CTLPlugin(RRDBase):

    vertical_label = 'Bytes/s'
    rrd_types = (
        ('disk_octets', 'read', None),
        ('disk_octets', 'write', None),
    )

    def get_title(self):
        return 'SCSI target port ({identifier})'

    def get_identifiers(self):
        ids = []
        for entry in glob.glob(f'{self._base_path}/ctl-*'):
            ident = entry.split('-', 1)[-1]
            if ident.endswith('ioctl'):
                continue
            if os.path.exists(os.path.join(entry, 'disk_octets.rrd')):
                ids.append(ident)

        ids.sort(key=RRDBase._sort_ports)
        return ids


class DiskPlugin(RRDBase):

    vertical_label = 'Bytes/s'
    rrd_types = (
        ('disk_octets', 'read', None),
        ('disk_octets', 'write', None),
    )

    def get_title(self):
        return 'Disk I/O ({identifier})'

    def get_identifiers(self):
        ids = []
        for entry in glob.glob(f'{self._base_path}/disk-*'):
            ident = entry.split('-', 1)[-1]
            if not os.path.exists(f'/dev/{ident}'):
                continue
            if ident.startswith('pass'):
                continue
            if os.path.exists(os.path.join(entry, 'disk_octets.rrd')):
                ids.append(ident)

        ids.sort(key=RRDBase._sort_disks)
        return ids


class GeomStatBase(object):

    geom_stat_name = None

    def get_identifiers(self):
        ids = []
        for entry in glob.glob(f'{self._base_path}/geom_stat/{self.geom_stat_name}-*'):
            ident = entry.split('-', 1)[-1].replace('.rrd', '')
            if not RE_DISK.match(ident):
                continue
            if not os.path.exists(f'/dev/{ident}'):
                continue
            if ident.startswith('pass'):
                continue
            ids.append(ident)

        ids.sort(key=RRDBase._sort_disks)
        return ids


class DiskGeomBusyPlugin(GeomStatBase, RRDBase):

    geom_stat_name = 'geom_busy_percent'
    identifier_plugin = False
    plugin = 'geom_stat'
    vertical_label = 'Percent'

    def get_rrd_types(self, identifier):
        return (
            (f'geom_busy_percent-{identifier}', 'value', None),
        )

    def get_title(self):
        return 'Disk Busy ({identifier})'


class DiskGeomLatencyPlugin(GeomStatBase, RRDBase):

    geom_stat_name = 'geom_latency'
    identifier_plugin = False
    plugin = 'geom_stat'
    vertical_label = 'Time,msec'

    def get_rrd_types(self, identifier):
        return (
            (f'geom_latency-{identifier}', 'read', None),
            (f'geom_latency-{identifier}', 'write', None),
            (f'geom_latency-{identifier}', 'delete', None),
        )

    def get_title(self):
        return 'Disk Latency ({identifier})'


class DiskGeomOpsRWDPlugin(GeomStatBase, RRDBase):

    geom_stat_name = 'geom_ops_rwd'
    identifier_plugin = False
    plugin = 'geom_stat'
    vertical_label = 'Operations/s'

    def get_rrd_types(self, identifier):
        return (
            (f'geom_ops_rwd-{identifier}', 'read', None),
            (f'geom_ops_rwd-{identifier}', 'write', None),
            (f'geom_ops_rwd-{identifier}', 'delete', None),
        )

    def get_title(self):
        return 'Disk Operations detailed ({identifier})'


class DiskGeomQueuePlugin(GeomStatBase, RRDBase):

    geom_stat_name = 'geom_queue'
    identifier_plugin = False
    plugin = 'geom_stat'
    vertical_label = 'Requests'

    def get_rrd_types(self, identifier):
        return (
            (f'geom_queue-{identifier}', 'length', None),
        )

    def get_title(self):
        return 'Pending I/O requests on ({identifier})'


class ARCSizePlugin(RRDBase):

    plugin = 'zfs_arc'
    vertical_label = 'Bytes'
    rrd_types = (
        ('cache_size-arc', 'value', None),
        ('cache_size-L2', 'value', None),
    )

    def get_title(self):
        return 'ARC Size'


class ARCRatioPlugin(RRDBase):

    plugin = 'zfs_arc'
    vertical_label = 'Hits (%)'
    rrd_types = (
        ('cache_ratio-arc', 'value', '%name%,100,*'),
        ('cache_ratio-L2', 'value', '%name%,100,*'),
    )

    def get_title(self):
        return 'ARC Hit Ratio'


class ARCResultPlugin(RRDBase):

    identifier_plugin = False
    plugin = 'zfs_arc'
    vertical_label = 'Requests'
    rrd_data_extra = """
        CDEF:total=%name_0%,%name_1%,+
        XPORT:total:total
    """

    def get_rrd_types(self, identifier):
        return (
            (f'cache_result-{identifier}-hit', 'value', '%name%,100,*'),
            (f'cache_result-{identifier}-miss', 'value', '%name%,100,*'),
        )

    def get_title(self):
        return 'ARC Requests ({identifier})'

    def get_identifiers(self):
        return ('demand_data', 'demand_metadata', 'prefetch_data', 'prefetch_metadata')


class NFSStatPlugin(RRDBase):

    plugin = 'nfsstat-client'
    title = 'NFS Stats'
    vertical_label = 'Bytes'
    rrd_types = (
        ('nfsstat-read', 'value', None),
        ('nfsstat-write', 'value', None),
    )


class ReportingService(ConfigService):

    class Config:
        datastore = 'system.reporting'

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.__rrds = {}
        for name, klass in RRD_PLUGINS.items():
            self.__rrds[name] = klass(self.middleware)

    @accepts(
        Dict(
            'reporting_update',
            Bool('rrd_usedataset'),
            Bool('cpu_in_percentage'),
            Str('graphite'),
            Int('rrd_ramdisk_size', validators=[Range(min=1)]),
            List('graph_timespans', items=[Int('timespan', validators=[Range(min=1)])], empty=False, unique=True),
            Int('graph_rows', validators=[Range(min=1)]),
            Bool('confirm_rrd_destroy'),
            update=True
        )
    )
    async def do_update(self, data):
        """
        Configure Reporting Database settings.

        `rrd_usedataset` is a flag that determines whether reporting database is located in system dataset or on
        RAMDisk.

        If `cpu_in_percentage` is `true`, collectd will report CPU usage in percentage instead of "jiffies".

        `graphite` specifies a hostname or IP address that will be used as the destination to send collectd data
        using the graphite plugin.

        `rrd_ramdisk_size` specifies size (in bytes) for RAMDisk if `rrd_usedataset` is unchecked.

        `graph_timespans` and `graph_rows` correspond to collectd `RRARows` and `RRATimespan` options. Changing these
        will require destroying your current reporting database so when these fields are changed, an additional
        `confirm_rrd_destroy: true` flag must be present

        .. examples(websocket)::

          Update reporting settings

            :::javascript
            {
                "id": "6841f242-840a-11e6-a437-00e04d680384",
                "msg": "method",
                "method": "reporting.update",
                "params": [{
                    "rrd_usedataset": true,
                    "cpu_in_percentage": false,
                    "graphite": "",
                    "rrd_ramdisk_size": 1073741824,
                }]
            }

          Recreate reporting database with new settings

            :::javascript
            {
                "id": "6841f242-840a-11e6-a437-00e04d680384",
                "msg": "method",
                "method": "reporting.update",
                "params": [{
                    "graph_timespans": [3600, 86400, 604800, 2678400, 31622400],
                    "graph_rows": 1200,
                    "confirm_rrd_destroy": true,
                }]
            }
        """

        confirm_rrd_destroy = data.pop('confirm_rrd_destroy', False)

        old = await self.config()

        new = copy.deepcopy(old)
        new.update(data)

        verrors = ValidationErrors()

        data_dir = '/var/db/collectd/rrd'
        update_ramdisk = False
        if old['rrd_ramdisk_size'] != new['rrd_ramdisk_size']:
            disk_parts = psutil.disk_partitions()
            data_dir_is_ramdisk = len([d for d in disk_parts if d.mountpoint == data_dir and d.device == 'tmpfs']) > 0
            if data_dir_is_ramdisk:
                update_ramdisk = True

                used = psutil.disk_usage(data_dir).used
                if new['rrd_ramdisk_size'] < used:
                    verrors.add(
                        'reporting_update.rrd_ramdisk_size',
                        f'Your current RAMDisk usage is {humanfriendly.format_size(used)} ({used} bytes), you can\'t '
                        f'set RAMDisk size below this value'
                    )

        destroy_database = False
        for k in ['graph_timespans', 'graph_rows']:
            if old[k] != new[k]:
                destroy_database = True

                if not confirm_rrd_destroy:
                    verrors.add(
                        f'reporting_update.{k}',
                        f'Changing this option requires destroying of reporting database so you\'ll have to confirm '
                        f'this action by setting corresponding flag',
                    )

        if verrors:
            raise verrors

        await self.middleware.call(
            'datastore.update',
            self._config.datastore,
            old['id'],
            new,
            {'prefix': self._config.datastore_prefix}
        )

        if old['rrd_usedataset'] != new['rrd_usedataset']:
            await self.middleware.call('service.stop', 'collectd')
            await self._rrd_toggle()

        if update_ramdisk:
            await self.middleware.call('service.stop', 'collectd')
            await self.middleware.call('reporting.persist_ramdisk')
            await run('umount', data_dir, check=False)

        if destroy_database:
            await self.middleware.call('service.stop', 'collectd')
            await self.middleware.run_in_thread(os.unlink, PERSIST_FILE)
            await run('sh', '-c', 'rm -rf /var/db/collectd/rrd/*', check=False)

        await self.middleware.call('service.restart', 'collectd')

        return await self.config()

    async def _rrd_toggle(self):
        config = await self.config()
        systemdatasetconfig = await self.middleware.call('systemdataset.config')

        # Path where collectd stores files
        rrd_path = '/var/db/collectd/rrd'
        # Path where rrd fies are stored in system dataset
        rrd_syspath = f'/var/db/system/rrd-{systemdatasetconfig["uuid"]}'

        if config['rrd_usedataset']:
            # Move from tmpfs to system dataset
            if os.path.exists(rrd_path):
                if os.path.islink(rrd_path):
                    # rrd path is already a link
                    # so there is nothing we can do about it
                    return False
                cp = await run('rsync', '-a', f'{rrd_path}/', f'{rrd_syspath}/', check=False)
                return cp.returncode == 0
        else:
            # Move from system dataset to tmpfs
            if os.path.exists(rrd_path):
                if os.path.islink(rrd_path):
                    os.unlink(rrd_path)
            else:
                os.makedirs(rrd_path)
            cp = await run('rsync', '-a', f'{rrd_syspath}/', f'{rrd_path}/', check=False)
            return cp.returncode == 0
        return False

    @private
    def use_rrd_dataset(self):
        config = self.middleware.call_sync('reporting.config')
        systemdatasetconfig = self.middleware.call_sync('systemdataset.config')
        is_freenas = self.middleware.call_sync('system.is_freenas')
        rrd_mount = ''
        if systemdatasetconfig['path']:
            rrd_mount = f'{systemdatasetconfig["path"]}/rrd-{systemdatasetconfig["uuid"]}'

        use_rrd_dataset = False
        if (
            rrd_mount and config['rrd_usedataset'] and (
                is_freenas or (not is_freenas and self.middleware.call_sync('failover.status') != 'BACKUP')
            )
        ):
            use_rrd_dataset = True

        return use_rrd_dataset

    @private
    def update_collectd_dataset(self):
        config = self.middleware.call_sync('reporting.config')
        systemdatasetconfig = self.middleware.call_sync('systemdataset.config')
        is_freenas = self.middleware.call_sync('system.is_freenas')
        rrd_mount = ''
        if systemdatasetconfig['path']:
            rrd_mount = f'{systemdatasetconfig["path"]}/rrd-{systemdatasetconfig["uuid"]}'

        use_rrd_dataset = self.use_rrd_dataset()

        # If not is_freenas remove the rc.conf cache rc.conf.local will
        # run again using the correct collectd_enable. See #5019
        if not is_freenas:
            try:
                os.remove('/var/tmp/freenas_config.md5')
            except FileNotFoundError:
                pass

        hostname = self.middleware.call_sync('system.info')['hostname']
        if not hostname:
            hostname = self.middleware.call_sync('network.configuration.config')['hostname']

        data_dir = '/var/db/collectd/rrd'
        disk_parts = psutil.disk_partitions()
        data_dir_is_ramdisk = len([d for d in disk_parts if d.mountpoint == data_dir and d.device == 'tmpfs']) > 0

        if use_rrd_dataset:
            if os.path.isdir(rrd_mount):
                if os.path.isdir(data_dir) and not os.path.islink(data_dir):
                    if data_dir_is_ramdisk:
                        # copy-umount-remove
                        subprocess.Popen(
                            ['cp', '-a', data_dir, f'{data_dir}.{time.strftime("%Y%m%d%H%M%S")}'],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE
                        ).communicate()

                        # Should we raise an exception if umount fails ?
                        subprocess.Popen(
                            ['umount', data_dir],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE
                        ).communicate()

                        remove(data_dir)
                    else:
                        shutil.move(data_dir, f'{data_dir}.{time.strftime("%Y%m%d%H%M%S")}')

                if os.path.realpath(data_dir) != rrd_mount:
                    remove(data_dir)
                    os.symlink(rrd_mount, data_dir)
            else:
                self.middleware.logger.error(f'{rrd_mount} does not exist or is not a directory')
                return None
        else:
            remove(data_dir, link_only=True)

            if not os.path.isdir(data_dir):
                os.makedirs(data_dir)

            # Create RAMdisk (if not already exists) for RRD files so they don't fill up root partition
            if not data_dir_is_ramdisk:
                subprocess.Popen(
                    ['mount', '-t', 'tmpfs', '-o', f'size={config["rrd_ramdisk_size"]}', 'tmpfs', data_dir],
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE
                ).communicate()

        pwd = rrd_mount if use_rrd_dataset else data_dir
        if not os.path.exists(pwd):
            self.middleware.logger.error(f'{pwd} does not exist')
            return None

        if os.path.isfile(PERSIST_FILE):
            with tarfile.open(PERSIST_FILE) as tar:
                if 'collectd/rrd' in tar.getnames():
                    tar.extractall(pwd, get_members(tar, 'collectd/rrd/'))

            if use_rrd_dataset:
                remove(PERSIST_FILE)

        # Migrate from old version, where "${hostname}" was a real directory
        # and "localhost" was a symlink.
        # Skip the case where "${hostname}" is "localhost", so symlink was not
        # (and is not) needed.
        if (
            hostname != 'localhost' and os.path.isdir(os.path.join(pwd, hostname)) and not os.path.islink(
                os.path.join(pwd, hostname)
            )
        ):
            if os.path.exists(os.path.join(pwd, 'localhost')):
                if os.path.islink(os.path.join(pwd, 'localhost')):
                    remove(os.path.join(pwd, 'localhost'))
                else:
                    # This should not happen, but just in case
                    shutil.move(
                        os.path.join(pwd, 'localhost'),
                        os.path.join(pwd, f'localhost.bak.{time.strftime("%Y%m%d%H%M%S")}')
                    )
            shutil.move(os.path.join(pwd, hostname), os.path.join(pwd, 'localhost'))

        # Remove all directories except "localhost" and it's backups (that may be erroneously created by
        # running collectd before this script)
        to_remove_dirs = [
            os.path.join(pwd, d) for d in os.listdir(pwd)
            if not d.startswith('localhost') and os.path.isdir(os.path.join(pwd, d))
        ]
        for r_dir in to_remove_dirs:
            remove(r_dir)

        # Remove all symlinks (that are stale if hostname was changed).
        to_remove_symlinks = [
            os.path.join(pwd, l) for l in os.listdir(pwd)
            if os.path.islink(os.path.join(pwd, l))
        ]
        for r_symlink in to_remove_symlinks:
            remove(r_symlink)

        # Create "localhost" directory if it does not exist
        if not os.path.exists(os.path.join(pwd, 'localhost')):
            os.makedirs(os.path.join(pwd, 'localhost'))

        # Create "${hostname}" -> "localhost" symlink if necessary
        if hostname != 'localhost':
            os.symlink(os.path.join(pwd, 'localhost'), os.path.join(pwd, hostname))

        # Let's return a positive value to indicate that necessary collectd operations were performed successfully
        return True

    @private
    def sysrrd_disable(self):
        # skip if no sentinel is found
        if os.path.exists(SYSRRD_SENTINEL):
            systemdataset_config = self.middleware.call_sync('systemdataset.config')
            rrd_mount = f'{systemdataset_config["path"]}/rrd-{systemdataset_config["uuid"]}'
            if os.path.isdir(rrd_mount):
                # Let's create tar from system dataset rrd which collectd.conf understands
                with tarfile.open(PERSIST_FILE, mode='w:bz2') as archive:
                    archive.add(rrd_mount, filter=rename_tarinfo)

            os.remove(SYSRRD_SENTINEL)

    @private
    def persist_ramdisk(self):
        if self.middleware.call_sync('reporting.use_rrd_dataset'):
            return

        with tempfile.NamedTemporaryFile(dir=os.path.dirname(PERSIST_FILE), suffix='.tar.bz2', delete=False) as f:
            try:
                subprocess.run(['tar', '-C', '/var/db', '-cjf', f.name, 'collectd'], check=True)

                free = psutil.disk_usage(os.path.dirname(PERSIST_FILE)).free
                if os.path.exists(PERSIST_FILE):
                    free += os.path.getsize(PERSIST_FILE)
                if free < 20 * 1024 * 1024:
                    syslog.syslog('Not enough space on /data to save collectd data')
                    return

                shutil.move(f.name, PERSIST_FILE)
            finally:
                if os.path.exists(f.name):
                    os.unlink(f.name)

    @filterable
    def graphs(self, filters, options):
        return filter_list([i.__getstate__() for i in self.__rrds.values()], filters, options)

    def __rquery_to_start_end(self, query):
        unit = query.get('unit')
        if unit:
            verrors = ValidationErrors()
            for i in ('start', 'end'):
                if i in query:
                    verrors.add(
                        f'reporting_query.{i}',
                        f'{i!r} should only be used if "unit" attribute is not provided.',
                    )
            verrors.check()
        else:
            if 'start' not in query:
                unit = 'HOURLY'
            else:
                starttime = query['start']
                endtime = query.get('end') or 'now'

        if unit:
            unit = unit[0].lower()
            page = query['page']
            starttime = f'end-{page + 1}{unit}'
            if not page:
                endtime = 'now'
            else:
                endtime = f'now-{page}{unit}'
        return starttime, endtime

    @accepts(
        List('graphs', items=[
            Dict(
                'graph',
                Str('name', required=True),
                Str('identifier', default=None, null=True),
            ),
        ], empty=False),
        Dict(
            'reporting_query',
            Str('unit', enum=['HOUR', 'DAY', 'WEEK', 'MONTH', 'YEAR']),
            Int('page', default=0),
            Str('start', empty=False),
            Str('end', empty=False),
            Bool('aggregate', default=True),
            register=True,
        )
    )
    def get_data(self, graphs, query):
        """
        Get reporting data for given graphs.

        List of possible graphs can be retrieved using `reporting.graphs` call.

        For the time period of the graph either `unit` and `page` OR `start` and `end` should be
        used, not both.

        `aggregate` will return aggregate available data for each graph (e.g. min, max, mean).

        .. examples(websocket)::

          Get graph data of "nfsstat" from the last hour.

            :::javascript
            {
                "id": "6841f242-840a-11e6-a437-00e04d680384",
                "msg": "method",
                "method": "reporting.get_data",
                "params": [
                    [{"name": "nfsstat"}],
                    {"unit": "HOURLY"},
                ]
            }

        """
        starttime, endtime = self.__rquery_to_start_end(query)
        rv = []
        for i in graphs:
            try:
                rrd = self.__rrds[i['name']]
            except KeyError:
                raise CallError(f'Graph {i["name"]!r} not found.', errno.ENOENT)
            rv.append(
                rrd.export(i['identifier'], starttime, endtime, aggregate=query['aggregate'])
            )
        return rv

    @private
    @accepts(Ref('reporting_query'))
    def get_all(self, query):
        starttime, endtime = self.__rquery_to_start_end(query)
        rv = []
        for rrd in self.__rrds.values():
            idents = rrd.get_identifiers()
            if idents is None:
                idents = [None]
            for ident in idents:
                rv.append(rrd.export(ident, starttime, endtime, aggregate=query['aggregate']))
        return rv

    @private
    def get_plugin_and_rrd_types(self, name_idents):
        rv = []
        for name, identifier in name_idents:
            rrd = self.__rrds[name]
            rv.append(((name, identifier), rrd.plugin, rrd.get_rrd_types(identifier)))
        return rv


class GraphiteServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


class GraphiteHandler(socketserver.BaseRequestHandler):
    def handle(self):
        last = b''
        while True:
            data = b''

            recv = self.request.recv(1428)
            if not recv:
                return
            data += recv

            # Try to read a batch of updates at once, instead of breaking per message size
            while True:
                if not select.select([self.request.fileno()], [], [], 0.1)[0]:
                    break

                recv = self.request.recv(1428)
                if not recv:
                    return
                data += recv

            if last:
                data = last + data
                last = b''
            lines = (last + data).split(b'\r\n')
            if lines[-1] != b'':
                last = lines[-1]

            nameident_queues = defaultdict(set)
            nameident_timestamps = defaultdict(set)
            for line in lines[:-1]:
                line, value, timestamp = line.split(b' ')
                timestamp = int(timestamp)
                name = line.split(b'.', 1)[1].decode()
                with RRD_TYPE_QUEUES_EVENT_LOCK:
                    if name in RRD_TYPE_QUEUES_EVENT:
                        queues = RRD_TYPE_QUEUES_EVENT[name]
                        nameident_queues.update(queues)
                        for nameident in queues.keys():
                            nameident_timestamps[nameident].add(timestamp)
            for nameident, queues in nameident_queues.items():
                for q in queues:
                    q.put((nameident, nameident_timestamps.get(nameident)))


def collectd_graphite(middleware):
    with GraphiteServer(('127.0.0.1', 2003), GraphiteHandler) as server:
        server.middleware = middleware
        server.serve_forever()


class RealtimeEventSource(EventSource):

    @staticmethod
    def get_cpu_usages(cp_diff):
        cp_total = sum(cp_diff)
        cpu_user = cp_diff[0] / cp_total * 100
        cpu_nice = cp_diff[1] / cp_total * 100
        cpu_system = cp_diff[2] / cp_total * 100
        cpu_interrupt = cp_diff[3] / cp_total * 100
        cpu_idle = cp_diff[4] / cp_total * 100
        # Usage is the sum of user, nice, system and interrupt over total (including idle)
        cpu_usage = (sum(cp_diff[:4]) / cp_total) * 100
        return {
            'usage': cpu_usage,
            'user': cpu_user,
            'nice': cpu_nice,
            'system': cpu_system,
            'interrupt': cpu_interrupt,
            'idle': cpu_idle,
        }

    def run(self):

        cp_time_last = None
        cp_times_last = None

        while not self._cancel.is_set():
            data = {}
            # Virtual memory use
            data['virtual_memory'] = psutil.virtual_memory()._asdict()

            data['cpu'] = {}
            # Get CPU usage %
            # cp_times has values for all cores
            cp_times = sysctl.filter('kern.cp_times')[0].value
            # cp_time is the sum of all cores
            cp_time = sysctl.filter('kern.cp_time')[0].value
            if cp_times_last:
                # Get the difference of times between the last check and the current one
                # cp_time has a list with user, nice, system, interrupt and idle
                cp_diff = list(map(lambda x: x[0] - x[1], zip(cp_times, cp_times_last)))
                cp_nums = int(len(cp_times) / 5)
                for i in range(cp_nums):
                    data['cpu'][i] = self.get_cpu_usages(cp_diff[i * 5:i * 5 + 5])

                cp_diff = list(map(lambda x: x[0] - x[1], zip(cp_time, cp_time_last)))
                data['cpu']['average'] = self.get_cpu_usages(cp_diff)
            cp_time_last = cp_time
            cp_times_last = cp_times

            # CPU temperature
            data['cpu']['temperature'] = {}
            for i in itertools.count():
                v = sysctl.filter(f'dev.cpu.{i}.temperature')
                if not v:
                    break
                data['cpu']['temperature'][i] = v[0].value

            self.send_event('ADDED', fields=data)
            time.sleep(2)


class ReportingEventSource(EventSource):

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.queue = queue.Queue()
        self.queue_reverse = []

    def run(self):
        try:
            arg = json.loads(self.arg)
        except Exception:
            self.middleware.logger.debug(
                'Failed to subscribe to reporting.get_data', exc_info=True,
            )
            return

        plugin_rrd_types = self.middleware.call_sync(
            'reporting.get_plugin_and_rrd_types', [(i['name'], i['identifier']) for i in arg]
        )

        for name_ident, plugin, rrd_types in plugin_rrd_types:
            for rrd_type in rrd_types:
                name = f'{plugin}.{rrd_type[0]}.{rrd_type[1]}'
                with RRD_TYPE_QUEUES_EVENT_LOCK:
                    queues = RRD_TYPE_QUEUES_EVENT[name][name_ident]
                    queues.add(self.queue)
                    self.queue_reverse.append(queues)

        while not self._cancel.is_set():
            nameident, timestamps = self.queue.get()
            if not timestamps:
                self.middleware.logger.debug('Timetamps not found for %r', nameident)
                timestamps = [int(time.time())]
            name, ident = nameident
            # 10 is subtracted because rrdtool needs the full 10 seconds step to return
            # not null data for the period. That means we are actually returning the previous
            # step (read delaying stats by at least 10 seconds)
            start = math.floor(min(timestamps) / 10) * 10 - 10
            end = math.ceil(max(timestamps) / 10) * 10 - 10
            if start == end:
                start -= 10
            try:
                data = self.middleware.call_sync(
                    'reporting.get_data',
                    [{
                        'name': name,
                        'identifier': ident,
                    }],
                    {'start': start, 'end': end, 'aggregate': False},
                )
                self.send_event('ADDED', fields=data[0])
            except Exception:
                self.middleware.logger.debug(
                    'Failed to send reporting event for {name!r}:{ident!r}', exc_info=True,
                )

    def on_finish(self):
        """
        We need to remove queue from RRD_TYPE_QUEUES_EVENT
        """
        for q in self.queue_reverse:
            q.remove(self.queue)


def setup(middleware):
    start_daemon_thread(target=collectd_graphite, args=[middleware])
    middleware.register_event_source('reporting.get_data', ReportingEventSource)
    middleware.register_event_source('reporting.realtime', RealtimeEventSource)
