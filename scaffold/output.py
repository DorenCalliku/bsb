from .helpers import ConfigurableClass, get_qualified_class_name
from .morphologies import Morphology
from contextlib import contextmanager
from abc import abstractmethod, ABC
import h5py, os, time, pickle, numpy as np
from numpy import string_

class ResourceHandler(ABC):
    def __init__(self):
        self.handle = None

    @contextmanager
    def load(self, mode=None):
        # id = np.random.randint(0, 100)
        already_open = True
        if self.handle is None: # Is the handle not open yet? Open it.
            # Pass the mode argument if it is given, otherwise allow child to rely
            # on its own default value for the mode argument.
            self.handle = self.get_handle(mode) if not mode is None else self.get_handle()
            already_open = False
        try:
            yield self.handle # Return the handle
        finally: # This is always called after the context manager closes.
            if not already_open: # Did we open the handle? We close it.
                self.release_handle(self.handle)
                self.handle = None

    @abstractmethod
    def get_handle(self, mode=None):
        '''
            Open the output resource and return a handle.
        '''
        pass

    @abstractmethod
    def release_handle(self, handle):
        '''
            Close the open output resource and release the handle.
        '''
        pass

class HDF5ResourceHandler(ResourceHandler):
    def get_handle(self, mode='a'):
        '''
            Open an HDF5 resource.
        '''
        # Open a new handle to the resource.
        return h5py.File(self.file, mode)

    def release_handle(self, handle):
        '''
            Close the MorphologyRepository storage resource.
        '''
        return handle.close()

class TreeHandler(ResourceHandler):
    '''
        Interface that allows a ResourceHandler to handle storage of TreeCollections.
    '''

    @abstractmethod
    def load_tree(collection_name, tree_name):
        pass

    @abstractmethod
    def store_tree_collections(self, tree_collections):
        pass

    @abstractmethod
    def list_trees(self, collection_name):
        pass

class HDF5TreeHandler(HDF5ResourceHandler, TreeHandler):
    '''
        TreeHandler that uses HDF5 as resource storage
    '''
    def store_tree_collections(self, tree_collections):
        with self.load() as f:
            if not 'trees' in f:
                tree_group = f.create_group('trees')
            else:
                tree_group = f['trees']
            for tree_collection in tree_collections:
                if not tree_collection.name in tree_group:
                    tree_collection_group = tree_group.create_group(tree_collection.name)
                else:
                    tree_collection_group = tree_group[tree_collection.name]
                for tree_name, tree in tree_collection.items():
                    if tree_name in tree_collection_group:
                        del tree_collection_group[tree_name]
                    tree_dataset = tree_collection_group.create_dataset(tree_name, data=string_(pickle.dumps(tree)))

    def load_tree(self, collection_name, tree_name):
        with self.load() as f:
            try:
                return pickle.loads(f['/trees/{}/{}'.format(collection_name, tree_name)][()])
            except KeyError as e:
                raise Exception("Tree not found in HDF5 file '{}', path does not exist: '{}'".format(f.file))

    def list_trees(self, collection_name):
        with self.load() as f:
            try:
                return list(f['trees'][collection_name].keys())
            except KeyError as e:
                return Exception("Tree collection '{}' not found".format(collection_name))

class OutputFormatter(ConfigurableClass, TreeHandler):

    def __init__(self):
        ConfigurableClass.__init__(self)
        TreeHandler.__init__(self)
        self.save_file_as = None

    @abstractmethod
    def create_output(self):
        pass

    @abstractmethod
    def init_scaffold(self):
        '''
            Initialize the scaffold when it has been loaded from an output file.
        '''
        pass

    @abstractmethod
    def get_simulator_output_path(self, simulator_name):
        '''
            Return the path where a simulator can dump preliminary output.
        '''
        pass

    @abstractmethod
    def has_cells_of_type(self, name):
        '''
            Check whether the position matrix for a certain cell type is present.
        '''
        pass

    @abstractmethod
    def get_cells_of_type(self, name):
        '''
            Return the position matrix for a specific cell type.
        '''
        pass

    @abstractmethod
    def exists(self):
        '''
            Check if the resource exists.
        '''
        pass

    @abstractmethod
    def get_connectivity_set_connection_types(self, tag):
        '''
            Return the connection types that contributed to this connectivity set.
        '''
        pass

    @abstractmethod
    def get_connectivity_set_meta(self, tag):
        '''
            Return the meta dictionary of this connectivity set.
        '''
        pass

