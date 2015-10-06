# vim: tabstop=4 shiftwidth=4 softtabstop=4

# Copyright (c) 2013 OpenStack Foundation.
# All Rights Reserved.
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.
#
# @author: Bob Melander, Cisco Systems, Inc.

import json
import httplib
import socket
import netaddr

from oslo.config import cfg

from neutron.api.rpc.agentnotifiers import l3_rpc_agent_api
from neutron.api.v2 import attributes
from neutron.api.rpc.handlers import l3_rpc
from neutron.common import constants as q_const
from neutron.common import rpc as n_rpc
from neutron.common import topics
from neutron.db import common_db_mixin
from neutron.db import extraroute_db
from neutron.db import l3_dvrscheduler_db
from neutron.db import l3_gwmode_db
from neutron.db import l3_hamode_db
from neutron.db import l3_hascheduler_db
from neutron.openstack.common import importutils
from neutron.plugins.common import constants



from neutron.openstack.common import log as logging
LOG = logging.getLogger(__name__)

DEVICE_OWNER_ROUTER_INTF = q_const.DEVICE_OWNER_ROUTER_INTF

SUCCESS_CODES = range(200, 207)
FAILURE_CODES = [0, 301, 302, 303, 400, 401, 403, 404, 500, 501, 502, 503, 504, 505]

ROUTER_PATH = "/routers"
ROUTER_INTERFACE_PATH = "/routers/%s/ports"

NETWORK_DHCP_PATH = "/networks/dhcp"

