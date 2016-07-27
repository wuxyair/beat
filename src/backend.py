"""
Text file trace backend modified from pymc3 to work efficiently with
ATMCMC

Store sampling values as CSV files.

File format
-----------

Sampling values for each chain are saved in a separate file (under a
directory specified by the `name` argument).  The rows correspond to
sampling iterations.  The column names consist of variable names and
index labels.  For example, the heading

  x,y__0_0,y__0_1,y__1_0,y__1_1,y__2_0,y__2_1

represents two variables, x and y, where x is a scalar and y has a
shape of (3, 2).
"""
from glob import glob
import numpy as num
import os
import pandas as pd

import time

import pymc3
from pymc3.theanof import modelcontext
from pymc3.backends import base, ndarray
from pymc3.backends import tracetab as ttab
from pymc3.blocking import DictToArrayBijection, ArrayOrdering


class ArrayStepSharedLLK(pymc3.arraystep.BlockedStep):
    """
    Modified ArrayStepShared To handle returned larger point including the
    likelihood values.
    Takes additionally a list of output vars including the likelihoods.
    """
    def __init__(self, vars, out_vars, shared, blocked=True):
        """
        Parameters
        ----------
        vars : list of sampling variables
        shared : dict of theano variable -> shared variable
        blocked : Boolean (default True)
        """
        self.vars = vars
        self.ordering = ArrayOrdering(vars)
        self.shared = {var.name: shared for var, shared in shared.items()}
        self.blocked = blocked
        self.bij_in = None
        self.bij_out = None

    def step(self, point):
        for var, share in self.shared.items():
            share.container.storage[0] = point[var]

        if self.bij_in is None:
            self.bij_in = DictToArrayBijection(self.ordering, point)

        return self.astep(self.bij_in.map(point))


class BaseATMCMCTrace(object):
    """Base ATMCMC trace object

    Parameters
    ----------
    name : str
        Name of backend
    model : Model
        If None, the model is taken from the `with` context.
    vars : list of variables
        Sampling values will be stored for these variables. If None,
        `model.unobserved_RVs` is used.
    """
    def __init__(self, name, model=None, vars=None):
        self.name = name
        print name
        print 'Hier 1', time.time()
        model = modelcontext(model)
        print 'Hier 2', time.time()
        self.model = model
        if vars is None:
            vars = model.unobserved_RVs
            print 'Hier 3', vars, time.time()
        self.vars = vars
        self.varnames = [var.name for var in vars]

        ## Get variable shapes. Most backends will need this
        ## information.
        print model.test_point
        print 'Hier 6', time.time()
        self.var_shapes_list = [num.atleast_1d(var.tag.test_value.shape)
                        for var in vars]
        self.var_dtype_list = [var.tag.test_value.dtype for var in vars]

        self.var_shapes = {var: shape
            for var, shape in zip(self.varnames, self.var_shapes_list)}
        self.var_dtypes = {var: dtype
            for var, dtype in zip(self.varnames, self.var_dtypes_list)}
        print 'Hier 7', time.time()
        self.chain = None

    def __getitem__(self, idx):
        if isinstance(idx, slice):
            return self._slice(idx)

        try:
            return self.point(int(idx))
        except (ValueError, TypeError):  # Passed variable or variable name.
            raise ValueError('Can only index with slice or integer')


