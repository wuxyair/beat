import logging
import os
import collections
import copy

from pyrocko import util, orthodrome
from pyrocko.cake import m2d

import numpy as num

from pyproj import Proj
import pickle


DataMap = collections.namedtuple('DataMap', 'list_ind, slc, shp, dtype')

kmtypes = set(['east_shift', 'north_shift', 'length', 'width', 'depth'])
km = 1000.


class ListArrayOrdering(object):
    """
    An ordering for a list to an array space. Takes also non theano.tensors.
    Modified from pymc3 blocking.

    Input:
    list_arrays - list of numpy arrays or list of theano.tensors
    intype - str, either 'tensor' or 'numpy'
    """
    def __init__(self, list_arrays, intype='numpy'):
        self.vmap = []
        dim = 0

        count = 0
        for array in list_arrays:
            if intype == 'tensor':
                array = array.tag.test_value
            elif intype == 'numpy':
                pass

            slc = slice(dim, dim + array.size)
            self.vmap.append(DataMap(
                count, slc, array.shape, array.dtype))
            dim += array.size
            count += 1

        self.dimensions = dim


class ListToArrayBijection(object):
    """
    A mapping between a List of arrays and an array space
    """
    def __init__(self, ordering, list_arrays):
        self.ordering = ordering
        self.list_arrays = list_arrays

    def fmap(self, list_arrays):
        """
        Maps values from List space to array space

        Parameters
        ----------
        list_arrays : list of numpy arrays
        """
        a_list = num.empty(self.ordering.dimensions)
        for list_ind, slc, _, _ in self.ordering.vmap:
            a_list[slc] = list_arrays[list_ind].ravel()
        return a_list

    def f3map(self, list_arrays):
        """
        Maps values from List space to array space with 3 columns

        Parameters
        ----------
        list_arrays : list of numpy arrays
        """
        a_list = num.empty((self.ordering.dimensions, 3))
        for list_ind, slc, _, _ in self.ordering.vmap:
            a_list[slc, :] = list_arrays[list_ind]
        return a_list

    def rmap(self, array):
        """
        Maps value from array space to List space

        Parameters
        ----------
        array - numpy-array non-symbolic
        """
        a_list = copy.copy(self.list_arrays)

        for list_ind, slc, shp, dtype in self.ordering.vmap:
            a_list[list_ind] = num.atleast_1d(
                                        array)[slc].reshape(shp).astype(dtype)

        return a_list

    def srmap(self, tarray):
        """
        Maps value from symbolic variable array space to List space

        Parameters
        ----------
        tarray - theano-array symbolic
        """
        a_list = copy.copy(self.list_arrays)

        for list_ind, slc, shp, dtype in self.ordering.vmap:
            a_list[list_ind] = tarray[slc].reshape(shp).astype(dtype.name)

        return a_list


def weed_input_rvs(input_rvs, dataset):
    '''
    Throw out random variables from input list that are not needed by the
    respective synthetics generating functions.
    mode = seis/geo
    '''
    name_order = [param.name for param in input_rvs]
    weeded_input_rvs = copy.copy(input_rvs)

    if dataset == 'geodetic':
        tobeweeded = ['time', 'duration']
    elif dataset == 'seismic':
        tobeweeded = ['opening']

    indexes = []
    for burian in tobeweeded:
        if burian in name_order:
            indexes.append(name_order.index(burian))

    indexes.sort(reverse=True)

    for ind in indexes:
        weeded_input_rvs.pop(ind)

    return weeded_input_rvs


def apply_station_blacklist(stations, blacklist):
    '''
    Throw out stations listed in the blacklist.
    '''

    station_names = [station.station for station in stations]

    indexes = []
    for burian in blacklist:
        indexes.append(station_names.index(burian))

    indexes.sort(reverse=True)

    for ind in indexes:
        stations.pop(ind)

    return stations


