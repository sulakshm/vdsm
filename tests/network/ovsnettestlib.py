# Copyright 2016-2017 Red Hat, Inc.
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 2 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program; if not, write to the Free Software
# Foundation, Inc., 51 Franklin Street, Fifth Floor, Boston, MA  02110-1301 USA
#
# Refer to the README and COPYING files for full details of the license
#
from __future__ import absolute_import

from vdsm.network import cmd
from vdsm.network.ovs.driver import create


TEST_BRIDGE = 'vdsmbr_test'
TEST_BRIDGES = (TEST_BRIDGE, 'ovs-test-br1')
TEST_BOND = 'bond.ovs.test'

OVS_CTL = '/usr/share/openvswitch/scripts/ovs-ctl'


class OvsService(object):
    def __init__(self):
        rc, out, err = cmd.exec_sync([OVS_CTL, 'status'])
        self.ovs_init_state_is_up = (rc == 0)

    def setup(self):
        if not self.ovs_init_state_is_up:
            cmd.exec_sync([OVS_CTL, 'start'])

    def teardown(self):
        ovsdb = create()
        bridges = ovsdb.list_bridge_info().execute()

        with ovsdb.transaction() as t:
            for bridge in bridges:
                if bridge in TEST_BRIDGES:
                    t.add(ovsdb.del_br(bridge['name']))

        if not self.ovs_init_state_is_up:
            cmd.exec_sync([OVS_CTL, 'stop'])