class MorphologyRepository(HDF5TreeHandler):

    defaults = {
        'file': 'morphology_repository.hdf5'
    }

    protected_keys = ['voxel_clouds']

    def __init__(self, file=None):
        super().__init__()
        if not file is None:
            self.file = file

	# Abstract function from ResourceHandler
    def get_handle(self, mode='a'):
        '''
            Open the HDF5 storage resource and initialise the MorphologyRepository structure.
        '''
        # Open a new handle to the HDF5 resource.
        handle = HDF5TreeHandler.get_handle(self, mode)
        # Repository structure missing from resource? Create it.
        self.initialise_repo_structure(handle)
        # Return the handle to the resource.
        return handle

    def initialise_repo_structure(self, handle):
        if not 'morphologies' in handle:
            handle.create_group('morphologies')
        if not 'morphologies/voxel_clouds' in handle:
            handle.create_group('morphologies/voxel_clouds')

    def import_swc(self, file, name, tags=[], overwrite=False):
        '''
            Import and store .swc file contents as a morphology in the repository.
        '''
        # Read as CSV
        swc_data = np.loadtxt(file)
        # Create empty dataset
        dataset_length = len(swc_data)
        dataset_data = np.empty((dataset_length, 10))
        # Map parent id's to start coordinates. Root node (id: -1) is at 0., 0., 0.
        starts = {-1: [0., 0., 0.]}
        id_map = {-1: -1}
        next_id = 1
		# Get translation for a new space with compartment 0 as origin.
        translation = swc_data[0, 2:5]
        # Iterate over the compartments
        for i in range(dataset_length):
            # Extract compartment record
            compartment = swc_data[i, :]
            # Renumber the compartments to yield a continuous incrementing list of IDs
            # (increases performance of graph theory and network related tasks)
            compartment_old_id = compartment[0]
            compartment_id = next_id
            next_id += 1
            # Keep track of a map to translate old IDs to new IDs
            id_map[compartment_old_id] = compartment_id
            compartment_type = compartment[1]
            # Check if parent id is known
            if not compartment[6] in id_map:
                raise Exception("Node {} references a parent node {} that isn't known yet".format(compartment_old_id, compartment[6]))
            # Map the old parent ID to the new parent ID
            compartment_parent = id_map[compartment[6]]
            # Use parent endpoint as startpoint, get endpoint and store it as a startpoint for child compartments
            compartment_start = starts[compartment_parent]
			# Translate each compartment to a new space with compartment 0 as origin.
            compartment_end = compartment[2:5] - translation
            starts[compartment_id] = compartment_end
            # Get more compartment radius
            compartment_radius = compartment[5]
            # Store compartment in the repository dataset
            dataset_data[i] = [
                compartment_id,
                compartment_type,
                *compartment_start,
                *compartment_end,
                compartment_radius,
                compartment_parent
            ]
        # Save the dataset in the repository
        with self.load() as repo:
            if overwrite: # Do we overwrite previously existing dataset with same name?
                self.remove_morphology(name) # Delete anything that might be under this name.
            elif self.morphology_exists(name):
                raise Exception("A morphology called '{}' already exists in this repository.")
            # Create the dataset
            dset = repo['morphologies'].create_dataset(name, data=dataset_data)
            # Set attributes
            dset.attrs['name'] = name
            dset.attrs['search_radii'] = np.max(np.abs(dataset_data[:, 2:5]), axis=0)
            dset.attrs['type'] = 'swc'

    def import_repository(self, repository, overwrite=False):
        with repository.load() as external_handle:
            with self.load() as internal_handle:
                m_group = internal_handle['morphologies']
                for m_key in external_handle['morphologies'].keys():
                    if m_key not in self.protected_keys:
                        if overwrite or not m_key in m_group:
                            external_handle.copy('/morphologies/' + m_key, m_group)
                        else:
                            print("[WARNING] Did not import '{}' because it already existed and overwrite=False".format(m_key))

    def get_morphology(self, name, scaffold=None):
        '''
            Load a morphology from repository data
        '''
        # Open repository and close afterwards
        with self.load() as repo:
            # Check if morphology exists
            if not self.morphology_exists(name):
                raise Exception("Attempting to load unknown morphology '{}'".format(name))
            # Take out all the data with () index, and send along the metadata stored in the attributes
            data = self.raw_morphology(name)
            repo_data = data[()]
            repo_meta = dict(data.attrs)
            voxel_kwargs = {}
            if self.voxel_cloud_exists(name):
                voxels = self.raw_voxel_cloud(name)
                voxel_kwargs['voxel_data'] = voxels['positions'][()]
                voxel_kwargs['voxel_meta'] = dict(voxels.attrs)
                voxel_kwargs['voxel_map'] = pickle.loads(voxels['map'][()])
            return Morphology.from_repo_data(repo_data, repo_meta, scaffold=scaffold, **voxel_kwargs)

    def store_voxel_cloud(self, morphology, overwrite=False):
        with self.load('a') as repo:
            if self.voxel_cloud_exists(morphology.morphology_name):
                if not overwrite:
                    print("[WARNING] Did not overwrite existing voxel cloud for '{}'".format(morphology.morphology_name))
                    return
                else:
                    del repo['/morphologies/voxel_clouds/' + morphology.morphology_name]
            voxel_cloud_group = repo['/morphologies/voxel_clouds/'].create_group(morphology.morphology_name)
            voxel_cloud_group.attrs['name'] = morphology.morphology_name
            voxel_cloud_group.attrs['bounds'] = morphology.cloud.bounds
            voxel_cloud_group.attrs['grid_size'] = morphology.cloud.grid_size
            voxel_cloud_group.create_dataset('positions', data=morphology.cloud.voxels)
            voxel_cloud_group.create_dataset('map', data=string_(pickle.dumps(morphology.cloud.map)))

    def morphology_exists(self, name):
        with self.load() as repo:
            return name in self.handle['morphologies']

    def voxel_cloud_exists(self, name):
        with self.load() as repo:
            return name in self.handle['morphologies/voxel_clouds']

    def remove_morphology(self, name):
        with self.load() as repo:
            if self.morphology_exists(name):
                del self.handle['morphologies/' + name]

    def remove_voxel_cloud(self, name):
        with self.load() as repo:
            if self.voxel_cloud_exists(name):
                del self.handle['morphologies/voxel_clouds/' + name]

    def list_all_morphologies(self):
        with self.load() as repo:
            return list(filter(lambda x: x != 'voxel_clouds', repo['morphologies'].keys()))

    def list_all_voxelized(self):
        with self.load() as repo:
            all = list(repo['morphologies'].keys())
            voxelized = list(filter(lambda x: x in repo['/morphologies/voxel_clouds'], all))
            return voxelized

    def raw_morphology(self, name):
        '''
            Return the morphology dataset
        '''
        with self.load() as repo:
        	return repo['morphologies/' + name]

    def raw_voxel_cloud(self, name):
        '''
            Return the morphology dataset
        '''
        with self.load() as repo:
            return repo['morphologies/voxel_clouds/' + name]

