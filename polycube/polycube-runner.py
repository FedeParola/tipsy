#!/usr/bin/env python3

# TIPSY: Telco pIPeline benchmarking SYstem
#
# Copyright (C) 2020 by ?
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
                  'type=xdp_drv'])
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
        # Setup router
        router = {}
        router['type'] = 'xdp_drv'
        router['ports'] = [
            {
                'name': 'dport',
                'peer': self.downlink_p,
                'mac': self.plconf.gw.mac,
                'ip': self.plconf.gw.ip + '/30',
                'secondaryip': [
                    {'ip': '1.1.255.254/16'}
                ]
            },
            {
                'name': 'uport',
                'peer': self.uplink_p,
                'mac': self.plconf.gw.mac,
                'ip': '140.0.0.1/16'
            }
        ]

        # Setup gtphandler
        gtphandler = {}
        gtphandler['type'] = 'xdp_drv'
        gtphandler['user-equipment'] = []

        # Setup policer
        policer = {}
        policer['type'] = 'xdp_drv'
        policer['contract'] = []

        # Setup classifier
        classifier = {}
        classifier['type'] = 'xdp_drv'
        classifier['traffic-class'] = []

        # Create cubes
        r = requests.post('http://localhost:8000/polycube/v1/router/r1',
                          json.dumps(router))
        print(r.text)
        r = requests.post('http://localhost:8000/polycube/v1/gtphandler/gh1',
                          json.dumps(gtphandler))
        print(r.text)
        r = requests.post('http://localhost:8000/polycube/v1/policer/p1',
                          json.dumps(policer))
        print(r.text)
        r = requests.post('http://localhost:8000/polycube/v1/classifier/c1',
                          json.dumps(classifier))
        print(r.text)

        # Add static arp entries for base stations
        arp_table = "["
        for i, bst in enumerate(self.plconf.bsts):
            if i > 0:
                arp_table += ','
            arp_table += json.dumps({
                'address': bst.ip,
                'mac': bst.mac,
                'interface': 'dport'
            })
        
        # Next hops: add static arp entries with custom ip addrs in net 140.0.0.0/16
        # PROBLEM: can't set different smac for every next-hop
        for i, nh in enumerate(self.plconf.nhops):
            arp_table += ','
            arp_table += json.dumps({
                'address': fill_ip('140.0.%d.%d', i, 1),
                'mac': nh.dmac,
                'interface': 'uport'
            })

        arp_table += ']'
        r = requests.post(
                'http://localhost:8000/polycube/v1/router/r1/arp-table',
                arp_table)
        print(r.text)

        # Setup UEs
        self.add_users(self.plconf.users)

        # Setup servers
        self.add_servers(self.plconf.srvs)

        # Connect cubes
        call_cmd(['polycubectl', 'attach', 'gh1', 'r1:dport'])
        call_cmd(['polycubectl', 'attach', 'p1', 'r1:dport', 'position=first'])
        call_cmd(['polycubectl', 'attach', 'c1', 'r1:dport', 'position=first'])

    def add_users(self, users):
        routes = '['
        classes = '['
        contracts = '['
        ues = '['

        for i, u in enumerate(users):
            if i > 0:
                routes += ','
                classes += ','
                contracts += ','
                ues += ','

            routes += json.dumps({
                'network': u.ip + '/32',
                'nexthop': self.plconf.bsts[u.tun_end].ip
            })
            classes += json.dumps({
                'id': u.teid,
                'priority': 0,
                'direction': 'egress',
                'dstip': u.ip + '/32'
            })
            contracts += json.dumps({
                'traffic-class': u.teid,
                'action': 'limit',
                'rate-limit': u.rate_limit,
                'burst-limit': u.rate_limit
            })
            ues += json.dumps({
                'ip': u.ip,
                'tunnel-endpoint': self.plconf.bsts[u.tun_end].ip,
                'teid': u.teid
            })

        routes += ']'
        classes += ']'
        contracts += ']'
        ues += ']'

        r = requests.post(
                'http://localhost:8000/polycube/v1/router/r1/route',
                routes)
        print(r.text)
        r = requests.post(
                'http://localhost:8000/polycube/v1/classifier/c1/traffic-class',
                classes)
        print(r.text)
        r = requests.post(
                'http://localhost:8000/polycube/v1/policer/p1/contract',
                contracts)
        print(r.text)
        r = requests.post(
                'http://localhost:8000/polycube/v1/gtphandler/gh1/user-equipment',
                ues)
        print(r.text)

    def add_servers(self, servers):
        routes = '['
        for i, srv in enumerate(servers):
            if i > 0:
                routes += ','
            routes += json.dumps({
                'network': srv.ip + '/' + str(srv.prefix_len),
                'nexthop': fill_ip('140.0.%d.%d', srv.nhop, 1),
            })
        routes += ']'

        r = requests.post(
                'http://localhost:8000/polycube/v1/router/r1/route',
                routes)
        print(r.text)

    def add_user(self, user):
        call_cmd(['polycubectl', 'r1', 'route', 'add', user.ip + '/32',
                  self.plconf.bsts[user.tun_end].ip])
        call_cmd(['polycubectl', 'c1', 'traffic-class', 'add', str(user.teid),
                  'priority=0', 'direction=egress', 'dstip=' + user.ip + '/32'])
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
            polycubed_stop_cmd = ['sudo', 'kill', str(polycubed_pid)]
            call_cmd(polycubed_stop_cmd)
            polycubed_stopped = False
            for _ in range(60):
                time.sleep(1)
                retval = subprocess.call(['pidof', 'polycubed'])
                if retval:
                    polycubed_stopped = True
                    break
            assert polycubed_stopped
        except subprocess.CalledProcessError:
            sys.exit('ERROR: pid of running polycubed instance not found')
        except:
            sys.exit('ERROR: stopping polycubed failed')

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

        # Set the number of cores
        if self.plconf.core > 0:
            try:
                call_cmd(['sudo', 'killall', 'irqbalance'])
                cores = '0-' + str(self.plconf.core - 1)
                set_cores_cmd = ['sudo',
                        self.bmconf['tipsy-dir'] +
                        '/polycube/set_irq_affinity.sh'],
                        cores,
                        self.bmconf['downlink-port'],
                        self.bmconf['uplink']
                call_cmd(set_cores_cmd)
            
            except:
                return subprocess.run("sudo killall polycubed")
                sys.exit('ERROR: setting cores count failed: %s' %
                         ' '.join(set_cores_cmd))

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
