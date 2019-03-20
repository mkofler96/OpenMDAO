"""Finite difference derivative approximations."""
from __future__ import division, print_function

from collections import namedtuple, defaultdict
from itertools import groupby
from six import iteritems
from six.moves import range, zip

import numpy as np

from openmdao.approximation_schemes.approximation_scheme import ApproximationScheme, \
    _gather_jac_results
from openmdao.utils.name_maps import abs_key2rel_key
from openmdao.utils.array_utils import sub2full_indices, get_local_offset_map

FDForm = namedtuple('FDForm', ['deltas', 'coeffs', 'current_coeff'])

DEFAULT_ORDER = {
    'forward': 1,
    'backward': 1,
    'central': 2,
}

FD_COEFFS = {
    ('forward', 1): FDForm(deltas=np.array([1.0]),
                           coeffs=np.array([1.0]),
                           current_coeff=-1.0),
    ('backward', 1): FDForm(deltas=np.array([-1.0]),
                            coeffs=np.array([-1.0]),
                            current_coeff=1.0),
    ('central', 2): FDForm(deltas=np.array([1.0, -1.0]),
                           coeffs=np.array([0.5, -0.5]),
                           current_coeff=0.),
}

_full_slice = slice(None)


def _generate_fd_coeff(form, order):
    """
    Create an FDForm namedtuple containing the deltas, coefficients, and current coefficient.

    Parameters
    ----------
    form : str
        Requested form of FD (e.g. 'forward', 'central', 'backward').
    order : int
        The order of accuracy of the requested FD scheme.

    Returns
    -------
    FDForm
        namedtuple containing the 'deltas', 'coeffs', and 'current_coeff'. These deltas and
        coefficients need to be scaled by the step size.
    """
    try:
        fd_form = FD_COEFFS[form, order]
    except KeyError:
        # TODO: Automatically generate requested form and store in dict.
        msg = 'Finite Difference form="{}" and order={} are not supported'
        raise ValueError(msg.format(form, order))
    return fd_form


