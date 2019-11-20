"""
 ****************************************************************************
 Filename:          node_hw.py
 Description:       Fetches Server FRUs and Logical Sensor data using inband IPMI interface to BMC
 Creation Date:     11/04/2019
 Author:            Madhura Mande

 Do NOT modify or remove this copyright and confidentiality notice!
 Copyright (c) 2001 - $Date: 2019/04/11 $ Seagate Technology, LLC.
 The code contained herein is CONFIDENTIAL to Seagate Technology, LLC.
 Portions are also trade secret. Any use, duplication, derivation, distribution
 or disclosure of this code, for any reason, not expressly authorized is
 prohibited. All other rights are expressly reserved by Seagate Technology, LLC.
 ****************************************************************************
"""

import os
import subprocess
import time
import json
import re
import tempfile
import socket
import uuid

from zope.interface import implements

from framework.utils.severity_reader import SeverityReader
from message_handlers.node_data_msg_handler import NodeDataMsgHandler
from message_handlers.logging_msg_handler import LoggingMsgHandler

from framework.base.debug import Debug
from framework.base.module_thread import ScheduledModuleThread
from framework.base.internal_msgQ import InternalMsgQ
from framework.utils.config_reader import ConfigReader
from framework.utils.service_logging import logger
from sensors.INode_hw import INodeHWsensor

IPMITOOL = "sudo ipmitool "

# bash exit codes
BASH_ILLEGAL_CMD = 127
DETECT_VM = "sudo /usr/bin/systemd-detect-virt"

