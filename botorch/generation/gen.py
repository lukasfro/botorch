#!/usr/bin/env python3
# Copyright (c) Facebook, Inc. and its affiliates.
#
# This source code is licensed under the MIT license found in the
# LICENSE file in the root directory of this source tree.

r"""
Candidate generation utilities.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple, Type, Union

import numpy as np
import torch
from botorch.acquisition import AcquisitionFunction
from botorch.generation.utils import _remove_fixed_features_from_optimization
from botorch.optim.parameter_constraints import (
    _arrayify,
    make_scipy_bounds,
    make_scipy_linear_constraints,
)
from botorch.optim.stopping import ExpMAStoppingCriterion
from botorch.optim.utils import _filter_kwargs, columnwise_clamp, fix_features
from scipy.optimize import minimize
from torch import Tensor
from torch.optim import Optimizer


def gen_candidates_scipy(
    initial_conditions: Tensor,
    acquisition_function: AcquisitionFunction,
    lower_bounds: Optional[Union[float, Tensor]] = None,
    upper_bounds: Optional[Union[float, Tensor]] = None,
    inequality_constraints: Optional[List[Tuple[Tensor, Tensor, float]]] = None,
    equality_constraints: Optional[List[Tuple[Tensor, Tensor, float]]] = None,
    options: Optional[Dict[str, Any]] = None,
    fixed_features: Optional[Dict[int, Optional[float]]] = None,
) -> Tuple[Tensor, Tensor]:
    r"""Generate a set of candidates using `scipy.optimize.minimize`.

    Optimizes an acquisition function starting from a set of initial candidates
    using `scipy.optimize.minimize` via a numpy converter.

    Args:
        initial_conditions: Starting points for optimization.
        acquisition_function: Acquisition function to be used.
        lower_bounds: Minimum values for each column of initial_conditions.
        upper_bounds: Maximum values for each column of initial_conditions.
        inequality constraints: A list of tuples (indices, coefficients, rhs),
            with each tuple encoding an inequality constraint of the form
            `\sum_i (X[indices[i]] * coefficients[i]) >= rhs`.
        equality constraints: A list of tuples (indices, coefficients, rhs),
            with each tuple encoding an inequality constraint of the form
            `\sum_i (X[indices[i]] * coefficients[i]) = rhs`.
        options: Options used to control the optimization including "method"
            and "maxiter". Select method for `scipy.minimize` using the
            "method" key. By default uses L-BFGS-B for box-constrained problems
            and SLSQP if inequality or equality constraints are present.
        fixed_features: This is a dictionary of feature indices to values, where
            all generated candidates will have features fixed to these values.
            If the dictionary value is None, then that feature will just be
            fixed to the clamped value and not optimized. Assumes values to be
            compatible with lower_bounds and upper_bounds!

    Returns:
        2-element tuple containing

        - The set of generated candidates.
        - The acquisition value for each t-batch.

    Example:
        >>> qEI = qExpectedImprovement(model, best_f=0.2)
        >>> bounds = torch.tensor([[0., 0.], [1., 2.]])
        >>> Xinit = gen_batch_initial_conditions(
        >>>     qEI, bounds, q=3, num_restarts=25, raw_samples=500
        >>> )
        >>> batch_candidates, batch_acq_values = gen_candidates_scipy(
                initial_conditions=Xinit,
                acquisition_function=qEI,
                lower_bounds=bounds[0],
                upper_bounds=bounds[1],
            )
    """
    options = options or {}

    # REDUCED is used indicate if we are optimizing over a reduced domain dimension
    # after considering fixed_features.
    # REDUCED mode if fixed_features is not None except for when fixed_features.values()
    # contains None and linear constraints are passed.
    REDUCED = fixed_features is not None
    if inequality_constraints or equality_constraints:
        REDUCED = REDUCED and (None not in fixed_features.values())

    if REDUCED:
        _no_fixed_features = _remove_fixed_features_from_optimization(
            fixed_features=fixed_features,
            acquisition_function=acquisition_function,
            initial_conditions=initial_conditions,
            lower_bounds=lower_bounds,
            upper_bounds=upper_bounds,
            inequality_constraints=inequality_constraints,
            equality_constraints=equality_constraints,
        )

        # call the routine with no fixed_features
        clamped_candidates, batch_acquisition = gen_candidates_scipy(
            initial_conditions=_no_fixed_features.initial_conditions,
            acquisition_function=_no_fixed_features.acquisition_function,
            lower_bounds=_no_fixed_features.lower_bounds,
            upper_bounds=_no_fixed_features.upper_bounds,
            inequality_constraints=_no_fixed_features.inequality_constraints,
            equality_constraints=_no_fixed_features.equality_constraints,
            options=options,
            fixed_features=None,
        )
        clamped_candidates = _no_fixed_features.acquisition_function._construct_X_full(
            clamped_candidates
        )
        return clamped_candidates, batch_acquisition

    clamped_candidates = columnwise_clamp(
        X=initial_conditions, lower=lower_bounds, upper=upper_bounds
    )

    shapeX = clamped_candidates.shape
    x0 = _arrayify(clamped_candidates.view(-1))
    bounds = make_scipy_bounds(
        X=initial_conditions, lower_bounds=lower_bounds, upper_bounds=upper_bounds
    )
    constraints = make_scipy_linear_constraints(
        shapeX=clamped_candidates.shape,
        inequality_constraints=inequality_constraints,
        equality_constraints=equality_constraints,
    )

    def f(x):
        if np.isnan(x).any():
            raise RuntimeError(
                f"{np.isnan(x).sum()} elements of the {x.size} element array "
                f"`x` are NaN."
            )
        X = (
            torch.from_numpy(x)
            .to(initial_conditions)
            .view(shapeX)
            .contiguous()
            .requires_grad_(True)
        )
        X_fix = fix_features(X, fixed_features=fixed_features)
        loss = -acquisition_function(X_fix).sum()
        # compute gradient w.r.t. the inputs (does not accumulate in leaves)
        gradf = _arrayify(torch.autograd.grad(loss, X)[0].contiguous().view(-1))
        if np.isnan(gradf).any():
            msg = (
                f"{np.isnan(gradf).sum()} elements of the {x.size} element "
                "gradient array `gradf` are NaN. This often indicates numerical issues."
            )
            if initial_conditions.dtype != torch.double:
                msg += " Consider using `dtype=torch.double`."
            raise RuntimeError(msg)
        fval = loss.item()
        return fval, gradf

    res = minimize(
        f,
        x0,
        method=options.get("method", "SLSQP" if constraints else "L-BFGS-B"),
        jac=True,
        bounds=bounds,
        constraints=constraints,
        callback=options.get("callback", None),
        options={k: v for k, v in options.items() if k not in ["method", "callback"]},
    )
    candidates = fix_features(
        X=torch.from_numpy(res.x).to(initial_conditions).reshape(shapeX),
        fixed_features=fixed_features,
    )

    clamped_candidates = columnwise_clamp(
        X=candidates, lower=lower_bounds, upper=upper_bounds, raise_on_violation=True
    )
    with torch.no_grad():
        batch_acquisition = acquisition_function(clamped_candidates)

    return clamped_candidates, batch_acquisition


def gen_candidates_torch(
    initial_conditions: Tensor,
    acquisition_function: AcquisitionFunction,
    lower_bounds: Optional[Union[float, Tensor]] = None,
    upper_bounds: Optional[Union[float, Tensor]] = None,
    optimizer: Type[Optimizer] = torch.optim.Adam,
    options: Optional[Dict[str, Union[float, str]]] = None,
    verbose: bool = True,
    fixed_features: Optional[Dict[int, Optional[float]]] = None,
) -> Tuple[Tensor, Tensor]:
    r"""Generate a set of candidates using a `torch.optim` optimizer.

    Optimizes an acquisition function starting from a set of initial candidates
    using an optimizer from `torch.optim`.

    Args:
        initial_conditions: Starting points for optimization.
        acquisition_function: Acquisition function to be used.
        lower_bounds: Minimum values for each column of initial_conditions.
        upper_bounds: Maximum values for each column of initial_conditions.
        optimizer (Optimizer): The pytorch optimizer to use to perform
            candidate search.
        options: Options used to control the optimization. Includes
            maxiter: Maximum number of iterations
        verbose: If True, provide verbose output.
        fixed_features: This is a dictionary of feature indices to values, where
            all generated candidates will have features fixed to these values.
            If the dictionary value is None, then that feature will just be
            fixed to the clamped value and not optimized. Assumes values to be
            compatible with lower_bounds and upper_bounds!

    Returns:
        2-element tuple containing

        - The set of generated candidates.
        - The acquisition value for each t-batch.

    Example:
        >>> qEI = qExpectedImprovement(model, best_f=0.2)
        >>> bounds = torch.tensor([[0., 0.], [1., 2.]])
        >>> Xinit = gen_batch_initial_conditions(
        >>>     qEI, bounds, q=3, num_restarts=25, raw_samples=500
        >>> )
        >>> batch_candidates, batch_acq_values = gen_candidates_torch(
                initial_conditions=Xinit,
                acquisition_function=qEI,
                lower_bounds=bounds[0],
                upper_bounds=bounds[1],
            )
    """
    options = options or {}

    # REDUCED is used indicate if we are optimizing over a reduced domain dimension
    # after considering fixed_features.
    REDUCED = fixed_features is not None

    if REDUCED:
        _no_fixed_features = _remove_fixed_features_from_optimization(
            fixed_features=fixed_features,
            acquisition_function=acquisition_function,
            initial_conditions=initial_conditions,
            lower_bounds=lower_bounds,
            upper_bounds=upper_bounds,
            inequality_constraints=None,
            equality_constraints=None,
        )

        # call the routine with no fixed_features
        clamped_candidates, batch_acquisition = gen_candidates_torch(
            initial_conditions=_no_fixed_features.initial_conditions,
            acquisition_function=_no_fixed_features.acquisition_function,
            lower_bounds=_no_fixed_features.lower_bounds,
            upper_bounds=_no_fixed_features.upper_bounds,
            optimizer=optimizer,
            options=options,
            verbose=verbose,
            fixed_features=None,
        )
        clamped_candidates = _no_fixed_features.acquisition_function._construct_X_full(
            clamped_candidates
        )
        return clamped_candidates, batch_acquisition

    clamped_candidates = columnwise_clamp(
        X=initial_conditions, lower=lower_bounds, upper=upper_bounds
    ).requires_grad_(True)
    bayes_optimizer = optimizer(
        params=[clamped_candidates], lr=options.get("lr", 0.025)
    )
    i = 0
    stop = False
    stopping_criterion = ExpMAStoppingCriterion(
        **_filter_kwargs(ExpMAStoppingCriterion, **options)
    )
    while not stop:
        i += 1
        loss = -acquisition_function(clamped_candidates).sum()
        if verbose:
            print("Iter: {} - Value: {:.3f}".format(i, -(loss.item())))

        def closure():
            bayes_optimizer.zero_grad()
            output_grad = torch.autograd.grad(loss, clamped_candidates)[0]
            clamped_candidates.grad = output_grad
            return loss

        bayes_optimizer.step(closure)
        with torch.no_grad():
            clamped_candidates = columnwise_clamp(
                X=clamped_candidates, lower=lower_bounds, upper=upper_bounds
            ).requires_grad_(True)
        stop = stopping_criterion.evaluate(fvals=loss.detach())
    clamped_candidates = columnwise_clamp(
        X=clamped_candidates,
        lower=lower_bounds,
        upper=upper_bounds,
        raise_on_violation=True,
    )
    with torch.no_grad():
        batch_acquisition = acquisition_function(clamped_candidates)

    return clamped_candidates, batch_acquisition


def get_best_candidates(batch_candidates: Tensor, batch_values: Tensor) -> Tensor:
    r"""Extract best (q-batch) candidate from batch of candidates

    Args:
        batch_candidates: A `b x q x d` tensor of `b` q-batch candidates, or a
            `b x d` tensor of `b` single-point candidates.
        batch_values: A tensor with `b` elements containing the value of the
            respective candidate (higher is better).

    Returns:
        A tensor of size `q x d` (if q-batch mode) or `d` from batch_candidates
        with the highest associated value.

    Example:
        >>> qEI = qExpectedImprovement(model, best_f=0.2)
        >>> bounds = torch.tensor([[0., 0.], [1., 2.]])
        >>> Xinit = gen_batch_initial_conditions(
        >>>     qEI, bounds, q=3, num_restarts=25, raw_samples=500
        >>> )
        >>> batch_candidates, batch_acq_values = gen_candidates_scipy(
                initial_conditions=Xinit,
                acquisition_function=qEI,
                lower_bounds=bounds[0],
                upper_bounds=bounds[1],
            )
        >>> best_candidates = get_best_candidates(batch_candidates, batch_acq_values)
    """
    best = torch.argmax(batch_values.view(-1), dim=0)
    return batch_candidates[best]
