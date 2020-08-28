from ...simulation import (
    SimulatorAdapter,
    SimulationComponent,
    SimulationCell,
    TargetsNeurons,
    TargetsSections,
)
from ...helpers import get_configurable_class
from ...reporting import report, warn
from ...models import ConnectivitySet
from ...exceptions import (
    MissingMorphologyError,
    IntersectionDataNotFoundError,
    ConfigurationError,
)
import random, os, sys
import numpy as np


class NeuronCell(SimulationCell):
    node_name = "simulations.?.cell_models"

    casts = {
        "record_soma": bool,
        "record_spikes": bool,
        "parameters": dict,
    }

    defaults = {
        "record_soma": False,
        "record_spikes": False,
        "parameters": {},
        "entity": False,
    }

    def boot(self):
        super().boot()
        self.instances = []
        if not self.relay:
            self.model_class = get_configurable_class(self.model)
        self.cell_type = self.scaffold.get_cell_type(self.name)

    def __getitem__(self, i):
        return self.instances[i]

    def validate(self):
        if not self.relay and not hasattr(self, "model"):
            raise ConfigurationError(
                "Missing required attribute 'model' in " + self.get_config_node()
            )
        if not self.relay:
            self.model_class = get_configurable_class(self.model)

    def get_parameters(self):
        # Get the default synapse parameters
        params = self.parameters.copy()
        return params


class NeuronConnection(SimulationComponent):
    node_name = "simulations.?.connection_models"

    required = ["synapse"]

    def validate(self):
        pass

    def resolve_synapses(self):
        return self.synapse if isinstance(self.synapse, list) else [self.synapse]


class NeuronDevice(TargetsNeurons, TargetsSections, SimulationComponent):
    node_name = "simulations.?.devices"

    device_types = [
        "spike_generator",
        "current_clamp",
        "spike_recorder",
        "voltage_recorder",
    ]

    casts = {
        "radius": float,
        "origin": [float],
    }

    defaults = {}

    required = ["targetting", "device", "io"]

    def validate(self):
        if self.device not in self.__class__.device_types:
            raise ConfigurationError(
                "Unknown device '{}' for {}".format(self.device, self.get_config_node())
            )
        if self.targetting == "cell_type" and not hasattr(self, "cell_types"):
            raise ConfigurationError(
                "Device '{}' targets cells using the 'cell_type' mechanism, but does not specify the required 'cell_types' attribute.".format(
                    self.name
                )
            )

    def create_patterns(self):
        raise NotImplementedError(
            "The "
            + self.__class__.__name__
            + " device does not implement any `create_patterns` function."
        )

    def get_pattern(self, target, cell=None, section=None, synapse=None):
        raise NotImplementedError(
            "The "
            + self.__class__.__name__
            + " device does not implement any `get_pattern` function."
        )

    def implement(self, target, cell, section):
        raise NotImplementedError(
            "The "
            + self.__class__.__name__
            + " device does not implement any `implement` function."
        )

    def validate_specifics(self):
        raise NotImplementedError(
            "The "
            + self.__class__.__name__
            + " device does not implement any `validate_specifics` function."
        )

    def get_locations(self, target):
        locations = []
        if target in self.adapter.relay_scheme:
            for cell_id, section_id in self.adapter.relay_scheme[target]:
                if cell_id not in self.adapter.node_cells:
                    continue
                cell = self.adapter.cells[cell_id]
                section = cell.sections[section_id]
                locations.append((cell, section))
        elif target in self.adapter.node_cells:
            cell = self.adapter.cells[target]
            sections = self.target_section(cell)
            locations.extend((cell, section) for section in sections)
        return locations


class NeuronEntity:
    @classmethod
    def instantiate(cls, **kwargs):
        instance = cls()
        instance.entity = True
        for k, v in kwargs.items():
            instance.__dict__[k] = v
        return instance

    def set_reference_id(self, id):
        self.ref_id = id

    def record_soma(self):
        raise NotImplementedError("Entities do not have a soma to record.")