class KulcloudL3RouterPlugin(common_db_mixin.CommonDbMixin,
                     extraroute_db.ExtraRoute_db_mixin,
                     l3_hamode_db.L3_HA_NAT_db_mixin,
                     l3_gwmode_db.L3_NAT_db_mixin,
                     l3_dvrscheduler_db.L3_DVRsch_db_mixin,
                     l3_hascheduler_db.L3_HA_scheduler_db_mixin):

    """Implementation of the Neutron L3 Router Service Plugin.

    This class implements a L3 service plugin that provides
    router and floatingip resources and manages associated
    request/response.
    All DB related work is implemented in classes
    l3_db.L3_NAT_db_mixin and extraroute_db.ExtraRoute_db_mixin.
    """
    supported_extension_aliases = ["router", "ext-gw-mode",
                                   "extraroute", "l3_agent_scheduler"]

    def __init__(self):
        #qdbapi.register_models(base=model_base.BASEV2)
        self.setup_rpc()
        self.router_scheduler = importutils.import_object(
            cfg.CONF.router_scheduler_driver)
	self.start_periodic_agent_status_check()
        
        self.nbapi_server = '203.255.254.101'
        

        self.nbapi_port = 8181
        self.base_uri = '/1.0/openstack'
        self.time_out = 5
        self.success_codes = SUCCESS_CODES
        self.failure_codes = FAILURE_CODES
	super(KulcloudL3RouterPlugin, self).__init__()

    def setup_rpc(self):
        # RPC support
        self.topic = topics.L3PLUGIN
        self.conn = n_rpc.create_connection(new=True)
        self.agent_notifiers.update(
            {q_const.AGENT_TYPE_L3: l3_rpc_agent_api.L3AgentNotifyAPI()})
        #self.callbacks = L3RouterPluginRpcCallbacks()
        #self.dispatcher = self.callbacks.create_rpc_dispatcher()
	self.endpoints = [l3_rpc.L3RpcCallback()]
        self.conn.create_consumer(self.topic, self.endpoints,
                                  fanout=False)

        self.conn.consume_in_threads()

    def get_plugin_type(self):
        return constants.L3_ROUTER_NAT

    def get_plugin_description(self):
        """returns string description of the plugin."""
        return ("L3 Router Service Plugin for basic L3 forwarding"
                " between (L2) Neutron networks and access to external")

    """ Sample Example 
    def get_routers(self, context, filters=None, fields=None,
                    sorts=None, limit=None, marker=None,
                    page_reverse=False):
	LOG.info(_("KULCLOUD : get_routers"))
	pass
    """


    def create_floatingip(self, context, floatingip):
        """Create floating IP.

        :param context: Neutron request context
        :param floatingip: data fo the floating IP being created
        :returns: A floating IP object on success

        AS the l3 router plugin aysnchrounously creates floating IPs
        leveraging tehe l3 agent, the initial status fro the floating
        IP object will be DOWN.
        """
        return super(L3RouterPlugin, self).create_floatingip(
            context, floatingip,
            initial_status=q_const.FLOATINGIP_STATUS_DOWN)


    def rest_call(self, action, url, data, headers, https=False):
        uri = self.base_uri + url
        body = json.dumps(data)
        if not headers:
            headers = {}
        headers['Content-type'] = 'application/json'
        headers['Accept'] = 'application/json'

        if https is False:
            conn = httplib.HTTPConnection(
                    self.nbapi_server, self.nbapi_port, self.time_out)
        else:
            conn = httplib.HTTPSConnection(
                    self.nbapi_server, 
                    port=self.nbapi_port, timeout=self.time_out)

        if conn is None:
            return 0, None, None, None

        try:
            conn.request(action, uri, body, headers)
            response = conn.getresponse()
            respstr = response.read()
            respdata = respstr
            if response.status in self.success_codes:
                try:
                    respdata = json.loads(respstr)
                except ValueError:
                    pass
            ret = (response.status, response.reason, respstr, respdata)
        except (socket.timeout, socket.error) as e:
            LOG.error(_('RESTCALL: %(action)s failure, %(e)r'),
                        {'action' : action, 'e' : e})
            ret = 0, None, None, None

        conn.close()
        return ret

    def get_port_name(self, port_id, prefix=None, vlan_id=None):
        DEV_NAME_LEN = 11
        DEV_NAME_PREFIX = 'pr-'

        if prefix == None:
            prefix = DEV_NAME_PREFIX

        if vlan_id == None:
            port_name = (prefix + port_id)[:DEV_NAME_LEN]
        else:
            port_name = (prefix + port_id)[:DEV_NAME_LEN] + '.' + str(vlan_id)

        return port_name

    def create_router(self, context, router):
        router_dict = super(KulcloudL3RouterPlugin, self).create_router(
            context, router)

        try:
            result = self.__send_create_router(router_dict['name'], 'openstack')
            return router_dict

        except RuntimeError, e:
            LOG.exception(" ")
            super(KulcloudL3RouterPlugin, self).delete_router(
                context, router_dict['id'])

    def delete_router(self, context, router_id):
        router = super(KulcloudL3RouterPlugin, self).get_router(context,
            router_id, 'name')
        super(KulcloudL3RouterPlugin, self).delete_router(context, router_id)

        try:
            self.__send_delete_router(router['name'])
        except RuntimeError, e:
            LOG.exception(" ")

    def update_router(self, context, id, router):
        r = router['router']
        has_gw_info = False
        if EXTERNAL_GW_INFO in r:
            has_gw_info = True
            gw_info = r[EXTERNAL_GW_INFO]
            del r[EXTERNAL_GW_INFO]
        with context.session.begin(subtransactions=True):
            if has_gw_info:
                self._update_router_gw_info(context, id, gw_info)
            router_db = self._get_router(context, id)
            # Ensure we actually have something to update
            if r.keys():
                router_db.update(r)
        return self._make_router_dict(router_db)

    def add_router_interface(self, context, router_id, interface_info):
        if not interface_info:
            msg = _("Either subnet_id or port_id must be specified")
            raise n_exc.BadRequest(resource='router', msg=msg)

        router = self.get_router(context, router_id)
        _router = self._get_router(context, router_id)
        router_name = router['name']

        if 'port_id' in interface_info:
            with context.session.begin(subtransactions=True):
                if 'subnet_id' in interface_info:
                    msg = _("Cannot specify both subnet-id and port-id")
                    raise n_exc.BadRequest(resource='router', msg=msg)

                port = self._core_plugin._get_port(context,
                                                   interface_info['port_id'])
                if port['device_id']:
                    raise n_exc.PortInUse(net_id=port['network_id'],
                                          port_id=port['id'],
                                          device_id=port['device_id'])
                fixed_ips = [ip for ip in port['fixed_ips']]
                if len(fixed_ips) != 1:
                    msg = _('Router port must have exactly one fixed IP')
                    raise n_exc.BadRequest(resource='router', msg=msg)
                subnet_id = fixed_ips[0]['subnet_id']
                subnet = self._core_plugin._get_subnet(context, subnet_id)

                #TODO : Exception handling
                self._check_for_dup_router_subnet(context, _router,
                                                  port['network_id'],
                                                  subnet_id,
                                                  subnet['cidr'])

                network = self._core_plugin._get_network(context,
                                                  subnet['network_id'])
                network_dict = self._core_plugin._make_network_dict(network)
                self._core_plugin._extend_network_dict_provider(context,
                                                  network_dict)

                vlan_id = network_dict['provider:segmentation_id']
                intf_name = "pr-vlan%d" % vlan_id if vlan_id != None else None
                port.update({'device_id': router_id,
                             'device_owner': DEVICE_OWNER_ROUTER_INTF,
                             'name': intf_name,
                             'status': 'ACTIVE'})
                
        elif 'subnet_id' in interface_info:
            subnet_id = interface_info['subnet_id']
            subnet = self._core_plugin._get_subnet(context, subnet_id)
            if not subnet['gateway_ip']:
                msg = _('Subnet for router interface must have a gateway IP')
                raise n_exc.BadRequest(resource='router', msg=msg)

            #TODO : Exception handling
            self._check_for_dup_router_subnet(context, _router,
                                              subnet['network_id'],
                                              subnet_id,
                                              subnet['cidr'])
            fixed_ip = {'ip_address' : subnet['gateway_ip'],
                        'subnet_id' : subnet['id']}
            
            network = self._core_plugin._get_network(context,
                                                subnet['network_id'])
            network_dict = self._core_plugin._make_network_dict(network)
            self._core_plugin.type_manager._extend_network_dict_provider(context,
                                                network_dict)

            vlan_id = network_dict['provider:segmentation_id']
            intf_name = "pr-vlan%d" % vlan_id if vlan_id else None

            port = self._core_plugin.create_port(context, {
                'port':
                {'tenant_id' : subnet['tenant_id'],
                 'network_id' : subnet['network_id'],
                 'fixed_ips' : [fixed_ip],
                 'mac_address' : attributes.ATTR_NOT_SPECIFIED,

                 'admin_state_up' : True,
                 'device_id' : router_id,
                 'device_owner' : DEVICE_OWNER_ROUTER_INTF,
                 'name' : intf_name}})

        ip = port['fixed_ips'][0]['ip_address']
        cidr = subnet['cidr']

        try:
            self.__send_add_router_interface(router_name, intf_name,
                                             ip, cidr, vlan_id)
            self.__run_dhcp_process(intf_name, ip, cidr, network)

            with context.session.begin(subtransactions=True):
                port = self._core_plugin._get_port(context, port['id'])
                port.update({'status' : 'ACTIVE'})

            info = {'id' : router_id,
                    'tenant_id' : subnet['tenant_id'],
                    'port_id' : port['id'],
                    'subnet_id' : subnet_id}
            return info
        except RuntimeError, e:
            self.remove_router_interface(context, router_id, interface_info)
            raise RuntimeError(e.message)



    def remove_router_interface(self, context, router_id, interface_info):
        if not interface_info:
            msg = _("Either subnet_id or port_id must be specified")
            raise n_exc.BadRequest(resource='router', msg=msg)
        if 'port_id' in interface_info:
            port_id = interface_info['port_id']
            port_db = self._core_plugin._get_port(context, port_id)
            if not (port_db['device_owner'] == DEVICE_OWNER_ROUTER_INTF and
                    port_db['device_id'] == router_id):
                raise l3.RouterInterfaceNotFound(router_id=router_id,
                                                 port_id=port_id)
            if 'subnet_id' in interface_info:
                port_subnet_id = port_db['fixed_ips'][0]['subnet_id']
                if port_subnet_id != interface_info['subnet_id']:
                    raise n_exc.SubnetMismatchForPort(
                        port_id=port_id,
                        subnet_id=interface_info['subnet_id'])
            subnet_id = port_db['fixed_ips'][0]['subnet_id']
            subnet = self._core_plugin._get_subnet(context, subnet_id)
            self._confirm_router_interface_not_in_use(
                context, router_id, subnet_id)
            self._core_plugin.delete_port(context, port_db['id'],
                                          l3_port_check=False)
        elif 'subnet_id' in interface_info:
            subnet_id = interface_info['subnet_id']
            self._confirm_router_interface_not_in_use(context, router_id,
                                                      subnet_id)

            subnet = self._core_plugin._get_subnet(context, subnet_id)
            found = False

            try:
                rport_qry = context.session.query(models_v2.Port)
                ports = rport_qry.filter_by(
                    device_id=router_id,
                    device_owner=DEVICE_OWNER_ROUTER_INTF,
                    network_id=subnet['network_id'])

                for p in ports:
                    if p['fixed_ips'][0]['subnet_id'] == subnet_id:
                        port_id = p['id']
                        self._core_plugin.delete_port(context, p['id'],
                                                      l3_port_check=False)
                        found = True
                        break
            except exc.NoResultFound:
                pass

            if not found:
                raise l3.RouterInterfaceNotFoundForSubnet(router_id=router_id,
                                                          subnet_id=subnet_id)
        info = {'id': router_id,
                'tenant_id': subnet['tenant_id'],
                'port_id': port_id,
                'subnet_id': subnet_id}

        router_name = self.get_router(context, router_id)['name']
        network = self._core_plugin.get_network(context,
                                                subnet['network_id'])
        vlan_id = network['provider:segmentation_id']
        port_name = "pr-vlan%d" % vlan_id
        self.__delete_router_interface(router_name, port_name)
        self.__send_kill_dhcp_process(port_name)
        return info


    def _update_dhcp_info(self, intf_name, network):
        hosts_info = self.__make_hosts_info(network)
        addn_hosts_info = self.__make_addn_hosts_info(network)
        opts_info = self.__make_dhcp_opts_info(network)

        self.__send_update_dhcp_info(intf_name,
                                     hosts_info,
                                     addn_hosts_info,
                                     opts_info)


    def __iter_hosts(self, network):
        dhcp_domain = "openstacklocal"
        
        for port in network.ports:
            for alloc in port.fixed_ips:
                hostname = 'host-%s' % alloc.ip_address.replace(
                    '.', '-').replace(':', '-')
                name = '%s.%s' % (hostname, dhcp_domain)
                yield (port, alloc, hostname, name)



    def __make_hosts_info(self, network):
        # static value
        result = []

        for (port, alloc, hostname, name) in self.__iter_hosts(network):
            if getattr(port, 'extra_dhcp_opts', False):
                # Don't check version
                result.append( dict( mac_address = port.mac_address,
                                     name = name,
                                     ip_address = alloc.ip_address,
                                     set_tag = 'set:',
                                     port_id = port.id) )
            else:
                result.append( dict( mac_address = port.mac_address,
                                     name = name,
                                     ip_address = alloc.ip_address) )

        return result


    
    def __make_addn_hosts_info(self, network):
        result = []

        for (port, alloc, hostname, name) in self.__iter_hosts(network):
            result.append( dict( ip_address = alloc.ip_address,
                                 name = name,
                                 hostname = hostname ) )

        return result


    def __format_option(self, tag, option, *args):
        # Ignore dnsmasq's version
        set_tag = 'tag:'

        option = str(option)

        if isinstance(tag, int):
            tag = 'tag%d' % tag

        if not option.isdigit():
            option = 'option:%s' % option

        return ','.join((set_tag + tag, '%s' % option) + args)


    def __make_dhcp_opts_info(self, network):
        # Ignore enable_isolated_metadata opt in dhcp_agent.ini
        options = []

        isolated_subnets = {}
        subnets = dict((subnet.id, subnet) for subnet in network.subnets)
        for port in network.ports:
            if port.device_owner != q_const.DEVICE_OWNER_ROUTER_INTF:
                continue
            for alloc in port.fixed_ips:
                if subnets[alloc.subnet_id].gateway_ip == alloc.ip_address:
                    isolated_subnets[alloc.subnet_id] = False

        dhcp_ips = {}
        subnet_idx_map = {}
        for i, subnet in enumerate(network.subnets):
            if not subnet.enable_dhcp:
                continue
            if subnet.dns_nameservers:
                options.append(
                    self.__format_option(i, 'dns-server',
                                         ','.join(subnet.dns_nameservers)))
            else:
                subnet_idx_map[subnet.id] = i

            gateway = subnet.gateway_ip
            host_routes = []
            if hasattr(subnet, 'host_routes'):
                for hr in subnet.host_routes:
                    if hr.destination == "0.0.0.0/0":
                        if not gateway:
                            gateway = hr.nexthop
                    else:
                        host_routes.append("%s,%s" % (hr.destination, hr.nexthop))

            # Ignore enable_isolated_metadata opt 
            """
            if (isolated_subnets[subnet.id] and
                    self.conf.enable_isolated_metadata and
                    subnet.ip_version == 4):
                subnet_dhcp_ip = subnet_to_interface_ip[subnet.id]
                host_routes.append(
                    '%s/32,%s' % (METADATA_DEFAULT_IP, subnet_dhcp_ip)
                )
            """
            WIN2k3_STATIC_DNS = 249

            if host_routes:
                if gateway and subnet.ip_version == 4:
                    host_routes.append("%s,%s" % ("0.0.0.0/0", gateway))
                options.append(
                    self.__format_option(i, "classless-static-route",
                                         ','.join(host_routes)))
                options.append(
                    self.__format_option(i, WIN2k3_STATIC_DNS,
                                         ','.join(host_routes)))

            if subnet.ip_version == 4:
                if gateway:
                    options.append(self.__format_option(i, 'router', gateway))
                else:
                    options.append(self.__format_option(i, 'router'))

        for port in network.ports:
            if getattr(port, 'extra_dhcp_opts', False):
                options.extend(
                    self.__format_option(port.id, opt.opt_name, opt.opt_value)
                    for opt in port.extra_dhcp_opts)

            if port.device_owner == q_const.DEVICE_OWNER_DHCP:
                for ip in port.fixed_ips:
                    i = subnet_idx_map.get(ip.subnet_id)
                    if i is None:
                        continue
                    dhcp_ips[i] = list()
                    dhcp_ips[i].append(ip.ip_address)

        for i, ips in dhcp_ips.items():
            if len(ips) > 1:
                options.append(self.__format_option(i,
                                                    'dns-server',
                                                    ','.join(ips)))

        return options
                



    def __run_dhcp_process(self, intf_name, ip, cidr, network):
        hosts_info = self.__make_hosts_info(network)
        addn_hosts_info = self.__make_addn_hosts_info(network)
        opts_info = self.__make_dhcp_opts_info(network)
        self.__send_run_dhcp_process(intf_name, ip, cidr, 
                                     hosts_info, addn_hosts_info, opts_info)




    def __send_create_router(self, name, protocol, 
                                   as_id=None, router_id=None):
        url = ROUTER_PATH
        body = dict(router_name = name,
                    routing_protocol = protocol)
        if as_id is not None:
            body['as_id'] = as_id
        if router_id is not None:
            body['router_id'] = router_id

        res = self.rest_call("POST", url, body, None)

        if res[0] not in SUCCESS_CODES:
            raise RuntimeError(res)

        return res

    def __send_delete_router(self, name):
        url = ROUTER_PATH + "/%s" % name

        res = self.rest_call("DELETE", url, None, None)
        if res[0] not in SUCCESS_CODES:
            raise RuntimeError(res)

    def __send_add_router_interface(self, name, intf_name, 
                                    ip, cidr, segment_id=None):
        url = ROUTER_PATH + "/%s/ports" % name
        
        body = dict(interfaces=list())
        body["interfaces"].append(dict(intf_name = intf_name,
                                       ip_address = ip,
                                       network_cidr = cidr,
                                       vlan_id = segment_id,
                                       type = 'openstack'))
        res = self.rest_call("POST", url, body, None)
        if res[0] not in SUCCESS_CODES:
            raise RuntimeError(res)
        return res


    def __send_run_dhcp_process(self, intf_name, ip, cidr,
                                hosts=None, addn_hosts=None, opts=None):
        url = NETWORK_DHCP_PATH
        body = dict( intf_name = intf_name,
                     ip_address = ip,
                     network_cidr = cidr,
                     hosts = hosts,
                     addn_hosts = addn_hosts,
                     opts = opts )
        res = self.rest_call("POST", url, body, None)
        if res[0] not in SUCCESS_CODES:
            raise RuntimeError(res)

        return res



    def __send_kill_dhcp_process(self, intf_name):
        url = "%s/%s" % (NETWORK_DHCP_PATH, intf_name)
        res = self.rest_call("DELETE", url, None, None)
        if res[0] not in SUCCESS_CODES:
            raise RuntimeError(res)

        return res




    def __delete_router_interface(self, router_name, port_name):
        url = ROUTER_PATH + "/%s/ports/%s" % (router_name, port_name)
        res = self.rest_call("DELETE", url, None, None)
        if res[0] not in SUCCESS_CODES:
            pass


    def __send_update_dhcp_info(self, intf_name, hosts, addn_hosts, opts):
        url = "/networks/dhcp/%s" % intf_name
        body = dict( hosts = hosts,
                     addn_hosts = addn_hosts,
                     opts = opts )
        res = self.rest_call("PUT", url, body, None)
        if res[0] not in SUCCESS_CODES:
            raise RuntimeError(res)

        return res

