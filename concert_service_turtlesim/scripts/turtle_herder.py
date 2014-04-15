#!/usr/bin/env python
#
# License: BSD
#   https://raw.github.com/robotics-in-concert/concert_services/license/LICENSE
#
##############################################################################
# About
##############################################################################

# Simple script to manage spawning and killing of turtles across multimaster
# boundaries. Typically turtlesim clients would connect to the kill and
# spawn services directly to instantiate themselves, but since we can't
# flip service proxies, this is not possible. So this node is the inbetween
# go-to node and uses a rocon service pair instead.
#
# It supplements this relay role with a bit of herd management - sets up
# random start locations and feeds back aliased names when running with
# a concert.

##############################################################################
# Imports
##############################################################################

import math
import random
import copy
import threading

import rospy
import rocon_python_comms
import rocon_gateway_utils
import turtlesim.srv as turtlesim_srvs
import concert_service_msgs.msg as concert_service_msgs
import gateway_msgs.msg as gateway_msgs
import gateway_msgs.srv as gateway_srvs

##############################################################################
# Classes
##############################################################################


class TurtleHerder:
    '''
      Shepherds the turtles!

      @todo get alised names from the concert client list if the topic is available

      @todo watchdog for killing turtles that are no longer connected.
    '''
    __slots__ = [
        'turtles',  # Dictionary of string : concert_msgs.RemoconApp[]
        '_spawn_turtle_service_client',
        '_kill_turtle_service_client',
        '_kill_turtle_service_pair_server',
        '_spawn_turtle_service_pair_server',
        '_gateway_flip_service',
        '_spawn_lock',  # need locks to ensure that the worker threads don't try and send multiple ros service requests (e.g. gateway flip requests)
        '_kill_lock'    # at th same time
    ]

    def __init__(self):
        self.turtles = []
        self._spawn_lock = threading.Lock()
        self._kill_lock = threading.Lock()
        # herding backend
        rospy.wait_for_service('~internal/kill')  # could use timeouts here
        rospy.wait_for_service('~internal/spawn')
        self._spawn_turtle_service_client = rospy.ServiceProxy('~internal/spawn', turtlesim_srvs.Spawn, persistent=True)
        self._kill_turtle_service_client = rospy.ServiceProxy('~internal/kill', turtlesim_srvs.Kill, persistent=True)
        # kill the default turtle that turtlesim starts with
        try:
            unused_response = self._kill_turtle_service_client("turtle1")
        except rospy.ServiceException:
            rospy.logerr("Turtle Herder : failed to contact the internal kill turtle service")
        except rospy.ROSInterruptException:
            rospy.loginfo("Turtle Herder : shutdown while contacting the internal kill turtle service")
            return
        # herding frontend
        # gateway
        gateway_namespace = rocon_gateway_utils.resolve_local_gateway()
        rospy.wait_for_service(gateway_namespace + '/flip')
        self._gateway_flip_service = rospy.ServiceProxy(gateway_namespace + '/flip', gateway_srvs.Remote)
        # we relay services inside the service pair, so make sure we use threads so it doesn't hold up requests from multiple sources
        self._kill_turtle_service_pair_server = rocon_python_comms.ServicePairServer('kill', self._kill_turtle_service, concert_service_msgs.KillTurtlePair, use_threads=True)
        self._spawn_turtle_service_pair_server = rocon_python_comms.ServicePairServer('spawn', self._spawn_turtle_service, concert_service_msgs.SpawnTurtlePair, use_threads=True)

    def _kill_turtle_service(self, request_id, msg):
        '''
          @param request_id
          @type uuid_msgs/UniqueID
          @param msg
          @type ServiceRequest
        '''
        self._kill_lock.acquire()
        response = concert_service_msgs.KillTurtleResponse()
        internal_service_request = turtlesim_srvs.KillRequest(msg.name)
        try:
            unused_internal_service_response = self._kill_turtle_service_client(internal_service_request)
            self.turtles.remove(msg.name)
        except rospy.ServiceException:  # communication failed
            rospy.logerr("Turtle Herder : failed to contact the internal kill turtle service")
        except rospy.ROSInterruptException:
            rospy.loginfo("Turtle Herder : shutdown while contacting the internal kill turtle service")
            self._kill_lock.release()
            return
        self._kill_turtle_service_pair_server.reply(request_id, response)
        self._send_flip_rules_request(name=msg.name, cancel=True)
        self._kill_lock.release()

    def _spawn_turtle_service(self, request_id, msg):
        '''
          @param request_id
          @type uuid_msgs/UniqueID
          @param msg
          @type ServiceRequest
        '''
        self._spawn_lock.acquire()
        # Unique name
        name = msg.name
        name_extension = ''
        count = 0
        while name + name_extension in self.turtles:
            name_extension = '_' + str(count)
            count = count + 1
        name = name + name_extension

        internal_service_request = turtlesim_srvs.SpawnRequest(
                                            random.uniform(3.5, 6.5),
                                            random.uniform(3.5, 6.5),
                                            random.uniform(0.0, 2.0 * math.pi),
                                            name)
        try:
            unused_internal_service_response = self._spawn_turtle_service_client(internal_service_request)
            self.turtles.append(name)
        except rospy.ServiceException as e:  # communication failed
            rospy.logerr("TurtleHerder : failed to contact the internal spawn turtle service [%s]" % e)
            name = ''
        except rospy.ROSInterruptException:
            rospy.loginfo("TurtleHerder : shutdown while contacting the internal spawn turtle service")
            self._spawn_lock.release()
            return
        response = concert_service_msgs.SpawnTurtleResponse()
        response.name = name
        self._spawn_turtle_service_pair_server.reply(request_id, response)
        self._send_flip_rules_request(name=name, cancel=False)
        self._spawn_lock.release()

    def _send_flip_rules_request(self, name, cancel):
        rules = []
        rule = gateway_msgs.Rule()
        rule.node = ''
        rule.type = gateway_msgs.ConnectionType.SUBSCRIBER
        # could resolve this better by looking up the service info
        rule.name = "/services/turtlesim/%s/cmd_vel" % name
        rules.append(copy.deepcopy(rule))
        rule.type = gateway_msgs.ConnectionType.PUBLISHER
        rule.name = "/services/turtlesim/%s/pose" % name
        rules.append(copy.deepcopy(rule))
        # send the request
        request = gateway_srvs.RemoteRequest()
        request.cancel = cancel
        remote_rule = gateway_msgs.RemoteRule()
        remote_rule.gateway = name
        for rule in rules:
            remote_rule.rule = rule
            request.remotes.append(copy.deepcopy(remote_rule))
        try:
            response = self._gateway_flip_service(request)
        except rospy.ServiceException as e:  # communication failed
            rospy.logerr("TurtleHerder : failed to send flip rules [%s]" % e)
            return
        except rospy.ROSInterruptException:
            rospy.loginfo("TurtleHerder : shutdown while contacting the gateway flip service")
            return

    def shutdown(self):
        for name in self.turtles:
            try:
                unused_internal_service_response = self._kill_turtle_service_client(name)
            except rospy.ServiceException:  # communication failed
                break  # quietly fail
            except rospy.ROSInterruptException:
                break  # quietly fail

##############################################################################
# Launch point
##############################################################################

if __name__ == '__main__':

    rospy.init_node('turtle_herder')

    turtle_herder = TurtleHerder()
#     spawn_turtle = rocon_python_comms.ServicePairClient('spawn', concert_service_msgs.SpawnTurtlePair)
#     rospy.rostime.wallsleep(0.5)
#     response = spawn_turtle(concert_service_msgs.SpawnTurtleRequest('kobuki'), timeout=rospy.Duration(3.0))
#     print("Response: %s" % response)
#     response = spawn_turtle(concert_service_msgs.SpawnTurtleRequest('guimul'), timeout=rospy.Duration(3.0))
#     print("Response: %s" % response)
    rospy.spin()
    turtle_herder.shutdown()