class Text(BaseATMCMCTrace):
    """Text trace object

    Parameters
    ----------
    name : str
        Name of directory to store text files
    model : Model
        If None, the model is taken from the `with` context.
    vars : list of variables
        Sampling values will be stored for these variables. If None,
        `model.unobserved_RVs` is used.
    """
    def __init__(self, name, model=None, vars=None):
        if not os.path.exists(name):
            os.mkdir(name)
        super(Text, self).__init__(name, model, vars)

        self.flat_names = {v: ttab.create_flat_names(v, shape)
                           for v, shape in self.var_shapes.items()}

        self.filename = None
        self._fh = None
        self.df = None

    ## Sampling methods

    def setup(self, draws, chain):
        """Perform chain-specific setup.

        Parameters
        ----------
        draws : int
            Expected number of draws
        chain : int
            Chain number
        """
        self.chain = chain
        self.filename = os.path.join(self.name, 'chain-{}.csv'.format(chain))

        cnames = [fv for v in self.varnames for fv in self.flat_names[v]]

        if os.path.exists(self.filename):
            with open(self.filename) as fh:
                prev_cnames = next(fh).strip().split(',')
            if prev_cnames != cnames:
                raise base.BackendError(
                    "Previous file '{}' has different variables names "
                    "than current model.".format(self.filename))
            self._fh = open(self.filename, 'a')
        else:
            self._fh = open(self.filename, 'w')
            self._fh.write(','.join(cnames) + '\n')

    def record(self, point):
        """Record results of a sampling iteration.

        Parameters
        ----------
        point : List
            Values mapped to variable names
        """
        import time
        print time.time()
        columns = [str(value.ravel()) for varname, value in zip(
                                                self.varnames, point)]

        self._fh.write(','.join(columns) + '\n')
        print time.time()

    def close(self):
        self._fh.close()
        self._fh = None  # Avoid serialization issue.

    ## Selection methods

    def _load_df(self):
        if self.df is None:
            self.df = pd.read_csv(self.filename)

    def __len__(self):
        if self.filename is None:
            return 0
        self._load_df()
        return self.df.shape[0]

    def get_values(self, varname, burn=0, thin=1):
        """Get values from trace.

        Parameters
        ----------
        varname : str
        burn : int
        thin : int

        Returns
        -------
        A NumPy array
        """
        self._load_df()
        var_df = self.df[self.flat_names[varname]]
        shape = (self.df.shape[0],) + self.var_shapes[varname]
        vals = var_df.values.ravel().reshape(shape)
        return vals[burn::thin]

    def _slice(self, idx):
        if idx.stop is not None:
            raise ValueError('Stop value in slice not supported.')
        return ndarray._slice_as_ndarray(self, idx)

    def point(self, idx):
        """Return dictionary of point values at `idx` for current chain
        with variables names as keys.
        """
        idx = int(idx)
        self._load_df()
        pt = {}
        for varname in self.varnames:
            vals = self.df[self.flat_names[varname]].iloc[idx]
            pt[varname] = vals.reshape(self.var_shapes[varname])
        return pt


def load(name, model=None):
    """Load Text database.

    Parameters
    ----------
    name : str
        Name of directory with files (one per chain)
    model : Model
        If None, the model is taken from the `with` context.

    Returns
    -------
    A MultiTrace instance
    """
    files = glob(os.path.join(name, 'chain-*.csv'))

    straces = []
    for f in files:
        chain = int(os.path.splitext(f)[0].rsplit('-', 1)[1])
        strace = Text(name, model=model)
        strace.chain = chain
        strace.filename = f
        straces.append(strace)
    return base.MultiTrace(straces)


def dump(name, trace, chains=None):
    """Store values from NDArray trace as CSV files.

    Parameters
    ----------
    name : str
        Name of directory to store CSV files in
    trace : MultiTrace of NDArray traces
        Result of MCMC run with default NDArray backend
    chains : list
        Chains to dump. If None, all chains are dumped.
    """
    if not os.path.exists(name):
        os.mkdir(name)
    if chains is None:
        chains = trace.chains

    var_shapes = trace._straces[chains[0]].var_shapes
    flat_names = {v: ttab.create_flat_names(v, shape)
                  for v, shape in var_shapes.items()}

    for chain in chains:
        filename = os.path.join(name, 'chain-{}.csv'.format(chain))
        df = ttab.trace_to_dataframe(trace, chains=chain, flat_names=flat_names)
        df.to_csv(filename, index=False)