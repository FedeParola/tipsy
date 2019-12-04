#!/usr/bin/env python3

# TIPSY: Telco pIPeline benchmarking SYstem
#
# Copyright (C) 2019 by ?
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful, but
# WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU
# General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program. If not, see <http://www.gnu.org/licenses/>.

import argparse
import json
import os
import re
import requests
import signal
import subprocess
import sys
import time
from tempfile import NamedTemporaryFile


class PL(object):
    def __init__(self, plconf, bmconf):
        self.plconf = plconf
        self.bmconf = bmconf
        self.runtime_interval = 1
        self.uplink_p = self.bmconf.sut.uplink_port
        self.downlink_p = self.bmconf.sut.downlink_port
        self._running = False

    def init(self):
        raise NotImplementedError

    def start(self):
        self._running = True
        self._run()

    def stop(self):
        self._running = False

    def _run(self):
        table_actions = ('mod_l3_table', 'mod_group_table')
        while self._running:
            for task in self.plconf.run_time:
                if not self._running:
                    return
                if task.action in table_actions:
                    self.mod_table(task.action, task.cmd,
                                   task.table, task.entry)
            time.sleep(self.runtime_interval)

    def mod_table(self, action, cmd, table, entry):
        raise NotImplementedError


class PL_portfwd(PL):
    def init(self):
        call_cmd(['polycubectl', 'simpleforwarder', 'add', 'sf0', 'type=XDP_DRV'])
        call_cmd(['polycubectl', 'sf0', 'ports', 'add', 'uport', 'peer=' + self.uplink_p])
        call_cmd(['polycubectl', 'sf0', 'ports', 'add', 'dport', 'peer=' + self.downlink_p])
        call_cmd(['polycubectl', 'sf0', 'actions', 'add', 'uport', 'action=FORWARD', 'outport=dport'])
        call_cmd(['polycubectl', 'sf0', 'actions', 'add', 'dport', 'action=FORWARD', 'outport=uport'])

    def _run(self):
        while self._running:
            time.sleep(self.runtime_interval)


# class PL_l3fwd(PL):
#     def __init__(self, plconf, bmconf):
#         super(PL_l3fwd, self).__init__(plconf, bmconf)
#         self.extra_seq = 0
#         self.extra_l2 = {}
#         self.extra_ip = '49.49.%d.%d'

#     def init(self):
#         cmds = []
#         for params in [(self.uplink_if, '192.168.2.2/24'),
#                        (self.downlink_if, '192.168.1.1/24')]:
#             cmds.append('set int ip address %s %s' % params)

#         for entry in self.plconf.upstream_l3_table:
#             ip = re.sub(r'\.[^.]+$', '.0', entry.ip)
#             route_params = (ip, entry.prefix_len, self.uplink_if)
#             cmds.append('ip route add %s/%d via %s' % route_params)
#             arp_params = (self.uplink_if, entry.ip,
#                           self.plconf.upstream_group_table[entry.nhop].dmac)
#             cmds.append('set ip arp %s %s %s' % arp_params)

#         for entry in self.plconf.downstream_l3_table:
#             ip = re.sub(r'\.[^.]+$', '.0', entry.ip)
#             route_params = (ip, entry.prefix_len, self.downlink_if)
#             cmds.append('ip route add %s/%d via %s' % route_params)
#             arp_params = (self.downlink_if, entry.ip,
#                           self.plconf.downstream_group_table[entry.nhop].dmac)
#             cmds.append('set ip arp %s %s %s' % arp_params)

#         for interf in [self.uplink_if, self.downlink_if]:
#             cmds.append('set int state %s up' % interf)

#         inc = 10000
#         for i in range(0, len(cmds), inc):
#             tmp_file = NamedTemporaryFile(delete=True).name
#             with open(tmp_file, 'w') as f:
#                 for cmd in cmds[i:i+inc]:
#                     f.write("%s\n" % cmd)
#                 f.flush()
#             vpp_cmd = ['sudo', 'vppctl', 'exec', tmp_file]
#             call_cmd(vpp_cmd)

#     def mod_table(self, action, cmd, table, entry):
#         if not any(x in cmd for x in ['add', 'del']):
#             raise ValueError
#         if 'l3' in action:
#             self.mod_l3_table(cmd, table, entry)
#         elif 'group' in action:
#             self.mod_group_table(cmd, table, entry)