def downsample_traces(data_traces, deltat=None):
    '''
    Downsample data_traces to given sampling interval 'deltat'.
    '''
    for tr in data_traces:
        if deltat is not None:
            try:
                tr.downsample_to(deltat, snap=True, allow_upsample_max=5)
            except util.UnavailableDecimation, e:
                print('Cannot downsample %s.%s.%s.%s: %s' % (
                                                            tr.nslc_id + (e,)))
                continue


def weed_stations(stations, event, distances=(30., 90.)):
    '''
    Throw out stations, that are not within the given distances(min,max) to
    a reference event.
    '''
    weeded_stations = []
    for station in stations:
        distance = orthodrome.distance_accurate50m(event, station) * m2d

        if distance >= distances[0] and distance <= distances[1]:
            weeded_stations.append(station)

    return weeded_stations


def transform_sources(sources, datasets):
    '''
    Transforms a list of :py:class:`beat.RectangularSource` to dict of sources
    :py:class:`pscmp.RectangularSource` for geodetic data and
    :py:class:`gf.RectangularSource` for seismic data.
    Input: sources - list of BEAT sources
           datasets - config.problem.config.datasets
    '''
    d = dict()

    for dataset in datasets:
        sub_sources = []

        for source in sources:
            sub_sources.append(source.patches(1, 1, dataset))

        # concatenate list of lists to single list
        transformed_sources = []
        map(transformed_sources.extend, sub_sources)

        d[dataset] = transformed_sources

    return d


def adjust_point_units(point):
    '''
    Transform variables with [km] units to [m]
    Input: Point
    Returns: Point
    '''
    for key, value in point.iteritems():
        if key in kmtypes:
            point[key] = value * km

    return point


def split_point(point):
    '''
    Split point in solution space into List of dictionaries with source
    parameters for each source.
    :py:param: point :py:class:`pymc3.Point`
    '''
    n_sources = point[point.keys()[0]].shape[0]

    source_points = []
    for i in range(n_sources):
        source_param_dict = dict()
        for param, value in point.iteritems():
            source_param_dict[param] = float(value[i])

        source_points.append(source_param_dict)

    return source_points


def utm_to_loc(utmx, utmy, zone, event):
    '''
    Convert UTM[m] to local coordinates with reference to the :py:class:`Event`
    Input: Numpy arrays with UTM easting(utmx) and northing(utmy)
           zone - Integer number with utm zone
    Returns: Local coordinates [m] x, y
    '''
    p = Proj(proj='utm', zone=zone, ellps='WGS84')
    ref_x, ref_y = p(event.lon, event.lat)
    utmx -= ref_x
    utmy -= ref_y
    return utmx, utmy


def utm_to_lonlat(utmx, utmy, zone):
    '''
    Convert UTM[m] to Latitude and Longitude
    Input: Numpy arrays with UTM easting(utmx) and northing(utmy)
           zone - Integer number with utm zone
    Returns: Longitude, Latitude [deg]
    '''
    p = Proj(proj='utm', zone=zone, ellps='WGS84')
    lon, lat = p(utmx, utmy, inverse=True)
    return lon, lat


def setup_logging(project_dir):
    '''
    Setup function for handling logging. The logfiles are saved in the
    'project_dir'.
    '''

    logger = logging.getLogger('beat')

    ch = logging.StreamHandler()
    ch.setLevel(logging.INFO)

    fl = logging.FileHandler(
        filename=os.path.join(project_dir, 'log.txt'), mode='w')
    fl.setLevel(logging.INFO)

    formatter = logging.Formatter(
        '%(asctime)s - %(name)s - %(levelname)s %(message)s')

    fl.setFormatter(formatter)

    logger.addHandler(ch)
    logger.addHandler(fl)

    return logger


def load_atmip_params(project_dir, stage_number, mode):
    '''
    Load step and update objects for given stage.
    Input: project_dir - string to directory of project
           stage number - string of stage number or 'final' for last stage
           mode - problem that has been solved (geometry, static, kinematic)
    '''
    stage_path = os.path.join(project_dir, mode, 'stage_%s' % stage_number,
        'atmip.params')
    step, update = pickle.load(open(stage_path, 'rb'))
    return step, update

