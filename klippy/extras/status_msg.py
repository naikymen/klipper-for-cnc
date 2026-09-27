# Console dump of the toolhead status
#
# The old `develop` branch had GET_STATUS_MSG as a helper method of the
# toolhead class.  The command only reads `toolhead.get_status()`, so it lives
# in an extra here and the core stays untouched.
#
# This file may be distributed under the terms of the GNU GPLv3 license.
import pprint


class StatusMsg:
    def __init__(self, config):
        self.printer = config.get_printer()
        gcode = self.printer.lookup_object('gcode')
        gcode.register_command('GET_STATUS_MSG', self.cmd_GET_STATUS_MSG,
                               desc=self.cmd_GET_STATUS_MSG_help)

    cmd_GET_STATUS_MSG_help = ("Pretty-print toolhead's get_status to the"
                               " console")
    def cmd_GET_STATUS_MSG(self, gcmd):
        curtime = self.printer.get_reactor().monotonic()
        toolhead = self.printer.lookup_object('toolhead')
        gcmd.respond_info(pprint.pformat(toolhead.get_status(curtime)))


def load_config(config):
    return StatusMsg(config)