class NodeHWsensor(ScheduledModuleThread, InternalMsgQ):
    """Obtains data about the FRUs and logical sensors and updates
       if any state change occurs"""

    implements(INodeHWsensor)

    SENSOR_NAME = "NodeHWsensor"
    PRIORITY = 1

    CONF_FILE = "/etc/sspl.conf"
    SYSINFO = "SYSTEM_INFORMATION"
    DATA_PATH_KEY = "data_path"
    DATA_PATH_VALUE_DEFAULT = "/var/sspl/data"

    sel_event_info = ""

    TYPE_PSU_SUPPLY = 'Power Supply'
    TYPE_PSU_UNIT = 'Power Unit'
    TYPE_FAN = 'Fan'
    TYPE_DISK = 'Drive Slot / Bay'

    SEL_USAGE_THRESHOLD = 90
    SEL_INFO_PERC_USED = "Percent Used"
    SEL_INFO_FREESPACE = "Free Space"
    SEL_INFO_ENTRIES = "Entries"

    CACHE_DIR_NAME  = "server"
    # This file stores the last index from the SEL list for which we have issued an event.
    INDEX_FILE = "last_sel_index"
    LIST_FILE  = "sel_list"

    UPDATE_CREATE_MODE = "w+"
    UPDATE_ONLY_MODE   = "r+"

    IPMI_ERRSTR = "Could not open device at "

    SYSTEM_INFORMATION = "SYSTEM_INFORMATION"
    SITE_ID = "site_id"
    RACK_ID = "rack_id"
    NODE_ID = "node_id"
    CLUSTER_ID = "cluster_id"

    request_shutdown = False
    sel_last_queried = None
    SEL_QUERY_FREQ = 300

    DYNAMIC_KEYS = {
            "Sensor Reading",
            "States Asserted",
            }

    # Dependency list
    DEPENDENCIES = {
                    "plugins": ["NodeDataMsgHandler", "LoggingMsgHandler"],
                    "rpms": []
    }

    @staticmethod
    def name():
        """@return: name of the module."""
        return NodeHWsensor.SENSOR_NAME

    def __init__(self):
        super(NodeHWsensor, self).__init__(self.SENSOR_NAME.upper(), self.PRIORITY)
        self.host_id = self._get_host_id()

        self.fru_types = {
            self.TYPE_FAN: self._parse_fan_info,
            self.TYPE_PSU_SUPPLY: self._parse_psu_supply_info,
            self.TYPE_PSU_UNIT: self._parse_psu_unit_info,
            self.TYPE_DISK: self._parse_disk_info,
        }

        # Validate configuration file for required valid values
        try:
            self.conf_reader = ConfigReader(self.CONF_FILE)

        except (IOError, ConfigReader.Error) as err:
            logger.error("[ Error ] when validating the config file {0} - {1}"\
                 .format(self.CONF_FILE, err))

    def _get_file(self, name):
        if os.path.exists(name):
            mode = self.UPDATE_ONLY_MODE
        else:
            mode = self.UPDATE_CREATE_MODE
        return open(name, mode)

    def _initialize_cache(self):
        data_dir =  self.conf_reader._get_value_with_default(
            self.SYSINFO, self.DATA_PATH_KEY, self.DATA_PATH_VALUE_DEFAULT)
        self.cache_dir_path = os.path.join(data_dir, self.CACHE_DIR_NAME)

        if not os.path.exists(self.cache_dir_path):
            logger.info("Creating cache dir: {0}".format(self.cache_dir_path))
            os.makedirs(self.cache_dir_path)
        logger.info("Using cache dir: {0}".format(self.cache_dir_path))

        index_file_name = os.path.join(self.cache_dir_path, self.INDEX_FILE)
        self.index_file = self._get_file(index_file_name)

        if os.path.getsize(self.index_file.name) == 0:
            self._write_index_file(0)
        # Now self.index_file has a valid sel index in it

        list_file_name = os.path.join(self.cache_dir_path, self.LIST_FILE)
        self.list_file = self._get_file(list_file_name)

    def _write_index_file(self, index):
        if not isinstance(index, int):
            index = int(index, base=16)
        literal = "{0:x}\n".format(index)

        self.index_file.seek(0)
        self.index_file.truncate()
        self.index_file.write(literal)
        self.index_file.flush()

    def _read_index_file(self):
        self.index_file.seek(0)
        index_line = self.index_file.readline().strip()
        return int(index_line, base=16)

    def initialize(self, conf_reader, msgQlist, products):
        """initialize configuration reader and internal msg queues"""

        # Initialize ScheduledMonitorThread and InternalMsgQ
        super(NodeHWsensor, self).initialize(conf_reader)

        # Initialize internal message queues for this module
        super(NodeHWsensor, self).initialize_msgQ(msgQlist)

        self._site_id = int(conf_reader._get_value_with_default(
                                                self.SYSTEM_INFORMATION,
                                                self.SITE_ID,
                                                0))
        self._rack_id = int(conf_reader._get_value_with_default(
                                                self.SYSTEM_INFORMATION,
                                                self.RACK_ID,
                                                0))
        self._node_id = int(conf_reader._get_value_with_default(
                                                self.SYSTEM_INFORMATION,
                                                self.NODE_ID,
                                                0))

        self._cluster_id = int(conf_reader._get_value_with_default(
                                                self.SYSTEM_INFORMATION,
                                                self.CLUSTER_ID,
                                                0))

        # Set flag 'request_shutdown' if vm detected
        res, retcode = self._run_command(DETECT_VM)
        if retcode == 0:
            logger.info("{0}: VM env detected, ipmitool can't fetch monitoring"
                " data".format(self.SENSOR_NAME))
            self.request_shutdown = True
        else:
            # Set flag 'request_shutdown' if ipmitool cmd trial errors out
            res, retcode = self._run_ipmitool_subcommand("")

            self._initialize_cache()
            self.sensor_id_map = dict()

            if self.request_shutdown == False:
                for fru in self.fru_types:
                    self.sensor_id_map[fru] = { sensor_num: sensor_id
                        for (sensor_id, sensor_num) in
                        self._get_sensor_list_by_type(fru)}

    def _update_list_file(self):
        (tmp_fd, tmp_name) = tempfile.mkstemp(dir=self.cache_dir_path)
        sel_out, retcode = self._run_ipmitool_subcommand("sel list", out_file=tmp_fd)
        if retcode != 0:
            msg = "ipmitool sel list command failed: {0}".format(''.join(sel_out))
            logger.error(msg)
            raise Exception(msg)

        # os.rename() is an atomic operation on POSIX, which means that even
        # if we crash the SEL list in self.list_file will always be
        # in a consistent state.
        list_file_name = self.list_file.name
        os.rename(tmp_name, list_file_name)
        self.list_file.close()
        self.list_file = open(list_file_name, self.UPDATE_ONLY_MODE)

    def _check_and_clear_sel(self):
        """ Clear SEL Table if SEL used memory seen above threshold
            SEL_USAGE_THRESHOLD """

        info_dict = {}

        if self.sel_last_queried:
            last_checked = time.time() - self.sel_last_queried

            if last_checked < self.SEL_QUERY_FREQ:
                return

        try:
            sel_info, retcode = self._run_ipmitool_subcommand("sel info")
            if retcode != 0:
                logger.error("ipmitool sel info command failed, "
                    "with err {0}".format(retcode))
                return (False)

            # record SEL last queried time
            self.sel_last_queried = time.time()

            key = val = None
            info_list = ''.join(sel_info).split("\n")

            for info in info_list:
                if ':' in info:
                    key, val = [f.strip() for f in info.split(":", 1)]
                    info_dict[key] = val

            if self.SEL_INFO_PERC_USED in info_dict:
                '''strip '%' or any unwanted char from value'''
                info_dict[self.SEL_INFO_PERC_USED] = re.sub('%', '',
                                          info_dict[self.SEL_INFO_PERC_USED])

                if info_dict[self.SEL_INFO_PERC_USED].isdigit():
                   used = int(info_dict[self.SEL_INFO_PERC_USED])
                else:
                    entries = int(info_dict[self.SEL_INFO_ENTRIES])
                    free = int(re.sub('[A-Za-z]+','',\
                               info_dict[self.SEL_INFO_FREESPACE]))

                    entries = entries * 16
                    free = free + entries

                    used = (100 * entries) / free
                    logger.debug("SEL % Used: calculated {0}%".format(used))

            if used > self.SEL_USAGE_THRESHOLD:
                logger.warning("SEL usage above threshold {0}%, "
                    "clearing SEL".format(self.SEL_USAGE_THRESHOLD))

                cleared, retcode = self._run_ipmitool_subcommand("sel clear")
                if retcode != 0:
                    logger.critical("{0}: Error in clearing SEL, overflow"
                        " may result in loss of node alerts from SEL in future"\
                        .format(self.host_id))
                    return

                #reset last processed SEL index in cached index file
                self._write_index_file(0)

        except Exception as ae:
            logger.exception(ae)

    def run(self):
        """Run the sensor on its own thread"""

        # Check for debug mode being activated
        self._read_my_msgQ_noWait()

        if self.request_shutdown == False:
            try:
                # Check for a change in ipmi sel list and notify the node data
                # msg handler
                if os.path.getsize(self.list_file.name) != 0:
                    # If the SEL list file is not empty, that means that some
                    # of the processing from the last iteration is incomplete.
                    # Complete that before getting the new SEL events.
                    self._notify_NodeDataMsgHandler()

                self._update_list_file()
                self._notify_NodeDataMsgHandler()

                self._check_and_clear_sel()
            except Exception as ae:
                logger.exception(ae)

            # Reset debug mode if persistence is not enabled
            self._disable_debug_if_persist_false()
            self._scheduler.enter(30, self._priority, self.run, ())
        else:
            logger.warning("{0} Node hw monitoring disabled"\
                .format(self.SENSOR_NAME))
            self.shutdown()

    def _get_sel_event(self):
        last_index = self._read_index_file()

        # TODO: See if the following 2 loops can be reduced to
        # a single loop using mmap() and a fixed file format like
        # the one produced by the 'ipmitool sel file' or
        # 'ipmitool sel writeraw' commands.
        found = False
        self.list_file.seek(0, os.SEEK_SET)
        for line in self.list_file:
            if not found:
                if line.split("|")[0].strip() == "{0:x}".format(last_index):
                    found = True
                continue

            yield self._make_sel_event(line)

        if not found:
            # This can mean one of a few things:
            # 1. The SEL has been cleared beyond the last index we saw
            # 2. It has rotated to beyond the last index we saw
            # 3. self.list_file is empty
            self.list_file.seek(0, os.SEEK_SET)
            for line in self.list_file:
                yield self._make_sel_event(line)

    def _make_sel_event(self, sel_line):
            # Separate out the components of the sel event
            # Sample sel event which gets parsed
            # 2 | 04/16/2019 | 05:29:09 | Fan #0x30 | Lower Non-critical going low  | Asserted
            index, date, time, device_id, event, status = [
                attr.strip() for attr in sel_line.split("|") ]
            device_type, sensor_num = re.match(
                    '(.*) (#0x([0-9a-f]+))?', device_id).group(1, 3)
            return (index, date, time, device_id, device_type, sensor_num, event, status)

    def _notify_NodeDataMsgHandler(self):
        """See if there is any new event gets generated in the sel and notify
            node data message handler for generating JSON message"""

        last_fru_index = {}
        last_index = None
        for (index, date, time, device_id, device_type, sensor_num, event, status) \
                in self._get_sel_event():
            last_fru_index[device_type] = index
            last_index = index

        for (index, date, time, device_id, device_type, sensor_num, event, status) \
                in self._get_sel_event():

            is_last = (last_fru_index[device_type] == index)
            logger.debug("_notify_NodeDataMsgHandler fan: is_last: {}, sel_event: {}".format(
                is_last, (index, date, time, device_id, event, status)))
            # TODO: Also use information from the command
            # 'ipmitool sel get <sel-entry-id>'
            # which gives more detailed information
            if device_type in self.fru_types:
                self.fru_types[device_type](index, date, time, device_id,
                        sensor_num, event, status, is_last)

        if last_index is not None:
            self._write_index_file(last_index)
        self.list_file.seek(0)
        self.list_file.truncate()

    def _run_command(self, command, out_file=subprocess.PIPE):
        """executes commands"""

        process = subprocess.Popen(command, shell=True, stdout=out_file, stderr=subprocess.PIPE)
        result = process.communicate()
        return result, process.returncode

    def _run_ipmitool_subcommand(self, subcommand, grep_args=None, out_file=subprocess.PIPE):
        """executes ipmitool sub-commands, and optionally greps the output"""

        command = IPMITOOL + subcommand
        if grep_args is not None:
            command += " | grep " + grep_args
        res, retcode = self._run_command(command, out_file)

        # Detect if ipmitool removed or facing error after sensor initialized
        if retcode != 0:
            if retcode == 1:
                if isinstance(res, tuple):
                    resstr = ''.join(res)

                    if resstr.find(self.IPMI_ERRSTR) == 0:
                        logger.error("{0}: ipmitool error:: {1}\n"
                            "Dependencies failed, shutting down sensor".\
                            format(self.SENSOR_NAME, resstr))
                        self.request_shutdown = True
            elif (retcode == BASH_ILLEGAL_CMD):
                logger.error("{0}: Required ipmitool missing on Node."
                    " Dependencies failed, shutting down sensor"\
                    .format(self.SENSOR_NAME))
                self.request_shutdown = True

        return res, retcode

    def read_data(self):
        return self.sel_event_info

    def _get_sensor_list_by_type(self, sensor_type):
        """get list of sensors of type 'sensor_type'
           Returns a list of tuples, of which
           the first element is the sensor id and
           the second is the number."""

        sensor_list_out, retcode = self._run_ipmitool_subcommand("sdr type '{0}'".format(sensor_type))
        if retcode != 0:
            msg = "ipmitool sdr type command failed: {0}".format(''.join(sensor_list_out))
            logger.error(msg)
            return
        sensor_list = ''.join(sensor_list_out).split("\n")

        out = []
        for sensor in sensor_list:
            if sensor == "":
                break
            # Example of output form 'sdr type' command:
            # Sys Fan 2B       | 33h | ok  | 29.4 | 5332 RPM
            # PS1 1a Fan Fail  | A0h | ok  | 29.13 |
            # HDD 1 Status     | F1h | ok  |  4.2 | Drive Present
            fields_list = [ f.strip() for f in sensor.split("|")]
            sensor_id, sensor_num, status, entity_id, reading  = fields_list
            sensor_num = sensor_num.strip("h").lower()

            out.append((sensor_id, sensor_num))
        return out

    def _get_sensor_list_by_entity(self, entity_id):
        """get list of sensors belonging to entity 'entity_id'
           Returns a list of sensor IDs"""

        sensor_list_out, retcode = self._run_ipmitool_subcommand("sdr entity '{0}'".format(entity_id))
        if retcode != 0:
            msg = "ipmitool sdr entity command failed: {0}".format(''.join(sensor_list_out))
            logger.error(msg)
            return
        sensor_list = ''.join(sensor_list_out).split("\n")

        out = []
        for sensor in sensor_list:
            if sensor == '':
                continue
            # Output from 'sdr entity' command is same as from 'sdr type' command.
            fields_list = [f.strip() for f in sensor.split("|")]
            sensor_id, sensor_num, status, entity_id, reading = fields_list
            sensor_num = sensor_num.strip("h").lower()

            out.append(sensor_id)
        return out

    def _get_sensor_sdr_props(self, sensor_id):
        props_list_out, retcode = self._run_ipmitool_subcommand("sdr get '{0}'".format(sensor_id))
        if retcode != 0:
            msg = "ipmitool sensor get command failed: {0}".format(''.join(sel_out))
            logger.error(msg)
            return
        props_list = ''.join(props_list_out).split("\n")

        static_keys = {}
        curr_key = None
        for prop in props_list:
            if prop == '':
                continue
            if ':' in prop and '[' not in prop and ']' not in prop:
                curr_key, val = [f.strip() for f in prop.split(":")]
                static_keys[curr_key] = val
            else:
                static_keys[curr_key] += "\n" + prop

        dynamic = {}
        common_props = {
            'Sensor ID',
            'Entity ID',
        }
        # Whatever keys from DYNAMIC_KEYS are present,
        # move them to the 'dynamic' dict
        for c in (static_keys.viewkeys() & self.DYNAMIC_KEYS):
            dynamic[c] = static_keys[c]
            del static_keys[c]

        return (dynamic, static_keys)

    def _get_sensor_props(self, sensor_id):
        """get all the properties of a sensor.
           Returns a tuple (common, specific) where
           common is a dict of common sensor properties and
           their values for this sensor, and
           specific is a dict of the properties specific to this sensor"""
        props_list_out, retcode = self._run_ipmitool_subcommand("sensor get '{0}'".format(sensor_id))
        if retcode != 0:
            msg = "ipmitool sensor get command failed: {0}".format(''.join(props_list_out))
            logger.error(msg)
            return (False, False)
        props_list = ''.join(props_list_out).split("\n")
        props_list = props_list[1:] # The first line is 'Locating sensor record...'

        specific_static = {}
        curr_key = None
        for prop in props_list:
            if prop == '':
                continue
            if ':' in prop:
                curr_key, val = [f.strip() for f in prop.split(":")]
                specific_static[curr_key] = val
            else:
                specific_static[curr_key] += "\n" + prop

        common = {}
        common_props = {
            'Sensor ID',
            'Entity ID',
        }
        # Whatever keys from common_props are present,
        # move them to the 'common' dict
        for c in (specific_static.viewkeys() & common_props):
            common[c] = specific_static[c]
            del specific_static[c]

        specific_dynamic = {}
        for c in (specific_static.viewkeys() & self.DYNAMIC_KEYS):
            specific_dynamic[c] = specific_static[c]
            del specific_static[c]

        return (common, specific_static, specific_dynamic)

    def _parse_fan_info(self, index, date, time, device_id, sensor_id, event, status, is_last):
        """Parse out Fan realted changes that gets reaflected in the ipmi sel list"""

        #TODO: Can enrich the sspl event message with more FRU info using
        # command 'ipmitool sel get <sel-id>'

        fan_info = {}

        #TODO: Enabled Assertions list (fan_specific_list[] in code) needs
        # to be built dynamically to support platform specific assertions and
        # not limit to these hardcoded ones.
        fan_specific_list = ["Sensor Reading", "Lower Non-Recoverable",
                             "Lower Non-Recoverable","Upper Non-Recoverable",
                             "Lower Critical", "Lower Non-Critical",
                             "Upper Critical"]

        threshold_event = event.split(" ")
        threshold = threshold_event[len(threshold_event)-1]

        sensor_name = self.sensor_id_map[self.TYPE_FAN][sensor_id]

        fan_common_data, fan_specific_data, fan_specific_data_dynamic = self._get_sensor_props(sensor_name)

        for key in fan_specific_data.keys():
            if key not in fan_specific_list:
                del fan_specific_data[key]

        fan_info = fan_specific_data
        fan_info.update({"fru_id" : device_id, "event" : event})
        alert_type = "threshold_breached:" + threshold
        resource_type = NodeDataMsgHandler.IPMI_RESOURCE_TYPE_FAN
        severity_reader = SeverityReader()
        severity = severity_reader.map_severity(alert_type)
        fru_info = {    "site_id": self._site_id,
                        "rack_id": self._rack_id,
                        "node_id": self._node_id,
                        "cluster_id":self._cluster_id ,
                        "resource_type": resource_type,
                        "resource_id": sensor_name,
                        "event_time":self._get_epoch_time_from_date_and_time(date, time)
                    }

        if is_last:
            fan_info.update(fan_specific_data_dynamic)

        self._send_json_msg(resource_type, alert_type, severity, fru_info, fan_info)
        self._log_IEM(resource_type, alert_type, severity, fru_info, fan_info)

    def _parse_psu_supply_info(self, index, date, time, sensor, sensor_num, event, status, is_last):
        """Parse out PSU related changes that gets reflected in the ipmi sel list"""

        alerts = {
            ("Config Error", "Asserted"): ("fault","error"),
            ("Config Error", "Deasserted"): ("fault_resolved","informational"),
            ("Failure detected ()", "Asserted"): ("fault","error"),
            ("Failure detected ()", "Deasserted"): ("fault_resolved","informational"),
            ("Failure detected", "Asserted"): ("fault","error"),
            ("Failure detected", "Deasserted"): ("fault_resolved","informational"),
            ("Power Supply AC lost", "Asserted"): ("fault","critical"),
            ("Power Supply AC lost", "Deasserted"): ("fault_resolved","informational"),
            ("Power Supply Inactive", "Asserted"): ("fault","critical"),
            ("Power Supply Inactive", "Deasserted"): ("fault_resolved","informational"),
            ("Predictive failure", "Asserted"): ("fault","warning"),
            ("Predictive failure", "Deasserted"): ("fault_resolved","informational"),
            ("Presence detected", "Asserted"): ("insertion","informational"),
            ("Presence detected", "Deasserted"): ("missing","critical"),
        }

        sensor_id = self.sensor_id_map[self.TYPE_PSU_SUPPLY][sensor_num]
        resource_type = NodeDataMsgHandler.IPMI_RESOURCE_TYPE_PSU
        info = {
            "site_id": self._site_id,
            "rack_id": self._rack_id,
            "node_id": self._node_id,
            "cluster_id": self._cluster_id,
            "resource_type": resource_type,
            "resource_id": sensor,
            "event_time": self._get_epoch_time_from_date_and_time(date, time)
        }

        try:
            (alert_type, severity) = alerts[(event, status)]
        except KeyError:
            logger.error("Unknown event: {}, status: {}".format(event, status))
            return

        dynamic, static = self._get_sensor_sdr_props(sensor_id)
        specific_info = {}
        specific_info["fru_id"] = sensor
        specific_info["event"] = event
        specific_info.update(static)

        # Remove unnecessary characters props
        for key in ['Deassertions Enabled', 'Assertions Enabled',
                    'States Asserted', 'Assertion Events']:
            try:
                specific_info[key] = re.sub(',  +', ', ', re.sub('[\[\]]','', \
                    specific_info[key]).replace('\n',','))
            except KeyError:
                pass

        if is_last:
            specific_info.update(dynamic)

        self._send_json_msg(resource_type, alert_type, severity, info, specific_info)
        self._log_IEM(resource_type, alert_type, severity, info, specific_info)

    def _parse_psu_unit_info(self, index, date, time, sensor, sensor_num, event, status, is_last):
        """Parse out PSU related changes that gets reflected in the ipmi sel list"""

        alerts = {

            ("240VA power down", "Asserted"): ("fault", "critical"),
            ("240VV power down", "Deasserted"): ("fault_resolved", "informational"),
            ("AC lost", "Asserted"): ("fault", "critical"),
            ("AC lost", "Deasserted"): ("fault_resolved", "informational"),
            ("Failure detected", "Asserted"): ("fault", "critical"),
            ("Failure detected", "Deasserted"): ("fault_resolved", "informational"),
            ("Power off/down", "Asserted"): ("fault", "critical"),
            ("Power off/down", "Deasserted"): ("fault_resolved", "informational"),
            ("Soft-power control failure", "Asserted"): ("fault", "warning"),
            ("Soft-power control failure", "Deasserted"): ("fault_resolved", "informational"),



            ("Fully Redundant", "Asserted"): ("fault_resolved", "informational"),
            ("Fully Redundant", "Deasserted"): ("fault", "warning"),
            ("Non-Redundant: Insufficient Resources", "Asserted"): ("fault", "critical"),
            ("Non-Redundant: Insufficient Resources", "Deasserted"): ("fault_resolved", "informational"),
            ("Non-Redundant: Sufficient from Insufficient", "Asserted"): ("fault", "warning"),
            ("Non-Redundant: Sufficient from Insufficient", "Deasserted"): ("fault", "warning"),
            ("Non-Redundant: Sufficient from Redundant", "Asserted"): ("fault", "warning"),
            ("Non-Redundant: Sufficient from Redundant", "Deasserted"): ("fault", "informational"),
            ("Redundancy Degraded", "Asserted"): ("fault", "warning"),
            ("Redundancy Degraded", "Deasserted"): ("fault_resolved", "informational"),
            ("Redundancy Degraded from Fully Redundant", "Asserted"): ("fault", "warning"),
            ("Redundancy Degraded from Fully Redundant", "Deasserted"): ("fault_resolved", "warning"),
            ("Redundancy Degraded from Non-Redundant", "Asserted"): ("fault", "critical"),
            ("Redundancy Degraded from Non-Redundant", "Deasserted"): ("fault_resolved", "warning"),
            ("Redundancy Lost", "Asserted"): ("fault", "warning"),
            ("Redundancy Lost", "Deasserted"): ("fault_resolved", "informational"),

        }

        sensor_id = self.sensor_id_map[self.TYPE_PSU_UNIT][sensor_num]
        resource_type = NodeDataMsgHandler.IPMI_RESOURCE_TYPE_PSU

        info = {
            "site_id": self._site_id,
            "rack_id": self._rack_id,
            "node_id": self._node_id,
            "cluster_id": self._cluster_id,
            "resource_type": resource_type,
            "resource_id": sensor,
            "event_time": self._get_epoch_time_from_date_and_time(date, time)
        }

        try:
            (alert_type, severity) = alerts[(event, status)]
        except KeyError:
            logger.error("Unknown event: {}, status: {}".format(event, status))
            return

        dynamic, static = self._get_sensor_sdr_props(sensor_id)
        specific_info = {}
        specific_info["fru_id"] = sensor
        specific_info["event"] = event
        specific_info.update(static)

        for key in ['Deassertions Enabled', 'Assertions Enabled',
                    'Assertion Events', 'States Asserted']:
            try:
                specific_info[key] = re.sub(',  +', ', ', re.sub('[\[\]]','', \
                    specific_info[key]).replace('\n',','))
            except KeyError:
                pass

        if is_last:
            specific_info.update(dynamic)

        self._send_json_msg(resource_type, alert_type, severity, info, specific_info)
        self._log_IEM(resource_type, alert_type, severity, info, specific_info)

    def _parse_disk_info(self, index, date, time, sensor, sensor_num, event, status, is_last):
        """Parse out Disk related changes that gets reaflected in the ipmi sel list"""

        sensor_id = self.sensor_id_map[self.TYPE_DISK][sensor_num]

        common, specific, specific_dynamic = self._get_sensor_props(sensor_id)
        if common:
            disk_sensors_list = self._get_sensor_list_by_entity(common['Entity ID'])
            disk_sensors_list.remove(sensor_id)

            if not specific:
                specific = {"States Asserted": "N/A", "Sensor Type (Discrete)": "N/A"}
            specific_info = specific
            alert_severity_dict = {
                ("Drive Present", "Asserted"): ("insertion", "informational"),
                ("Drive Present", "Deasserted"): ("missing", "critical"),
                }

            resource_type = NodeDataMsgHandler.IPMI_RESOURCE_TYPE_DISK
            info = {
                "site_id": self._site_id,
                "rack_id": self._rack_id,
                "node_id": self._node_id,
                "cluster_id": self._cluster_id,
                "resource_type": resource_type,
                "resource_id": sensor,
                "event_time": self._get_epoch_time_from_date_and_time(date, time)
            }
            if (event, status) in alert_severity_dict:
                alert_type = alert_severity_dict[(event, status)][0]
                severity   = alert_severity_dict[(event, status)][1]
            else:
                alert_type = "fault"
                severity   = "informational"
            specific_info["fru_id"] = sensor
            specific_info["event"] = "{0} - {1}".format(event, status)

            if is_last:
                specific_info.update(specific_dynamic)

            for key in ['Deassertions Enabled', 'Assertions Enabled',
                        'Assertion Events', 'States Asserted']:
                try:
                    specific_info[key] = re.sub(',  +', ', ', re.sub('[\[\]]','', \
                        specific_info[key]).replace('\n',','))
                except KeyError:
                    pass

            self._send_json_msg(resource_type, alert_type, severity, info, specific_info)
            self._log_IEM(resource_type, alert_type, severity, info, specific_info)

    def _send_json_msg(self, resource_type, alert_type, severity, info, specific_info):
        """Transmit data to NodeDataMsgHandler which takes two arguments.
           device will be device name and data will consist of relevant data"""

        internal_json_msg = json.dumps({
            "sensor_request_type" : {
                "node_data":{
                    "alert_type": alert_type,
                    "severity": severity,
                    "alert_id": self._get_alert_id(info["event_time"]),
                    "host_id": self.host_id,
                    "info": info,
                    "specific_info": specific_info
                }
            }
          })

        # Send the event to node data message handler to generate json message and send out
        self._write_internal_msgQ(NodeDataMsgHandler.name(), internal_json_msg)

    def _log_IEM(self, resource_type, alert_type, severity, info, specific_info):
        """Sends an IEM to logging msg handler"""

        json_data = json.dumps({
            "sensor_request_type" : {
                "node_data":{
                    "alert_type": alert_type,
                    "severity": severity,
                    "alert_id": self._get_alert_id(info["event_time"]),
                    "host_id": self.host_id,
                    "info": info,
                    "specific_info": specific_info
                }
               }
            }, sort_keys=True)

        # Send the event to node data message handler to generate json message and send out
        internal_json_msg = json.dumps(
                {'actuator_request_type': {'logging': {'log_level': 'LOG_WARNING', 'log_type': 'IEM', 'log_msg': '{}'.format(json_data)}}})

        # Send the event to logging msg handler to send IEM message to journald
        self._write_internal_msgQ(LoggingMsgHandler.name(), internal_json_msg)

    def _get_host_id(self):
        return socket.getfqdn()

    def _get_alert_id(self, epoch_time):
        """Returns alert id which is a combination of
           epoch_time and salt value
        """
        salt = str(uuid.uuid4().hex)
        alert_id = epoch_time + salt
        return alert_id

    def _get_epoch_time_from_date_and_time(self, _date, _time):
        timestamp_format = '%m/%d/%Y %H:%M:%S'
        timestamp = time.strptime('{} {}'.format(_date,_time), timestamp_format)
        return str(int(time.mktime(timestamp)))

    def shutdown(self):
        """Clean up scheduler queue and gracefully shutdown thread"""
        super(NodeHWsensor, self).shutdown()