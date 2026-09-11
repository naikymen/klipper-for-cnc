# Multiple named directional G38 probes
#
# This file may be distributed under the terms of the GNU GPLv3 license.
from .probe_G38 import ProbeG38


class ProbeG38Multi(ProbeG38):
    def __init__(self, config):
        self.probe_name = config.get_name().split(None, 1)[1]
        if not self.probe_name:
            raise config.error("probe_G38_multi requires a probe name")
        self.selected = False
        super().__init__(config, 'probe_' + self.probe_name)

    def register_commands(self):
        gcode = self.printer.lookup_object('gcode')
        for command in ("MULTIPROBE_TOWARD", "MULTIPROBE_TOWARD_NOERROR",
                        "MULTIPROBE_AWAY", "MULTIPROBE_AWAY_NOERROR"):
            gcode.register_mux_command(
                command, "PROBE_NAME", self.probe_name,
                getattr(self, "cmd_" + command),
                desc=getattr(self, "cmd_" + command + "_help"))
        gcode.register_mux_command(
            "QUERY_PROBE_MUX", "PROBE_NAME", self.probe_name,
            self.cmd_QUERY_PROBE, desc=self.cmd_QUERY_PROBE_help)
        gcode.register_mux_command(
            "SET_PROBE_G38", "PROBE_NAME", self.probe_name,
            self.cmd_SET_PROBE_G38, desc=self.cmd_SET_PROBE_G38_help)
        if "G38.2" not in gcode.ready_gcode_handlers:
            gcode.register_command("G38.2", self._cmd_G38_2,
                                   when_not_ready=False,
                                   desc=self.cmd_G38_2_help)
            gcode.register_command("G38.3", self._cmd_G38_3,
                                   when_not_ready=False,
                                   desc=self.cmd_G38_3_help)
            gcode.register_command("G38.4", self._cmd_G38_4,
                                   when_not_ready=False,
                                   desc=self.cmd_G38_4_help)
            gcode.register_command("G38.5", self._cmd_G38_5,
                                   when_not_ready=False,
                                   desc=self.cmd_G38_5_help)
            gcode.register_command("QUERY_PROBE", self._cmd_QUERY_PROBE,
                                   desc=self.cmd_QUERY_PROBE_help)

    def _all_probes(self):
        return [obj for name, obj in self.printer.lookup_objects(
            module="probe_G38_multi")]

    def _selected_probe(self):
        probes = self._all_probes()
        toolhead = self.printer.lookup_object("toolhead")
        extruder_name = toolhead.get_extruder().get_name()
        if extruder_name:
            for probe in probes:
                if probe.probe_name == extruder_name:
                    return probe
        for probe in probes:
            if probe.selected:
                return probe
        return probes[0]

    def cmd_SET_PROBE_G38(self, gcmd):
        for probe in self._all_probes():
            probe.selected = probe is self
        gcmd.respond_info("G38 probe: %s" % self.probe_name)

    def _cmd_G38_2(self, gcmd):
        self._selected_probe()._probe(gcmd, True, True)

    def _cmd_G38_3(self, gcmd):
        self._selected_probe()._probe(gcmd, False, True)

    def _cmd_G38_4(self, gcmd):
        self._selected_probe()._probe(gcmd, True, False)

    def _cmd_G38_5(self, gcmd):
        self._selected_probe()._probe(gcmd, False, False)

    def _cmd_QUERY_PROBE(self, gcmd):
        self._selected_probe().cmd_QUERY_PROBE(gcmd)

    def _run_multi(self, gcmd, error_out, trigger_invert):
        self._probe(gcmd, error_out, trigger_invert)

    cmd_MULTIPROBE_TOWARD_help = (
        "Probe toward workpiece with named probe; error on failure")
    def cmd_MULTIPROBE_TOWARD(self, gcmd):
        self._run_multi(gcmd, True, True)

    cmd_MULTIPROBE_TOWARD_NOERROR_help = (
        "Probe toward workpiece with named probe")
    def cmd_MULTIPROBE_TOWARD_NOERROR(self, gcmd):
        self._run_multi(gcmd, False, True)

    cmd_MULTIPROBE_AWAY_help = (
        "Probe away from workpiece with named probe; error on failure")
    def cmd_MULTIPROBE_AWAY(self, gcmd):
        self._run_multi(gcmd, True, False)

    cmd_MULTIPROBE_AWAY_NOERROR_help = (
        "Probe away from workpiece with named probe")
    def cmd_MULTIPROBE_AWAY_NOERROR(self, gcmd):
        self._run_multi(gcmd, False, False)

    cmd_SET_PROBE_G38_help = "Select the default named G38 probe"


def load_config_prefix(config):
    return ProbeG38Multi(config)
