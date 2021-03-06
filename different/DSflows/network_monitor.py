# conding=utf-8
from __future__ import division
from operator import attrgetter
from ryu import cfg
from ryu.base import app_manager
from ryu.base.app_manager import lookup_service_brick
from ryu.controller import ofp_event
from ryu.controller.handler import MAIN_DISPATCHER, DEAD_DISPATCHER
from ryu.controller.handler import set_ev_cls
from ryu.ofproto import ofproto_v1_3
from ryu.lib import hub
import setting
import time


CONF = cfg.CONF


class NetworkMonitor(app_manager.RyuApp):
    """
        NetworkMonitor is a Ryu app for collecting traffic information.

    """
    OFP_VERSIONS = [ofproto_v1_3.OFP_VERSION]

    def __init__(self, *args, **kwargs):
        super(NetworkMonitor, self).__init__(*args, **kwargs)
        self.name = 'monitor'
        self.datapaths = {}
        self.port_stats = {}
        self.port_info = {}
        self.flow_stats = {}
        self.elephant_info = {}
        self.awareness = lookup_service_brick('awareness')
        self.graph = None
        self.capabilities = None
        self.best_paths = None
        self.monitor_thread = hub.spawn(self._monitor)

    @set_ev_cls(ofp_event.EventOFPStateChange,
                [MAIN_DISPATCHER, DEAD_DISPATCHER])
    def _state_change_handler(self, ev):
        """
            Record datapath's info
        """
        datapath = ev.datapath
        if ev.state == MAIN_DISPATCHER:
            if not datapath.id in self.datapaths:
                self.logger.debug('register datapath: %016x', datapath.id)
                self.datapaths[datapath.id] = datapath
        elif ev.state == DEAD_DISPATCHER:
            if datapath.id in self.datapaths:
                self.logger.debug('unregister datapath: %016x', datapath.id)
                del self.datapaths[datapath.id]

    def _monitor(self):
        """
            Main entry method of monitoring traffic.
        """
        while True:
            self.elephant_info = {}
            for dp in self.datapaths.values():
                self._request_stats(dp)
                self.capabilities = None
                self.best_paths = None
            self.ifChange = False
            hub.sleep(0.5)
            self.link_status()
            hub.sleep(setting.MONITOR_PERIOD)
            # self.show_stat()

    def _request_stats(self, datapath):
        """
            Sending request msg to datapath
        """
        self.logger.debug('send stats request: %016x', datapath.id)
        ofproto = datapath.ofproto
        parser = datapath.ofproto_parser

        # req = parser.OFPPortDescStatsRequest(datapath, 0)
        # datapath.send_msg(req)

        req = parser.OFPPortStatsRequest(datapath, 0, ofproto.OFPP_ANY)
        datapath.send_msg(req)

        req = parser.OFPFlowStatsRequest(datapath)
        datapath.send_msg(req)

    def get_port(self, dst_ip, access_table):
        """
            Get access port if dst host.
            access_table: {(sw,port) :(ip, mac)}
        """
        if access_table:
            if isinstance(access_table.values()[0], tuple):
                for key in access_table.keys():
                    if dst_ip == access_table[key][0]:
                        dst_port = key[1]
                        return dst_port
        return None

    def add_flow(self, dp, p, match, actions, idle_timeout=0, hard_timeout=0):
        """
            Send a flow entry to datapath.
        """
        ofproto = dp.ofproto
        parser = dp.ofproto_parser

        inst = [parser.OFPInstructionActions(ofproto.OFPIT_APPLY_ACTIONS,
                                             actions)]

        mod = parser.OFPFlowMod(datapath=dp, priority=p,
                                idle_timeout=idle_timeout,
                                hard_timeout=hard_timeout,
                                match=match, instructions=inst)
        dp.send_msg(mod)

    def send_flow_mod(self, datapath, flow_info, src_port, dst_port, flow_tcp, priority):
        """
            Build flow entry, and send it to datapath.
        """
        parser = datapath.ofproto_parser
        actions = []
        actions.append(parser.OFPActionOutput(dst_port))

        if  flow_tcp == 'dst':
            match = parser.OFPMatch(
                in_port=src_port, eth_type=flow_info[0],
                ipv4_src=flow_info[1], ipv4_dst=flow_info[2], ip_proto=17, udp_dst=5001)
        else:
            match = parser.OFPMatch(
                in_port=src_port, eth_type=flow_info[0],
                ipv4_src=flow_info[1], ipv4_dst=flow_info[2], ip_proto=17, udp_src=5001)

        self.add_flow(datapath, priority + 1, match, actions,
                      idle_timeout=15, hard_timeout=120)

    def get_port_pair_from_link(self, link_to_port, src_dpid, dst_dpid):
        """
            Get port pair of link, so that controller can install flow entry.
        """
        if (src_dpid, dst_dpid) in link_to_port:
            return link_to_port[(src_dpid, dst_dpid)]
        else:
            self.logger.info("dpid:%s->dpid:%s is not in links" % (
                             src_dpid, dst_dpid))
            return None

    def install_flow(self, datapaths, link_to_port, access_table, path,
                     flow_info, priority):
        '''
            Install flow entires for roundtrip: go and back.
            @parameter: path=[dpid1, dpid2...]
                        flow_info=(eth_type, src_ip, dst_ip, in_port)
        '''
        if path is None or len(path) == 0:
            self.logger.info("Path error!")
            return
        in_port = flow_info[3]
        first_dp = datapaths[path[0]]
        out_port = first_dp.ofproto.OFPP_LOCAL
        back_info = (flow_info[0], flow_info[2], flow_info[1])

        # inter_link
        if len(path) > 2:
            for i in xrange(1, len(path)-1):
                port = self.get_port_pair_from_link(link_to_port,
                                                    path[i-1], path[i])
                port_next = self.get_port_pair_from_link(link_to_port,
                                                         path[i], path[i+1])
                if port and port_next:
                    src_port, dst_port = port[1], port_next[0]
                    datapath = datapaths[path[i]]
                    self.send_flow_mod(datapath, flow_info, src_port, dst_port, 'dst', priority)
                    self.send_flow_mod(datapath, back_info, dst_port, src_port, 'src', priority)
                    self.logger.debug("inter_link flow install")
        if len(path) > 1:
            # the last flow entry: tor -> host
            port_pair = self.get_port_pair_from_link(link_to_port,
                                                     path[-2], path[-1])
            if port_pair is None:
                self.logger.info("Port is not found")
                return
            src_port = port_pair[1]

            dst_port = self.get_port(flow_info[2], access_table)
            if dst_port is None:
                self.logger.info("Last port is not found.")
                return

            last_dp = datapaths[path[-1]]
            self.send_flow_mod(last_dp, flow_info, src_port, dst_port, 'dst', priority)
            self.send_flow_mod(last_dp, back_info, dst_port, src_port, 'src', priority)

            # the first flow entry
            port_pair = self.get_port_pair_from_link(link_to_port,
                                                     path[0], path[1])
            if port_pair is None:
                self.logger.info("Port not found in first hop.")
                return
            out_port = port_pair[0]
            self.send_flow_mod(first_dp, flow_info, in_port, out_port, 'dst', priority)
            self.send_flow_mod(first_dp, back_info, out_port, in_port, 'src', priority)

        # src and dst on the same datapath
        else:
            out_port = self.get_port(flow_info[2], access_table)
            if out_port is None:
                self.logger.info("Out_port is None in same dp")
                return
            self.send_flow_mod(first_dp, flow_info, in_port, out_port, 'dst', priority)
            self.send_flow_mod(first_dp, back_info, out_port, in_port, 'src', priority)

    def find_max_bandwidth(self, src, dst):
        """
            find max bandwidth of flow
        """
        shortest_paths = self.awareness.shortest_paths
        src_location = self.awareness.get_host_location(src)[0]
        dst_location = self.awareness.get_host_location(dst)[0]
        paths = shortest_paths.get(src_location).get(dst_location)

        max_bandwidth = 0
        max_bandwidth_path = paths[0]

        if (len(paths) > 1):
            for path in paths:
                path_length = len(path)
                min_bandwidth = setting.MAX_CAPACITY
                if path_length > 1:
                    for i in xrange(path_length - 1):
                        pre = path[i]
                        cur = path[i + 1]
                        link = (pre, cur)
                        (src_port, dst_port) = self.awareness.link_to_port[link]
                        link_left_bandwidth = self.port_info[(pre, src_port)]
                        min_bandwidth = min(link_left_bandwidth, min_bandwidth)
                if min_bandwidth > max_bandwidth:
                    max_bandwidth = min_bandwidth
                    max_bandwidth_path = path

        max_bandwidth = setting.MAX_CAPACITY - max_bandwidth

        return max_bandwidth

    def change_flow_path(self, link, src, src_port):
        """
            find the flow and reroute flow
        """
        link_flow = self.elephant_info[link]
        choose_flow = {}

        for flow in link_flow:
            srcs = flow[0]
            dsts = flow[1]
            speed = link_flow[flow]
            max_bandwidth = self.find_max_bandwidth(srcs, dsts)
            carry_bandwidth = speed + max_bandwidth
            if carry_bandwidth > setting.MAX_CAPACITY * 0.8:
                choose = 0
            else:
                choose = round(speed / (max_bandwidth + speed), 2)
            choose_flow[(srcs, dsts)] = choose
            # self.logger.info(max_bandwidth)
            # self.logger.info(carry_bandwidth)

        sorted(choose_flow)
        self.logger.info(choose_flow)
        choose_src_dst = choose_flow.keys()
        self.logger.info(choose_src_dst)
        bandwidth = self.port_info[(src, src_port)]
        while bandwidth < setting.MAX_CAPACITY * 0.2:
            flow = choose_src_dst[0]
            bandwidth = 100

    def link_status(self):
        """
            check load of links
        """
        for link in self.awareness.link_to_port:
            (src, dst) = link
            (src_port, dst_port) = self.awareness.link_to_port[link]
            bandwidth = self.port_info[(src, src_port)]
            if bandwidth < setting.MAX_CAPACITY * 0.2:
                self.change_flow_path(link, src, src_port)

    def _save_stats(self, _dict, key, value, length):
        if key not in _dict:
            _dict[key] = []
        _dict[key].append(value)

        if len(_dict[key]) > length:
            _dict[key].pop(0)

    def _get_speed(self, now, pre, period):
        if period:
            return (now - pre) / (period)
        else:
            return 0

    def _get_free_bw(self, capacity, speed):
        # BW:Mbit/s
        return max(capacity/10**3 - speed * 8/10**6, 0)

    def _get_time(self, sec, nsec):
        return sec + nsec / (10 ** 9)

    def _get_period(self, n_sec, n_nsec, p_sec, p_nsec):
        return self._get_time(n_sec, n_nsec) - self._get_time(p_sec, p_nsec)

    @set_ev_cls(ofp_event.EventOFPFlowStatsReply, MAIN_DISPATCHER)
    def _flow_stats_reply_handler(self, ev):
        """
            Save flow stats reply info into self.flow_stats.
            Calculate flow speed and Save it.
        """
        body = ev.msg.body
        dpid = ev.msg.datapath.id
        self.flow_stats.setdefault(dpid, {})
        for stat in sorted([flow for flow in body if ((flow.priority not in [0, 65535]) and (flow.match.get('ipv4_src')) and (flow.match.get('ipv4_dst')))],
                           key=lambda flow: (flow.match.get('in_port'),
                                             flow.match.get('ipv4_dst'))):
            src = str(stat.match.get('ipv4_src'))
            dst = str(stat.match.get('ipv4_dst'))
            if str(dpid).startswith('2'):
                in_port = stat.match.get('in_port')
                out_port = stat.instructions[0].actions[0].port
                key = (dpid, src, dst)
                value = (stat.byte_count, time.time())
                self._save_stats(self.flow_stats[dpid], key, value, 2)

                pre = 0
                period = setting.MONITOR_PERIOD + 0.5
                tmp = self.flow_stats[dpid][key]
                now = tmp[-1][0]
                if len(tmp) > 1:
                    pre = tmp[-2][0]
                    period = tmp[-1][1] - tmp[-2][1]

                data = (now - pre) * 8 / (1024 * 1024)
                speed = round(data / period, 2)

                # self.logger.info(speed)

                if speed > 0:
                    if speed > setting.MAX_CAPACITY * 0.1:
                        src_dpid = self.awareness.link_in_to[(dpid, in_port)]
                        dst_dpid = self.awareness.link_in_to[(dpid, out_port)]
                        if (src_dpid, dpid) not in self.elephant_info:
                            self.elephant_info[(src_dpid, dpid)] = {}
                        if (dpid, dst_dpid) not in self.elephant_info:
                            self.elephant_info[(dpid, dst_dpid)] = {}
                        self.elephant_info[(src_dpid, dpid)][(src, dst)] = speed
                        self.elephant_info[(dpid, dst_dpid)][(src, dst)] = speed
                        # self.logger.info(self.elephant_info)
                        # self.elephant_info[key] = speed

    @set_ev_cls(ofp_event.EventOFPPortStatsReply, MAIN_DISPATCHER)
    def _port_stats_reply_handler(self, ev):
        """
            Save port's stats info
            Calculate port's speed and save it.
        """
        body = ev.msg.body
        dpid = ev.msg.datapath.id

        for stat in sorted(body, key=attrgetter('port_no')):
            port_no = stat.port_no
            if port_no != ofproto_v1_3.OFPP_LOCAL:
                key = (dpid, port_no)
                value = (stat.tx_bytes, stat.rx_bytes, time.time())

                self._save_stats(self.port_stats, key, value, 2)

                # Get port speed.
                tmp = self.port_stats[key]

                if key not in self.port_info:
                    self.port_info[key] = 0

                if len(tmp) > 1:

                    preTx = tmp[-2][0]
                    nowTx = tmp[-1][0]
                    preTime = tmp[-2][2]
                    nowTime = tmp[-1][2]
                    datas = (nowTx - preTx) * 8 / (1024 * 1024)
                    times = nowTime - preTime
                    speedTx = round(datas / times, 2)

                    bandwidth = setting.MAX_CAPACITY - speedTx
                    self.port_info[key] = bandwidth

    @set_ev_cls(ofp_event.EventOFPPortStatus, MAIN_DISPATCHER)
    def _port_status_handler(self, ev):
        """
            Handle the port status changed event.
        """
        msg = ev.msg
        reason = msg.reason
        port_no = msg.desc.port_no
        dpid = msg.datapath.id
        ofproto = msg.datapath.ofproto

        reason_dict = {ofproto.OFPPR_ADD: "added",
                       ofproto.OFPPR_DELETE: "deleted",
                       ofproto.OFPPR_MODIFY: "modified", }

        if reason in reason_dict:

            print "switch%d: port %s %s" % (dpid, reason_dict[reason], port_no)
        else:
            print "switch%d: Illeagal port state %s %s" % (port_no, reason)

    def show_stat(self):
        '''
            Show statistics info according to data type.
            type: 'port' 'flow'
        '''
        if setting.TOSHOW is False:
            return

        if(self.stats['flow']):
            bodys = self.stats['flow']
            print('datapath ''  port    ip-dst      '
                  'out-port packets  bytes  flow-speed(B/s)')
            print('---------------- ''  -------- ----------------- '
                  '-------- -------- -------- -----------')
            for dpid in bodys.keys():
                for stat in sorted(
                    [flow for flow in bodys[dpid] if flow.priority == 1],
                    key=lambda flow: (flow.match.get('in_port'),
                                      flow.match.get('ipv4_dst'))):
                    print('%6s %6x %10s %8x %8d %8d %8.1f' % (
                        dpid,
                        stat.match['in_port'], stat.match['ipv4_dst'],
                        stat.instructions[0].actions[0].port,
                        stat.packet_count, stat.byte_count,
                        abs(self.flow_speed[dpid][
                            (stat.match.get('in_port'),
                            stat.match.get('ipv4_dst'),
                            stat.instructions[0].actions[0].port)][-1])))
            print '\n'

        if(self.stats['port']):
            bodys = self.stats['port']
            print('datapath  port   ''rx-pkts  rx-bytes '
                  'tx-pkts  tx-bytes rx-speed(Mbit/s)'
                  ' tx-speed(Mbit/s)  ')
            print('----------------   -------- ''-------- --------'
                  '-------- --------'
                  '----------------  ----------------   ')
            format = '%6s %6x %8d %8d %8d %8d    %8.2f    %8.2f'
            for dpid in bodys.keys():
                for stat in sorted(bodys[dpid], key=attrgetter('port_no')):
                    if stat.port_no != ofproto_v1_3.OFPP_LOCAL:
                        print(format % (
                            dpid, stat.port_no,
                            stat.rx_packets, stat.rx_bytes,
                            stat.tx_packets, stat.tx_bytes,
                            self.port_info[(dpid, stat.port_no)]['speedRx'],
                            self.port_info[(dpid, stat.port_no)]['speedTx'],))
            print '\n'

        # Show the bandwidth of links
        if(self.link_info_flag == True):
            print('src        dst      bandwidth(Mbit/s)    freebandwidth(Mbit/s')
            format = '%2s %10s %16.2f %16.2f'
            for link in self.awareness.link_to_port:
                (src_dpid, dst_dpid) = link
                bandwidth = self.link_info[link]['bandwidth']
                freebandwidth = self.link_info[link]['freebandwidth']
                print(format % (src_dpid, dst_dpid, bandwidth, freebandwidth))
        # for link in self.awareness.link_to_port:
        #     (src_dpid, dst_dpid) = link
        #     self.logger.info(src_dpid)
        #     self.logger.info(dst_dpid)
        #     if(self.link_info_flag == True):
        #         self.logger.info(self.link_info[link]['bandwidth'])
