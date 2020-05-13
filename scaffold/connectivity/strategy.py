from ..helpers import ConfigurableClass, SortableByAfter
from ..functions import compute_intersection_slice
from ..models import ConnectivitySet
import abc


class _SimulationPlaceholder:
    pass


class ConnectionStrategy(ConfigurableClass, SortableByAfter):
    def __init__(self):
        super().__init__()
        self.simulation = _SimulationPlaceholder()
        self.tags = []
        self.labels = None

    @abc.abstractmethod
    def connect(self):
        pass

    def _wrap_connect(this):
        # This function is called after the ConnectionStrategy instance if constructed,
        # and replaces its user-defined `connect` function with a wrapped version of
        # itself. The wrapper provides the ConnectionStrategy with the cell type ids it
        # needs and might execute it multiple times. The set(s) of provided cell ids are
        # defined by the `from_cell_types` and `to_cell_types` arrays in the JSON config
        # of the ConnectionStrategy. If it contains a `with_label` the cell types will be
        # filtered to a subset of cells that are labelled with the label. If the
        # with_label ends on a wilcard (e.g. "label-big-*") multiple labels might apply
        # and the ConnectionStrategy is repeated for each label.
        import types

        # Store a local reference to the original connect function
        connect = this.connect
        # Wrapper closure that calls the local `connect`, referencing the original connect
        def wrapped_connect(self):
            # Handle with_label specifications.
            # This is a dirty solution that only implements the wanted microzone behavior
            # See https://github.com/Helveg/cerebellum-scaffold/issues/236

            # Introduced a local variable "mixed" that can be updated considering an attribute inside connection_types in the json file
            # if it is necessary to mix the 2 labeled populations when connecting them (like microzone positive and negative)
            if hasattr(self, "mix_labels"):
                mixed = self.mix_labels
            else:
                mixed = False

            if len(self._from_cell_types) == 0:
                # No specific type specification? No labelling either -> do connect.
                self._set_cells()
                connect()
            elif "with_label" not in {
                **self._from_cell_types[0],
                **self._to_cell_types[0],
            }:
                # No labels specified -> select all cells and do connect.
                self._set_cells()
                connect()
            else:
                # Label specified
                if "with_label" in self._from_cell_types[0].keys():
                    label_specification_pre = self._from_cell_types[0]["with_label"]
                    labels_pre = self.scaffold.get_labels(label_specification_pre)
                else:
                    labels_pre = []

                if "with_label" in self._to_cell_types[0].keys():
                    label_specification_post = self._to_cell_types[0]["with_label"]
                    labels_post = self.scaffold.get_labels(label_specification_post)
                else:
                    labels_post = []

                n_pre = len(labels_pre)
                n_post = len(labels_post)
                label_matrix = []

                if n_pre * n_post != 0:
                    for i in range(n_pre):
                        if mixed:
                            for j in range(n_post):
                                label_matrix.append([labels_pre[i], labels_post[j]])
                        else:
                            label_matrix.append([labels_pre[i], labels_post[i]])
                elif n_pre > 0:
                    for i in range(n_pre):
                        label_matrix.append([labels_pre[i], labels_post])
                else:
                    for j in range(n_post):
                        label_matrix.append([labels_pre, labels_post[j]])

                for label_pre, label_post in label_matrix:
                    self.labels = [label_pre, label_post]
                    self._set_cells(label_pre, label_post)
                    connect()
                    self.labels = None

        # Replace the connect function of this instance with a wrapped version.
        this.connect = types.MethodType(wrapped_connect, this)

    def _set_cells(self, label_pre=[], label_post=[]):
        self.from_cells = {}
        self.to_cells = {}
        types = ["from_cell", "to_cell"]
        # Do it for the from cells and to cells
        for t in types:
            # Iterate over the from or to cell types.
            for cell_type in self.__dict__[t + "_types"]:
                # Get the cell matrix and ids for the type.
                cells = cell_type.get_cells()
                ids = cell_type.get_ids().tolist()
                ids.sort()
                if label_pre and t == "from_cell":
                    labelled = self.scaffold.get_labelled_ids(label_pre).tolist()
                    labelled.sort()
                    # Compute intersect of sorted list
                    label_slice = compute_intersection_slice(ids, labelled)
                    # Store the labelled cells of the type.
                    self.__dict__[t + "s"][cell_type.name] = cells[label_slice]
                elif label_post and t == "to_cell":
                    labelled = self.scaffold.get_labelled_ids(label_post).tolist()
                    labelled.sort()
                    # Compute intersect of sorted list
                    label_slice = compute_intersection_slice(ids, labelled)
                    # Store the labelled cells of the type.
                    self.__dict__[t + "s"][cell_type.name] = cells[label_slice]
                else:
                    # Store all cells of the type.
                    self.__dict__[t + "s"][cell_type.name] = cells

    @classmethod
    def get_ordered(cls, objects):
        return objects.values()  # No sorting of connection types required.

    def get_after(self):
        return None if not self.has_after() else self.after

    def has_after(self):
        return hasattr(self, "after")

    def create_after(self):
        self.after = []

    def get_connection_matrices(self):
        return [self.scaffold.cell_connections_by_tag[tag] for tag in self.tags]

    def get_connectivity_sets(self):
        return [ConnectivitySet(self.scaffold.output_formatter, tag) for tag in self.tags]


class TouchingConvergenceDivergence(ConnectionStrategy):
    casts = {"divergence": int, "convergence": int}

    required = ["divergence", "convergence"]

    def validate(self):
        pass

    def connect(self):
        pass


class TouchConnect(ConnectionStrategy):
    def validate(self):
        pass

    def connect(self):
        pass