class FiniteDifference(ApproximationScheme):
    r"""
    Approximation scheme using finite differences to estimate derivatives.

    For example, using the 'forward' form with a step size of 'h' will approximate the derivative in
    the following way:

    .. math::

        f'(x) = \frac{f(x+h) - f(x)}{h} + O(h).

    Attributes
    ----------
    _exec_list : list
        A list of which derivatives (in execution order) to compute.
        The entries are of the form (key, fd_options), where key is (of, wrt) where of and wrt are
        absolute names and fd_options is a dictionary.
    _starting_outs : ndarray
        A copy of the starting outputs array used to restore the outputs to original values.
    _starting_ins : ndarray
        A copy of the starting inputs array used to restore the inputs to original values.
    _results_tmp : ndarray
        An array the same size as the system outputs. Used to store the results temporarily.
    """

    DEFAULT_OPTIONS = {
        'step': 1e-6,
        'form': 'forward',
        'order': None,
        'step_calc': 'abs',
        'directional': False,
    }

    def __init__(self):
        """
        Initialize the ApproximationScheme.
        """
        super(FiniteDifference, self).__init__()
        self._exec_list = []
        self._starting_ins = self._starting_outs = self._results_tmp = None

    def add_approximation(self, abs_key, kwargs):
        """
        Use this approximation scheme to approximate the derivative d(of)/d(wrt).

        Parameters
        ----------
        abs_key : tuple(str,str)
            Absolute name pairing of (of, wrt) for the derivative.
        kwargs : dict
            Additional keyword arguments, to be interpreted by sub-classes.
        """
        fd_options = self.DEFAULT_OPTIONS.copy()
        fd_options.update(kwargs)

        if fd_options['order'] is None:
            form = fd_options['form']
            if form in DEFAULT_ORDER:
                fd_options['order'] = DEFAULT_ORDER[fd_options['form']]
            else:
                msg = "'{}' is not a valid form of finite difference; must be one of {}"
                raise ValueError(msg.format(form, list(DEFAULT_ORDER.keys())))

        self._exec_list.append((abs_key, fd_options))
        self._approx_groups = None

    @staticmethod
    def _key_fun(approx_tuple):
        """
        Compute the sorting key for an approximation tuple.

        Parameters
        ----------
        approx_tuple : tuple(str, str, dict)
            A given approximated derivative (of, wrt, fd_options)

        Returns
        -------
        tuple(str, str, float, int, str)
            Sorting key (wrt, form, step_size, order, step_calc, directional)

        """
        options = approx_tuple[1]
        if 'coloring' in options and options['coloring'] is not None:
            # this will only happen after the coloring has been computed
            return ('@color', options['form'], options['order'],
                    options['step'], options['step_calc'], options['directional'])
        else:
            return (approx_tuple[0][1], options['form'], options['order'],
                    options['step'], options['step_calc'], options['directional'])

    def _get_approx_data(self, system, data):
        """
        Given approximation metadata, compute necessary deltas and coefficients.

        Parameters
        ----------
        system : System
            System whose derivatives are being approximated.
        data : tuple
            Tuple of the form (wrt, form, order, step, step_calc, directional)

        Returns
        -------
        tuple
            Tuple of the form (deltas, coeffs, current_coeff)
        """
        wrt, form, order, step, step_calc, _ = data

        # FD forms are written as a collection of changes to inputs (deltas) and the associated
        # coefficients (coeffs). Since we do not need to (re)evaluate the current step, its
        # coefficient is stored seperately (current_coeff). For example,
        # f'(x) = (f(x+h) - f(x))/h + O(h) = 1/h * f(x+h) + (-1/h) * f(x) + O(h)
        # would be stored as deltas = [h], coeffs = [1/h], and current_coeff = -1/h.
        # A central second order accurate approximation for the first derivative would be stored
        # as deltas = [-2, -1, 1, 2] * h, coeffs = [1/12, -2/3, 2/3 , -1/12] * 1/h,
        # current_coeff = 0.
        fd_form = _generate_fd_coeff(form, order)

        if step_calc == 'rel':
            if wrt in system._outputs._views_flat:
                step *= np.linalg.norm(system._outputs._views_flat[wrt])
            elif wrt in system._inputs._views_flat:
                step *= np.linalg.norm(system._inputs._views_flat[wrt])

        deltas = fd_form.deltas * step
        coeffs = fd_form.coeffs / step
        current_coeff = fd_form.current_coeff / step

        return deltas, coeffs, current_coeff

    def compute_approximations(self, system, jac=None, total=False):
        """
        Execute the system to compute the approximate sub-Jacobians.

        Parameters
        ----------
        system : System
            System on which the execution is run.
        jac : None or dict-like
            If None, update system with the approximated sub-Jacobians. Otherwise, store the
            approximations in the given dict-like object.
        total : bool
            If True total derivatives are being approximated, else partials.
        """
        if len(self._exec_list) == 0:
            return

        if jac is None:
            jac = system._jacobian

        if total:
            self._starting_outs = system._outputs._data.copy()
        else:
            self._starting_outs = system._residuals._data.copy()

        self._starting_ins = system._inputs._data.copy()
        self._results_tmp = self._starting_outs.copy()

        self._compute_approximations(system, jac, total, system._outputs._under_complex_step)

        # reclaim some memory
        self._starting_ins = self._starting_outs = self._results_tmp = None

    def _get_multiplier(self, data):
        return 1.0

    def _collect_result(self, array):
        return array

    def _run_point(self, system, idx_info, data, results_array, total):
        """
        Alter the specified inputs by the given deltas, run the system, and return the results.

        Parameters
        ----------
        system : System
            The system having its derivs approximated.
        idx_info : tuple of (ndarray of int, ndarray of float)
            Tuple of wrt indices and corresponding data array to perturb.
        data : tuple of float
            Tuple of the form (deltas, coeffs, current_coeff)
        results_array : ndarray
            Where the results will be stored.
        total : bool
            If True total derivatives are being approximated, else partials.

        Returns
        -------
        ndarray
            The results from running the perturbed system.
        """
        deltas, coeffs, current_coeff = data

        if current_coeff:
            current_vec = system._outputs if total else system._residuals
            # copy data from outputs (if doing total derivs) or residuals (if doing partials)
            results_array[:] = current_vec._data
            results_array *= current_coeff
        else:
            results_array[:] = 0.

        # Run the Finite Difference
        for delta, coeff in zip(deltas, coeffs):
            results = self._run_sub_point(system, idx_info, delta, total)
            results *= coeff
            results_array += results

        return results_array

    def _run_sub_point(self, system, idx_info, delta, total):
        """
        Alter the specified inputs by the given delta, run the system, and return the results.

        Parameters
        ----------
        system : System
            The system having its derivs approximated.
        idx_info : tuple of (ndarray of int, ndarray of float)
            Tuple of wrt indices and corresponding data array to perturb.
        delta : float
            Perturbation amount.
        total : bool
            If True total derivatives are being approximated, else partials.

        Returns
        -------
        ndarray
            The results from running the perturbed system.
        """
        inputs = system._inputs
        outputs = system._outputs
        resids = system._residuals

        if total:
            run_model = system.run_solve_nonlinear
            results_vec = outputs
        else:
            run_model = system.run_apply_nonlinear
            results_vec = resids

        for arr, idxs in idx_info:
            if arr is not None:
                arr._data[idxs] += delta

        run_model()

        # save results and restore starting inputs/outputs
        self._results_tmp[:] = results_vec._data
        inputs._data[:] = self._starting_ins
        results_vec._data[:] = self._starting_outs

        # if results_vec are the residuals then we need to remove the delta's we added earlier
        # to the outputs
        if not total:
            for arr, idxs in idx_info:
                if arr is outputs:
                    arr._data[idxs] -= delta

        return self._results_tmp