class HDF5Formatter(OutputFormatter, MorphologyRepository):
    '''
    	Stores the output of the scaffold as a single HDF5 file. Is also a MorphologyRepository
    	and an HDF5TreeHandler.
    '''

    defaults = {
        'file': 'scaffold_network_{}.hdf5'.format(time.strftime("%Y_%m_%d-%H%M%S")),
        'simulator_output_path': False,
        'morphology_repository': None
    }

    def create_output(self):
        was_compiled = self.exists()
        if was_compiled:
            with h5py.File('__backup__.hdf5', 'w') as backup:
                with self.load() as repo:
                    repo.copy('/morphologies', backup)

        if self.save_file_as:
            self.file = self.save_file_as

        with self.load('w') as output:
            self.store_configuration()
            self.store_cells()
            self.store_tree_collections(self.scaffold.trees.__dict__.values())
            self.store_statistics()
            self.store_appendices()
            self.store_morphology_repository(was_compiled)

        if was_compiled:
            os.remove('__backup__.hdf5')

    def init_scaffold(self):
        with self.load() as resource:
            self.scaffold.configuration.cell_type_map = resource['cells'].attrs['types']
            self.scaffold.placement_stitching = resource['cells/stitching'][:]
            for cell_type_name, count in resource['statistics/cells_placed'].attrs.items():
                self.scaffold.statistics.cells_placed[cell_type_name] = count

    def validate(self):
        pass

    def store_configuration(self):
        f = self.handle
        f.attrs['shdf_version'] = 3.0
        f.attrs['configuration_version'] = 3.0
        f.attrs['configuration_name'] = self.scaffold.configuration._name
        f.attrs['configuration_type'] = self.scaffold.configuration._type
        f.attrs['configuration_class'] = get_qualified_class_name(self.scaffold.configuration)
        f.attrs['configuration_string'] = self.scaffold.configuration._raw

    def store_cells(self):
        cells_group = self.handle.create_group('cells')
        cells_group.create_dataset('stitching', data=self.scaffold.placement_stitching)
        self.store_cell_positions(cells_group)
        self.store_cell_connections(cells_group)

    def store_cell_positions(self, cells_group):
        position_dataset = cells_group.create_dataset('positions', data=self.scaffold.cells)
        cell_type_names = self.scaffold.configuration.cell_type_map
        cells_group.attrs['types'] = cell_type_names
        type_maps_group = cells_group.create_group('type_maps')
        for type in self.scaffold.configuration.cell_types.keys():
            type_maps_group.create_dataset(type + '_map', data=np.where(self.scaffold.cells[:,1] == cell_type_names.index(type))[0])

    def store_cell_connections(self, cells_group):
        connections_group = cells_group.create_group('connections')
        compartments_group = cells_group.create_group('connection_compartments')
        morphologies_group = cells_group.create_group('connection_morphologies')
        for tag, connectome_data in self.scaffold.cell_connections_by_tag.items():
            related_types = list(filter(lambda x: tag in x.tags, self.scaffold.configuration.connection_types.values()))
            connection_dataset = connections_group.create_dataset(tag, data=connectome_data)
            connection_dataset.attrs['tag'] = tag
            connection_dataset.attrs['connection_types'] = list(map(lambda x: x.name, related_types))
            connection_dataset.attrs['connection_type_classes'] = list(map(get_qualified_class_name, related_types))
            if tag in self.scaffold._connectivity_set_meta:
                meta_dict = self.scaffold._connectivity_set_meta[tag]
                for key in meta_dict:
                    connection_dataset.attrs[key] = meta_dict[key]
            if tag in self.scaffold.connection_compartments:
                compartments_group.create_dataset(tag, data=self.scaffold.connection_compartments[tag])

    def store_statistics(self):
        statistics = self.handle.create_group('statistics')
        self.store_placement_statistics(statistics)

    def store_placement_statistics(self, statistics_group):
        storage_group = statistics_group.create_group('cells_placed')
        for key, value in self.scaffold.statistics.cells_placed.items():
            storage_group.attrs[key] = value

    def store_appendices(self):
        # Append extra datasets specified internally or by user.
        for key, data in self.scaffold.appends.items():
            dset = self.handle.create_dataset(key, data=data)

    def store_morphology_repository(self, was_compiled=False):
        with self.load() as resource:
            if was_compiled: # File already existed?
                # Copy from the backup of previous version
                with h5py.File('__backup__.hdf5', 'r') as backup:
                    if 'morphologies' in resource:
                        del resource['/morphologies']
                    backup.copy('/morphologies', resource)
            else: # Fresh compilation
                self.initialise_repo_structure(resource)
                if not self.morphology_repository is None: # Repo specified
                    self.import_repository(self.scaffold.morphology_repository)

    def get_simulator_output_path(self, simulator_name):
        return self.simulator_output_path or os.getcwd()

    def has_cells_of_type(self, name):
        with self.load() as resource:
            return name in list(resource['/cells'].attrs['types'])

    def get_cells_of_type(self, name):
        # Check if cell type is present
        if not self.has_cells_of_type(name):
            raise Exception("Attempting to load cell type '{}' that isn't defined in the storage.".format(name))
        # Slice out the cells of this type based on the map in the position dataset attributes.
        with self.load() as resource:
            type_map = self.get_type_map(name)
            return resource['/cells/positions'][()][type_map]

    def get_type_map(self, type):
        with self.load() as resource:
            return self.handle['/cells/type_maps/{}_map'.format(type)][()]

    def exists(self):
        return os.path.exists(self.file)

    def get_connectivity_set_connection_types(self, tag):
        '''
            Return all the ConnectionStrategies that contributed to the creation of this
            connectivity set.
        '''
        with self.load() as f:
            # Get list of contributing types
            type_list = f['cells/connections/' + tag].attrs['connection_types']
            # Map contributing type names to contributing types
            return list(map(lambda name: self.scaffold.get_connection_type(name), type_list))

    def get_connectivity_set_meta(self, tag):
        '''
            Return the metadata associated with this connectivity set.
        '''
        with self.load() as f:
            return dict(f['cells/connections/' + tag].attrs)
