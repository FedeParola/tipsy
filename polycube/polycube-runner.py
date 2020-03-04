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
import itertools
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
        actions = ('add', 'del')
        targets = ('user', 'server')
        tasks = ['_'.join(e) for e in itertools.product(actions, targets)]
        # In case other pipelines are added
        table_actions = ('mod_table', 'mod_l3_table', 'mod_group_table')
        while self._running:
            for task in self.plconf.run_time:
                if not self._running:
                    return
                if task.action == 'handover':
                    teid = task.args.user_teid
                    shift = task.args.bst_shift
                    user = [u for u in self.plconf.users if u.teid == teid][0]
                    new_bst = self._calc_new_bst_id(user.tun_end, shift)
                    self.handover(user, new_bst)
                elif task.action in table_actions:
                    self.mod_table(task.action, task.cmd,
                                   task.table, task.entry)
                elif task.action in tasks:
                    getattr(self, task.action)(task.args)
            time.sleep(self.runtime_interval)

    def _calc_new_bst_id(self, cur_bst_id, bst_shift):
        return (cur_bst_id + bst_shift) % len(self.plconf.bsts)

    def add_user(self, user):
        raise NotImplementedError

    def del_user(self, user):
        raise NotImplementedError

    def add_server(self, server):
        raise NotImplementedError

    def del_server(self, server):
        raise NotImplementedError

    def handover(self, user, new_bst):
        raise NotImplementedError

    def mod_table(self, action, cmd, table, entry):
        raise NotImplementedError


class PL_portfwd(PL):
    def init(self):
        call_cmd(['polycubectl', 'simpleforwarder', 'add', 'sf1',
                  'type=XDP_DRV'])
        call_cmd(['polycubectl', 'sf1', 'ports', 'add', 'uport',
                  'peer=' + self.uplink_p])
        call_cmd(['polycubectl', 'sf1', 'ports', 'add', 'dport',
                  'peer=' + self.downlink_p])
        call_cmd(['polycubectl', 'sf1', 'actions', 'add', 'uport',
                  'action=FORWARD', 'outport=dport'])
        call_cmd(['polycubectl', 'sf1', 'actions', 'add', 'dport',
                  'action=FORWARD', 'outport=uport'])

    def _run(self):
        while self._running:
            time.sleep(self.runtime_interval)


class PL_mgw(PL):
    def init(self):
        # Setup service chain
        call_cmd(['polycubectl', 'router', 'add', 'r1', 'type=xdp_drv'])
        call_cmd(['polycubectl', 'r1', 'ports', 'add', 'dport',
                  'peer=' + self.downlink_p, 'ip=' + self.plconf.gw.ip + '/30'])
        call_cmd(['polycubectl', 'r1', 'ports', 'add', 'uport',
                  'peer=' + self.uplink_p, 'ip=140.0.0.1/16'])

        call_cmd(['polycubectl', 'gtphandler', 'add', 'gh1', 'type=xdp_drv'])
        call_cmd(['polycubectl', 'attach', 'gh1', 'r1:dport')]

        call_cmd(['polycubectl', 'policer', 'add', 'p1', 'type=xdp_drv'])
        call_cmd(['polycubectl', 'attach', 'p1', 'r1:dport', 'position=first'])

        call_cmd(['polycubectl', 'classifier', 'add', 'c1', 'type=xdp_drv'])
        call_cmd(['polycubectl', 'attach', 'c1', 'r1:dport', 'position=first'])
        
        # Tipsy generates packets with the same dmac for both downlink
        # and uplink flows
        call_cmd(['polycubectl', 'r1', 'ports', 'dport', 'set',
                  'mac=' + self.plconf.gw.mac])
        call_cmd(['polycubectl', 'r1', 'ports', 'uport', 'set',
                  'mac=' + self.plconf.gw.mac])

        # Add secondary ip on UE port on the same network of BSTs
        call_cmd(['polycubectl', 'r1', 'ports', 'dport', 'secondaryip', 'add',
                  '1.1.255.254/16'])

        # UEs
        for ue in self.plconf.users:
            self.add_user(ue)

        # Add static arp entries for base stations
        for bst in self.plconf.bsts:
            call_cmd(['polycubectl', 'r1', 'arp-table', 'add', bst.ip,
                      'mac=' + bst.mac, 'interface=dport'])

        # Next hops: add static arp entries with custom ip addrs in net 140.0.0.0/16
        # PROBLEM: can't set different smac for every next-hop
        for i, nh in enumerate(self.plconf.nhops):
            call_cmd(['polycubectl', 'mgw1', 'arp-table', 'add', fill_ip('140.0.%d.%d', i, 1), 
                      'mac=' + nh.dmac, 'interface=uport'])

        # Servers
        for srv in self.plconf.srvs:
            self.add_server(srv)

    def add_user(self, user):
        call_cmd(['polycubectl', 'r1', 'route', 'add', user.ip + '/32',
                  self.plconf.bsts[user.tun_end].ip])
        call_cmd(['polycubectl', 'c1', 'traffic-class', 'add', str(user.teid),
                  'priority=0', 'dstip=' + user.ip + '/32'])
        call_cmd(['polycubectl', 'p1', 'contract', 'add', str(user.teid),
                  'action=limit', 'rate-limit=' + str(user.rate_limit),
                  'burst-limit=' + str(user.rate_limit)])
        call_cmd(['polycubectl', 'gh1', 'user-equipment', 'add',
                  user.ip,
                  'tunnel-endpoint=' + self.plconf.bsts[user.tun_end].ip,
                  'teid=' + str(user.teid)])

    def del_user(self, user):
        call_cmd(['polycubectl', 'r1', 'route', 'del', user.ip + '/32',
                  self.plconf.bsts[user.tun_end].ip])
        call_cmd(['polycubectl', 'c1', 'traffic-class', 'del', str(user.teid)])
        call_cmd(['polycubectl', 'p1', 'contract', 'del', str(user.teid)])
        call_cmd(['polycubectl', 'gh1', 'user-equipment', 'del', user.ip])

    def add_server(self, server):
        call_cmd(['polycubectl', 'r1', 'route', 'add',
                  server.ip + '/' + str(server.prefix_len),
                  fill_ip('140.0.%d.%d', server.nhop, 1), 'interface=uport'])

    def del_server(self, server):
        call_cmd(['polycubectl', 'r1', 'route', 'del',
                  server.ip + '/' + str(server.prefix_len),
                  fill_ip('140.0.%d.%d', server.nhop, 1)])

    def handover(self, user, new_bst):
        call_cmd(['polycubectl', 'r1', 'route', 'del', user.ip + '/32',
                  self.plconf.bsts[user.tun_end].ip])
        call_cmd(['polycubectl', 'r1', 'route', 'add', user.ip, 'set',
                  'tunnel-endpoint=' + self.plconf.bsts[new_bst].ip])
        call_cmd(['polycubectl', 'gh1', 'user-equipment', user.ip, 'set',
                  'tunnel-endpoint=' + self.plconf.bsts[new_bst].ip])

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


def fill_ip(template, seq, offset_first=0):
    i = seq + offset_first
    return template % (int(i / 254), (i % 254) + 1)


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