#     def mod_l3_table(self, cmd, table, entry):
#         route_template = 'sudo vppctl ip route %s %s/%d via %s'
#         arp_template = 'sudo vppctl set ip arp %s %s %s %s'
#         interface = {'upstream': self.uplink_if,
#                      'downstream': self.downlink_if}[table]
#         ip = re.sub(r'\.[^.]+$', '.0', entry.ip)
#         route_params = (cmd, ip, entry.prefix_len, interface)
#         if cmd == 'add':
#             cmd = ''
#         mac = getattr(self.plconf,
#                       '%s_group_table' % table)[entry.nhop].dmac
#         arp_params = (cmd, interface, entry.ip, mac)
#         for cmd in [(route_template % route_params),
#                     (arp_template % arp_params)]:
#             call_cmd(cmd.split())

#     def mod_group_table(self, cmd, table, entry):
#         interface = {'upstream': self.uplink_if,
#                      'downstream': self.downlink_if}[table]
#         arp_template = 'sudo vppctl set ip arp %s %s %s %s'
#         try:
#             self.extra_l2[entry.dmac]
#         except:
#             self.extra_l2[entry.dmac] = byte_seq(self.extra_ip,
#                                                  self.extra_seq)
#         if cmd == 'add':
#             ip = self.extra_l2.setdefault(entry.dmac,
#                                           byte_seq(self.extra_ip,
#                                                    self.extra_seq))
#             self.extra_seq += 1
#             cmd = ''
#         elif cmd == 'del':
#             ip = self.extra_l2[entry.dmac]
#         arp_params = (cmd, interface, ip, entry.dmac)
#         call_cmd((arp_template % arp_params).split())


class Polycube(object):
    def __init__(self, plconf, bmconf):
        self.webhook_url = 'http://localhost:9000/configured'
        self.plconf = plconf
        self.bmconf = bmconf
        self.pipeline = None

    def get_polycube_config(self):
        return

    def start(self):
        self.start_polycubed()
        self.start_pipeline()

    def stop(self):
        self.pipeline.stop()
        try:
            polycubed_pid = int(subprocess.check_output(['pidof', 'polycubed']))
        except subprocess.CalledProcessError:
            sys.exit('ERROR: Pid of running polycubed instance is not found.')
        polycubed_stop_cmd = ['sudo', 'kill', str(polycubed_pid)]
        call_cmd(polycubed_stop_cmd)

    def start_polycubed(self):
        polycubed_start_cmd = ['sudo', 'polycubed', '-d', '-p', '8000']
        try:
            call_cmd(polycubed_start_cmd)
            os.putenv('POLYCUBECTL_URL', 'http://localhost:8000/polycube/v1/')
            polycubectl_ready = False
            for _ in range(60):
                time.sleep(1)
                cmd = ['polycubectl', 'show', 'version']
                retval = subprocess.call(cmd)
                if not retval:
                    polycubectl_ready = True
                    break
            assert polycubectl_ready
        except:
            sys.exit('ERROR: starting polycubed failed: %s' %
                     ' '.join(polycubed_start_cmd))

    def start_pipeline(self):
        pl = getattr(sys.modules[__name__],
                     'PL_%s' % self.plconf.name)
        self.pipeline = pl(self.plconf, self.bmconf)
        self.pipeline.init()
        self.call_configured_webhook()
        self.pipeline.start()

    def call_configured_webhook(self):
        try:
            requests.get(self.webhook_url)
        except requests.ConnectionError:
            pass


class ObjectView(object):
    def __init__(self, **kwargs):
        tmp = {k.replace('-', '_'): v for k, v in kwargs.items()}
        self.__dict__.update(**tmp)

    def __repr__(self):
        return self.__dict__.__repr__()


def byte_seq(template, seq):
    return template % (int(seq / 254), (seq % 254) + 1)


def signal_handler(signum, frame):
    polycube.stop()


def call_cmd(cmd):
    print(' '.join(cmd))
    return subprocess.run(cmd, check=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--pl-conf', '-p', type=argparse.FileType('r'),
                        help='Pipeline config JSON file',
                        default='./pipeline.json')
    parser.add_argument('--bm-conf', '-b', type=argparse.FileType('r'),
                        help='Benchmark config JSON file',
                        default='./benchmark.json')
    args = parser.parse_args()

    try:
        def conv_fn(d): return ObjectView(**d)
        plconf = json.load(args.pl_conf, object_hook=conv_fn)
        bmconf = json.load(args.bm_conf, object_hook=conv_fn)
    except:
        raise

    polycube = Polycube(plconf, bmconf)

    signal.signal(signal.SIGINT, signal_handler)

    polycube.start()