class NeuronAdapter(SimulatorAdapter):
    """
        Interface between the scaffold model and the NEURON simulator.
    """

    simulator_name = "neuron"

    configuration_classes = {
        "cell_models": NeuronCell,
        "connection_models": NeuronConnection,
        "devices": NeuronDevice,
    }

    casts = {
        "temperature": float,
        "duration": float,
        "resolution": float,
        "initial": float,
    }

    defaults = {"initial": -65.0}

    required = ["temperature", "duration", "resolution"]

    def __init__(self):
        super().__init__()
        self.cells = {}
        self._next_gid = 0
        self.transmitter_map = {}

    def validate(self):
        pass

    def validate_prepare(self):
        output_handler = self.scaffold.output_formatter
        for connection_model in self.connection_models.values():
            # Get the connectivity set associated with this connection model
            connectivity_set = ConnectivitySet(output_handler, connection_model.name)
            from_type = connectivity_set.connection_types[0].from_cell_types[0]
            to_type = connectivity_set.connection_types[0].to_cell_types[0]
            from_cell_model = self.cell_models[from_type.name]
            to_cell_model = self.cell_models[to_type.name]
            if (
                from_type.entity
                or from_cell_model.relay
                or to_type.entity
                or to_cell_model.relay
            ):
                continue
            if not connectivity_set.compartment_set.exists():
                raise IntersectionDataNotFoundError(
                    "No intersection data found for '{}'".format(connection_model.name)
                )

    def prepare(self):
        from patch import p as simulator
        from time import time

        report("Preparing simulation", level=3)

        self.validate_prepare()
        self.h = simulator
        self.recorders = []

        simulator.dt = self.resolution
        simulator.celsius = self.temperature
        simulator.tstop = self.duration

        t = t0 = time()
        self.load_balance()
        report(
            "Load balancing on node",
            self.pc_id,
            "took",
            round(time() - t, 2),
            "seconds",
            all_nodes=True,
        )
        t = time()
        self.create_neurons()
        t = time() - t
        simulator.parallel.barrier()
        report(
            "Cell creation on node",
            self.pc_id,
            "took",
            round(t, 2),
            "seconds",
            all_nodes=True,
        )
        t = time()
        self.create_transmitters()
        report(
            "Transmitter creation on node",
            self.pc_id,
            "took",
            round(time() - t, 2),
            "seconds",
            all_nodes=True,
        )
        self.index_relays()
        simulator.parallel.barrier()
        t = time()
        self.create_receivers()
        t = time() - t
        report(
            "Receiver creation on node",
            self.pc_id,
            "took",
            round(t, 2),
            "seconds",
            all_nodes=True,
        )
        simulator.parallel.barrier()
        t = time()
        self.prepare_devices()
        t = time() - t
        report(
            "Device preparation on node",
            self.pc_id,
            "took",
            round(t, 2),
            "seconds",
            all_nodes=True,
        )
        simulator.parallel.barrier()
        t = time()
        self.create_devices()
        t = time() - t
        report(
            "Device creation on node",
            self.pc_id,
            "took",
            round(t, 2),
            "seconds",
            all_nodes=True,
        )
        report("Simulator preparation took", round(time() - t0, 2), "seconds")
        return simulator

    def load_balance(self):
        pc = self.h.parallel
        self.nhost = pc.nhost()
        self.pc_id = pc.id()
        self.cell_total = self.scaffold.get_cell_total()
        # Do a lazy round robin for now.
        self.node_cells = set(range(pc.id(), self.scaffold.get_cell_total(), pc.nhost()))

    def simulate(self, simulator):
        from plotly import graph_objects as go
        from plotly.subplots import make_subplots

        pc = simulator.parallel
        self.pc = pc
        pc.barrier()
        report("Simulating...", level=2)
        pc.set_maxstep(10)
        simulator.finitialize(-65.0)
        simulator.finitialize(self.initial)
        progression = 0
        while progression < self.duration:
            progression += 1
            pc.psolve(progression)
            pc.barrier()
            self.progress(progression, self.duration)
            if os.path.exists("interrupt_neuron"):
                report("Iterrupt requested. Stopping simulation.", level=1)
                break
        report("Finished simulation.", level=2)

    def collect_output(self):
        import h5py, time

        timestamp = str(time.time()).split(".")[0] + str(random.random()).split(".")[1]
        timestamp = self.pc.broadcast(timestamp)
        for node in range(self.scaffold.MPI.COMM_WORLD.size):
            self.pc.barrier()
            if node == self.pc_id:
                print("Node", self.pc_id, "is writing")
                with h5py.File(
                    "results_" + self.name + "_" + timestamp + ".hdf5", "a"
                ) as f:
                    for recorder in self.recorders:
                        y = list(recorder.recorder)
                        x = list(
                            recorder.time_recorder
                            or np.arange(0, len(y) * self.resolution, self.resolution)
                        )
                        # Because of NEURON's strange way of measuring time differently
                        # on each node, and just unreliably in general with respect to how
                        # simple it should be to divide 1.0 by 0.1 into 10 pieces but
                        # NEURON sometimes making that a funky 9 or spicy 11 pieces; we
                        # should check and trim the data arrays to be a uniform size.
                        if len(x) > len(y):
                            x = x[: len(y)]
                        if len(y) > len(x):
                            y = y[: len(x)]
                        data = np.column_stack((x, y))
                        path = "recorders/" + recorder.group + "/" + str(recorder.tag)
                        if path in f:
                            data = np.vstack((f[path][()], data))
                            del f[path]
                        d = f.create_dataset(path, data=data)
                        if hasattr(recorder, "meta"):
                            for k, v in recorder.meta.items():
                                d.attrs[k] = v
            self.pc.barrier()

    def create_transmitters(self):
        output_handler = self.scaffold.output_formatter
        for connection_model in self.connection_models.values():
            # Get the connectivity set associated with this connection model
            connectivity_set = ConnectivitySet(output_handler, connection_model.name)
            from_cell_model = self.cell_models[
                connectivity_set.connection_types[0].from_cell_types[0].name
            ]
            if from_cell_model.relay:
                print(
                    "Source is a relay; Skipping connection model {} transmitters".format(
                        connection_model.name
                    )
                )
                continue
            intersections = connectivity_set.intersections
            transmitters = [
                [i.from_id, i.from_compartment.section_id] for i in intersections
            ]
            unique_transmitters = [tuple(a) for a in np.unique(transmitters, axis=0)]
            # print("Unique transmitters:", unique_transmitters)
            transmitter_gids = list(
                range(self._next_gid, self._next_gid + len(unique_transmitters))
            )
            self._next_gid += len(unique_transmitters)
            partial_map = dict(zip(unique_transmitters, transmitter_gids))
            self.transmitter_map.update(partial_map)

            tcount = 0
            for (cell_id, section_id), gid in partial_map.items():
                cell_id = int(cell_id)
                if not cell_id in self.node_cells:
                    continue
                cell = self.cells[cell_id]
                cell.create_transmitter(cell.sections[int(section_id)], gid)
                tcount += 1
            print("Node", self.pc_id, "created", tcount, "transmitters")

    def create_receivers(self):
        output_handler = self.scaffold.output_formatter
        for connection_model in self.connection_models.values():
            # Get the connectivity set associated with this connection model
            connectivity_set = ConnectivitySet(output_handler, connection_model.name)
            from_cell_type = connectivity_set.connection_types[0].from_cell_types[0]
            if self.cell_models[from_cell_type.name].relay:
                continue
            from_cell_model = self.cell_models[from_cell_type.name]
            to_cell_type = connectivity_set.connection_types[0].to_cell_types[0]
            to_cell_model = self.cell_models[to_cell_type.name]
            if self.cell_models[to_cell_type.name].relay:
                raise NotImplementedError("Sorry, no relays yet, only for devices")
                # Fetch cell and section from `self.relay_scheme`
            else:
                synapse_types = connection_model.resolve_synapses()
                for intersection in connectivity_set.intersections:
                    if intersection.to_id in self.node_cells:
                        cell = self.cells[int(intersection.to_id)]
                        section_id = int(intersection.to_compartment.section_id)
                        section = cell.sections[section_id]
                        gid = self.transmitter_map[
                            tuple(
                                [
                                    intersection.from_id,
                                    intersection.from_compartment.section_id,
                                ]
                            )
                        ]
                        for synapse_type in synapse_types:
                            cell.create_receiver(section, gid, synapse_type)

    def create_neurons(self):
        for cell_model in self.cell_models.values():
            cell_positions = None
            if self.scaffold.configuration.get_cell_type(cell_model.name).entity:
                cell_data = self.scaffold.get_entities_by_type(cell_model.name)
                cell_data = np.column_stack((cell_data, np.zeros((len(cell_data), 4))))
            else:
                cell_data = self.scaffold.get_cells_by_type(cell_model.name)
            report("Placing " + str(len(cell_data)) + " " + cell_model.name)
            for cell in cell_data:
                cell_id = int(cell[0])
                if not cell_id in self.node_cells:
                    continue
                kwargs = cell_model.get_parameters()
                kwargs["position"] = cell[2:5]
                if cell_model.entity or cell_model.relay:
                    kwargs["relay"] = cell_model.relay
                    instance = NeuronEntity.instantiate(**kwargs)
                else:
                    instance = cell_model.model_class(**kwargs)
                instance.set_reference_id(cell_id)
                instance.cell_model = cell_model
                if cell_model.record_soma:
                    self.register_cell_recorder(instance, instance.record_soma())
                if cell_model.record_spikes:
                    spike_nc = self.h.NetCon(instance.soma[0], None)
                    spike_nc.threshold = -20
                    spike_recorder = spike_nc.record()
                    self.register_spike_recorder(instance, spike_recorder)
                cell_model.instances.append(instance)
                self.cells[cell_id] = instance
        print("Node", self.pc_id, "created", len(self.cells), "cells")

    def prepare_devices(self):
        device_module = __import__("devices", globals(), level=1)
        for device in self.devices.values():
            # CamelCase the snake_case to obtain the class name
            device_class = "".join(x.title() for x in device.device.split("_"))
            device.__class__ = device_module.__dict__[device_class]
            if self.pc_id == 0:
                # Have root 0 prepare the possibly random patterns.
                patterns = device.create_patterns()
            else:
                patterns = None
            # Broadcast to make sure all the nodes have the same patterns for each device.
            device.patterns = self.scaffold.MPI.COMM_WORLD.bcast(patterns, root=0)

    def create_devices(self):
        for device in self.devices.values():
            if self.pc_id == 0:
                # Have root 0 prepare the possibly random targets.
                targets = device.get_targets()
            else:
                targets = None
            # Broadcast to make sure all the nodes have the same targets for each device.
            targets = self.scaffold.MPI.COMM_WORLD.bcast(targets, root=0)
            for target in targets:
                for cell, section in device.get_locations(target):
                    device.implement(target, cell, section)

    def index_relays(self):
        report("Indexing relays.")
        terminal_relays = {}
        intermediate_relays = {}
        output_handler = self.scaffold.output_formatter
        for connection_model in self.connection_models.values():
            name = connection_model.name
            # Get the connectivity set associated with this connection model
            connectivity_set = ConnectivitySet(output_handler, connection_model.name)
            from_cell_type = connectivity_set.connection_types[0].from_cell_types[0]
            from_cell_model = self.cell_models[from_cell_type.name]
            to_cell_type = connectivity_set.connection_types[0].to_cell_types[0]
            to_cell_model = self.cell_models[to_cell_type.name]
            if not from_cell_model.relay:
                continue
            if to_cell_model.relay:
                report(
                    "Adding",
                    len(connectivity_set),
                    connection_model.name,
                    "connections as intermediate.",
                    level=3,
                )
                bin = intermediate_relays
                connections = connectivity_set.connections
                target = lambda c: c.to_id
            else:
                report(
                    "Adding",
                    len(connectivity_set),
                    connection_model.name,
                    "connections as terminal.",
                    level=3,
                )
                bin = terminal_relays
                connections = connectivity_set.intersections
                target = lambda c: (c.to_id, c.to_compartment.section_id)
            for connection in connections:
                fid = connection.from_id
                try:
                    arr = bin[fid]
                except:
                    arr = []
                    bin[fid] = arr
                arr.append(target(connection))

        report("Relays indexed, resolving intermediates.")

        while len(intermediate_relays) > 0:
            intermediates_to_remove = []
            for intermediate, targets in intermediate_relays.items():
                for target in targets:
                    if target in intermediate_relays:
                        # This target of this intermediary is also an intermediary and
                        # cannot be resolved to a terminal at this point, so we wait until
                        # a next iteration where the intermediary target might have been
                        # resolved.
                        continue
                    if target in terminal_relays:
                        # The target is a terminal relay and can be removed from our
                        # intermediary target list and its terminal targets added to our
                        # terminal target list.
                        try:
                            arr = terminal_relays[intermediate]
                        except:
                            arr = []
                            terminal_relays[intermediate] = arr
                        arr.extend(terminal_relays[target])
                        targets.remove(target)
                        # If we now have no more intermediary  targets we can be removed
                        # from the intermediary relay list.
                        if len(targets) == 0:
                            intermediates_to_remove.append(intermediate)
                    else:
                        # The target is not a relay at all and can be added to our
                        # terminal target list
                        try:
                            arr = terminal_relays[intermediate]
                        except:
                            arr = []
                            terminal_relays[intermediate] = arr
                        arr.append(target)
                        targets.remove(target)
                        if len(targets) == 0:
                            intermediates_to_remove.append(intermediate)
            for intermediate in intermediates_to_remove:
                report(
                    "Intermediate resolved to",
                    len(terminal_relays[intermediate]),
                    "targets",
                    level=4,
                )
                intermediate_relays.pop(intermediate, None)

        report("Relays resolved.")

        # Filter out all relays to targets not on this node.
        self.relay_scheme = {}
        for relay, targets in terminal_relays.items():
            my_targets = list(filter(lambda x: int(x[0]) in self.node_cells, targets))
            if my_targets:
                self.relay_scheme[relay] = my_targets
        report(
            "Node",
            self.pc_id,
            "needs to receive from",
            len(self.relay_scheme),
            "relays",
            level=4,
        )

    def register_recorder(
        self, group, cell, recorder, time_recorder=None, section=None, x=None, meta=None
    ):
        # Store the recorder so its output can be collected after the simulation.
        self.recorders.append(
            NeuronRecorder(group, cell, recorder, time_recorder, section, x, meta)
        )

    def register_cell_recorder(self, cell, recorder):
        self.recorders.append(NeuronRecorder("soma_voltages", cell, recorder))

    def register_spike_recorder(self, cell, recorder):
        self.recorders.append(
            NeuronRecorder(
                "soma_spikes",
                cell,
                SpikeConverter(cell, recorder),
                time_recorder=recorder,
            )
        )


class NeuronRecorder:
    def __init__(
        self, group, cell, recorder, time_recorder=None, section=None, x=None, meta=None
    ):
        if meta is None:
            meta = {}
        # Does this cell have plotting information?
        if hasattr(cell.cell_model.cell_type, "plotting"):
            # Pass it along to the recorder metadata
            meta["color"] = cell.cell_model.cell_type.plotting.color
            meta["display_label"] = cell.cell_model.cell_type.plotting.label
        self.group = group
        self.tag = str(cell.ref_id)
        if section is not None:
            self.tag += "." + section.name().split(".")[-1]
            if x is not None:
                self.tag += "({})".format(x)
        if meta is None:
            meta = {}
        meta["label"] = cell.cell_model.name
        self.meta = meta
        self.recorder = recorder
        self.time_recorder = time_recorder


class SpikeConverter:
    def __init__(self, cell, recorder):
        self.recorder = recorder
        self.signal = cell.ref_id

    def __iter__(self):
        return iter(self.signal for i in range(len(self.recorder)))