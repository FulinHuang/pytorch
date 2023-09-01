import copy
import functools
import logging
import warnings
from contextlib import ExitStack
from dataclasses import dataclass, field
from typing import (
    Any,
    cast,
    Dict,
    Iterable,
    Iterator,
    List,
    NamedTuple,
    Optional,
    Sequence,
    Set,
    Tuple,
    Union,
)

import torch
import torch.distributed as dist
import torch.distributed.fsdp._traversal_utils as traversal_utils
import torch.nn as nn
from torch.distributed._shard.sharded_tensor import ShardedTensor
from torch.distributed._tensor import DTensor
from torch.distributed.distributed_c10d import _get_pg_default_device
from torch.distributed.fsdp._common_utils import (
    _apply_to_modules,
    _FSDPState,
    _get_module_fsdp_state_if_fully_sharded_module,
    _get_param_to_fqns,
    _module_handle,
    _named_parameters_with_duplicates,
    clean_tensor_name,
)
from torch.distributed.fsdp._debug_utils import SimpleProfiler
from torch.distributed.fsdp._fsdp_extensions import (
    _ext_chunk_dtensor,
    _ext_chunk_tensor,
)
from torch.distributed.fsdp._runtime_utils import (
    _lazy_init,
    _reset_flat_param_grad_info_if_needed,
)
from torch.distributed.fsdp._shard_utils import _gather_state_dict
from torch.distributed.fsdp.api import ShardingStrategy
from torch.distributed.fsdp.flat_param import FlatParameter, FlatParamHandle
from torch.utils._pytree import tree_map_only


logger = logging.getLogger(__name__)


@dataclass
class FSDPParamInfo:
    state: _FSDPState
    handle: FlatParamHandle
    param_indices: Dict[str, int]


def sorted_items(dictionary: Dict[str, Any]) -> Iterator[Tuple[str, Any]]:
    keys = sorted(dictionary.keys())
    for k in keys:
        yield k, dictionary[k]


@dataclass
class _ConsolidatedOptimState:
    """
    This holds the consolidated optimizer state on the target rank. Positive-
    dimension tensor state is communicated across ranks, while zero-dimension
    tensor state and non-tensor state is taken directly from the target rank.

    PyTorch version 1.12 moved to using zero-dimension tensors for scalar
    values, but user implemented optimizers may still use float (i.e. a
    non-tensor). Thus, we support both and handle them identically.

    Attributes:
        tensor_state (Dict[str, torch.Tensor]): Mapping from positive-dimension
            tensor state name to the unsharded flat tensor representing the
            state.
        zero_dim_tensor_state (Dict[str, torch.Tensor]): Mapping from zero-
            dimension tensor state name to its value.
        non_tensor_state (Dict[str, Any]): Mapping from non-tensor state
            name to its value.
    """

    tensor_state: Dict[str, torch.Tensor] = field(default_factory=dict)
    zero_dim_tensor_state: Dict[str, torch.Tensor] = field(default_factory=dict)
    non_tensor_state: Dict[str, Any] = field(default_factory=dict)


class _PosDimTensorInfo(NamedTuple):
    """
    Meatadata for positive-dimension tensors used internally for
    :meth:`scatter_full_optim_state_dict`.

    Attributes:
        shape (torch.Size): Sharded tensor shape (which is equal to the
            unsharded tensor shape if the tensor is optimizer state for a
            non-FSDP parameter and is hence not sharded).
        dtype (torch.dtype): Data type of the tensor.
    """

    shape: torch.Size
    dtype: torch.dtype


class _OptimStateKey(NamedTuple):
    """
    This represents an optimizer state key that may be used commonly across
    ranks. It is based on the unflattened parameter names rather than parameter
    IDs to make it independent of each rank's own optimizer construction.
    """

    unflat_param_names: Tuple[str, ...]
    is_fsdp_managed: bool


def _unflatten_optim_state(
    fsdp_param_info: FSDPParamInfo,
    flat_param_state: Dict[str, Any],
    to_save: bool,
    shard_state: bool,
) -> List[Dict[str, Any]]:
    """
    Unflattens the optimizer state, consisting of the "state" part and the
    "param_groups" part. Unflattening the "state" part involves consolidating
    the state on the target rank and remapping from flattened to unflattened
    parameter IDs, and the "param_groups" part only involves remapping from
    flattened to unflattened parameter IDs.

    Args:
        fsdp_param_info (FSDPParamInfo): The FSDP state, the handle, and a
            mapping from FQN to original parameter index.
        flat_param_state (Dict[str, Any]): Entry for the flat parameter in the
            "state" part of the optimizer state dict.
        to_save (bool): Whether to save the state on this rank.

    Returns:
        List[Dict[str, Any]]: A :class:`list` holding the entries in the
        "state" part of the optimizer state dict corresponding to the
        unflattened parameters comprising the flat parameter if on the target
        rank or an empty :class:`list` otherwise. The final optimizer state
        dict will need to map these entries using the proper unflattened
        parameter IDs.
    """
    assert (
        not shard_state or to_save
    ), "If ``shard_state`` is True, ``to_save`` has to be True."
    consolidated_state = _communicate_optim_state(
        fsdp_param_info,
        flat_param_state,
    )
    if to_save:
        unflat_param_state = _unflatten_communicated_optim_state(
            fsdp_param_info,
            consolidated_state,
            shard_state,
        )
        for optim_state in unflat_param_state:
            # We can't use .items() below cuz we'd run into a concurrent modification error
            for key in list(optim_state.keys()):
                state = optim_state[key]
                if isinstance(state, torch.Tensor):
                    optim_state[key] = state.cpu()
        return unflat_param_state
    else:
        return []


def _is_zero_dim_tensor(x: Any) -> bool:
    return torch.is_tensor(x) and x.dim() == 0


def _communicate_optim_state(
    fsdp_param_info: FSDPParamInfo,
    flat_param_state: Dict[str, Any],
) -> _ConsolidatedOptimState:
    """
    Communicates the optimizer state for a flat parameter across ranks. All
    ranks will hold the entire non-sharded optimizer state on GPU.

    If ``N`` is the number of tensor optimizer states in the optimizer state
    dict, then the communication complexity is 0 if ``N = 0`` and ``N + 1``
    otherwise (where the plus 1 comes from all-gathering the padding per rank).

    Args:
        fsdp_param_info (FSDPParamInfo): The FSDP state, the handle, and a
            mapping from FQN to original parameter index.
        flat_param_state (Dict[str, Any]): The entry in the "state" part of the
            optimizer state dict corresponding to the flat parameter.

    Returns:
        ConsolidatedOptimState: Consolidated optimizer state for the target
        flat parameter.
    """
    fsdp_state = fsdp_param_info.state
    flat_param = fsdp_param_info.handle.flat_param
    state = _ConsolidatedOptimState()
    tensor_state, zero_dim_tensor_state, non_tensor_state = (
        state.tensor_state,
        state.zero_dim_tensor_state,
        state.non_tensor_state,
    )

    for state_name, value in sorted_items(flat_param_state):
        # Positive-dimension tensor state: communicate across ranks
        if torch.is_tensor(value) and value.dim() > 0:
            # If the parameter is not sharded, then neither is the
            # positive-dimension tensor state, so no need to communicate it --
            # we take the target rank's value
            if (
                fsdp_state.world_size == 1
                or fsdp_state.sharding_strategy == ShardingStrategy.NO_SHARD
            ):
                tensor_state[state_name] = value
                continue
            assert (
                fsdp_state.compute_device is not None
            ), "compute_device has not been initialized"
            if value.device.type != fsdp_state.compute_device.type:
                value = value.to(fsdp_state.compute_device)
            # Assume that positive-dimension tensor optimizer state
            # has the same shape as the sharded flat parameter
            buffer_size = flat_param._full_param_padded.size()  # type: ignore[attr-defined]
            tensor_buffer = value.new_zeros(*buffer_size)
            dist.all_gather_into_tensor(
                tensor_buffer, value, group=fsdp_state.process_group
            )
            fsdp_state._device_handle.synchronize()
            unpadded_numel = cast(
                nn.Parameter, flat_param._unpadded_unsharded_size
            ).numel()
            tensor_state[state_name] = tensor_buffer[:unpadded_numel]
        # Zero-dimension tensor state and non-tensor state: take this rank's
        # value directly
        else:
            if _is_zero_dim_tensor(value):
                zero_dim_tensor_state[state_name] = value.detach().clone()
            else:
                non_tensor_state[state_name] = value
    return state


def _unflatten_communicated_optim_state(
    fsdp_param_info: FSDPParamInfo,
    state: _ConsolidatedOptimState,
    shard_state: bool,
) -> List[Dict[str, Any]]:
    """
    Unflattens the communicated optimizer state (given by ``tensor_state``,
    ``non_tensor_state``, and ``zero_dim_tensor_state``) for a single flat
    parameter. This should only be called on the target rank.

    Args:
        fsdp_param_info (FSDPParamInfo): The FSDP state, the handle, and a
            mapping from FQN to original parameter index.
        state (_ConsolidatedOptimState): Consolidated optimizer state.

    Returns:
        List[Dict[str, Any]]: A :class:`list` holding the entries in the
        "state" part of the optimizer state dict corresponding to the
        unflattened parameters comprising the flat parameter. The final
        optimizer state dict will need to map these entries using the proper
        unflattened parameter IDs.
    """
    fsdp_state = fsdp_param_info.state
    handle = fsdp_param_info.handle
    flat_param = handle.flat_param
    unflat_param_state: List[Dict[str, Any]] = []
    flat_param_views: Dict[str, Iterator] = {}
    num_unflat_params = flat_param._num_params
    tensor_state, zero_dim_tensor_state, non_tensor_state = (
        state.tensor_state,
        state.zero_dim_tensor_state,
        state.non_tensor_state,
    )

    for _ in range(num_unflat_params):
        unflat_state_param = {}
        # Add positive-dimension tensor state: unflatten with views
        for state_name, flat_tensor in sorted_items(tensor_state):
            views_generated = state_name in flat_param_views
            if not views_generated:
                views = handle._get_unflat_views(flat_tensor)
                flat_param_views[state_name] = views
            else:
                views = flat_param_views[state_name]
            optim_state: Union[torch.Tensor, ShardedTensor, DTensor] = next(views)
            if shard_state:
                if not fsdp_state._optim_state_dict_config.use_dtensor:
                    assert fsdp_state.process_group is not None
                    optim_state = _ext_chunk_tensor(
                        optim_state,
                        fsdp_state.rank,
                        fsdp_state.world_size,
                        fsdp_state._device_handle.device_count(),
                        fsdp_state.process_group,
                    )
                else:
                    assert fsdp_state._device_mesh is not None
                    optim_state = _ext_chunk_dtensor(
                        optim_state, fsdp_state.rank, fsdp_state._device_mesh
                    )

            unflat_state_param[state_name] = optim_state

        # Add zero-dimension tensor state: take the target rank's value
        for state_name, zero_dim_tensor in sorted_items(zero_dim_tensor_state):
            unflat_state_param[state_name] = zero_dim_tensor
        # Add non-tensor state: take the target rank's value
        for state_name, non_tensor in sorted_items(non_tensor_state):
            unflat_state_param[state_name] = non_tensor
        unflat_param_state.append(unflat_state_param)
    return unflat_param_state


def _broadcast_processed_state(
    fsdp_state: _FSDPState,
    optim_state: Dict[str, Any],
    group: Optional[dist.ProcessGroup],
) -> Dict[str, Any]:
    objects: List[Any] = [None]
    if fsdp_state.rank == 0:
        objects[0] = tree_map_only(
            torch.Tensor,
            lambda v: v.cpu() if v.dim() == 0 else _PosDimTensorInfo(v.shape, v.dtype),
            optim_state,
        )
    dist.broadcast_object_list(objects, src=0, group=group)
    if fsdp_state.rank == 0:
        return optim_state
    else:
        return objects[0]


def _broadcast_state(
    fsdp_state: _FSDPState, state: Any, group: Optional[dist.ProcessGroup]
) -> Any:
    device = _get_pg_default_device(group)
    if fsdp_state.rank == 0:
        if not isinstance(state, torch.Tensor) or state.dim() == 0:
            return state
        tensor = state.to(device)
    else:
        if isinstance(state, torch.Tensor):
            assert state.dim() == 0, (
                "For non-zero ranks, a tensor state should have zero dimension, "
                "but got the state with shape {state.shape()}."
            )
            return state
        elif not isinstance(state, _PosDimTensorInfo):
            return state
        tensor = torch.zeros(state.shape, dtype=state.dtype, device=device)
    dist.broadcast(tensor, src=0, group=group)
    return tensor


def _shard_orig_param_state(
    fsdp_param_info: FSDPParamInfo,
    fqn: str,
    optim_state: Dict[str, Any],
) -> Dict[str, Any]:
    """
    Shard the optimizer state for the original parameter with the name ``fqn``.
    This API should only be used when ``use_orig_params`` is True.
    """
    if not optim_state:
        return {}
    fsdp_state = fsdp_param_info.state
    flat_param = fsdp_param_info.handle.flat_param
    param_idx = fsdp_param_info.param_indices[fqn]
    shard_param_info = flat_param._shard_param_infos[param_idx]  # type: ignore[attr-defined]
    optim_state = _gather_state_dict(optim_state, fsdp_state.process_group)
    if not shard_param_info.in_shard:
        return {}
    # Flatten and shard the state.
    new_optim_state: Dict[str, Any] = {}
    intra_param_start_idx = shard_param_info.intra_param_start_idx
    intra_param_end_idx = shard_param_info.intra_param_end_idx
    for state_name, value in optim_state.items():
        if (
            torch.is_tensor(value)
            and value.dim() > 0
            and fsdp_state.sharding_strategy != ShardingStrategy.NO_SHARD
        ):
            value = value.flatten()[intra_param_start_idx : intra_param_end_idx + 1]  # type: ignore[operator]
        new_optim_state[state_name] = value
    return new_optim_state


def _flatten_optim_state_dict(
    optim_state_dict: Dict[str, Any],
    model: nn.Module,
    use_orig_params: bool = False,
    optim: Optional[torch.optim.Optimizer] = None,
    rank0_only: bool = False,
    group: Optional[dist.ProcessGroup] = None,
) -> Dict[str, Any]:
    """
    Flattens the full optimizer state dict, still keying by unflattened parameter
    names.

    If ``use_orig_params`` is True, each rank will have all FSDP-managed
    parameters but some of these parameters may be empty due to the sharding.
    For a regular optim.Optimizer, states for those empty parameters will
    not be initialized. So, when aggregating the FQNs across ranks, no assert
    will be raised on a rank even if it does not have all the states -- it is
    valid and FSDP know how to aggregate them. However, FSDP has to ignore
    handling those parameters that are not managed by FSDP and do not exist on
    the local rank -- it is managed by other parallelism and FSDP does not
    know ho to handle/aggregate them.

    Note that ``_flatten_tensor_optim_state`` does not need ``optim`` to
    flatten/shard the state. However, NamedOptimizer and KeyedOptimizer require
    all the states even if the corresponding parameters are empty. To this end,
    ``optim`` will be used to to get the initial state of the empty parameters.
    ``optim`` should only be non-None if the ``optim` is KeyedOptimizer or
    NamedOptimizer.

    Returns:
        Dict[str, Any]: The flattened optimizer state dict.
    """
    SimpleProfiler.reset()

    unflat_osd = optim_state_dict
    if "state" not in unflat_osd and not rank0_only:
        raise ValueError(
            '`optim_state_dict` must have the keys "state"'
            "to be a valid optimizer state dict"
        )
    param_to_fqns = _get_param_to_fqns(model)
    fqn_to_fsdp_param_info = _get_fqn_to_fsdp_param_info(model)
    fsdp_state = next(iter(fqn_to_fsdp_param_info.values())).state

    # Broadcast unflat_osd without non-scalar tensor if rank0_only is True.
    if rank0_only:
        unflat_osd = _broadcast_processed_state(fsdp_state, unflat_osd, group=group)

    # Construct the "state" part
    flat_osd_state: Dict[Union[_OptimStateKey, str], Any] = {}
    unflat_osd_state = unflat_osd["state"]
    all_state_keys = set(unflat_osd_state.keys())

    for param, fqns in param_to_fqns.items():
        fqn = fqns[0]
        if fqn not in unflat_osd_state:
            continue
        all_state_keys.difference_update(fqns)

        if rank0_only:
            for fqn in fqns:
                if not unflat_osd_state[fqn]:
                    continue
                for state_name in unflat_osd_state[fqn].keys():
                    unflat_osd_state[fqn][state_name] = _broadcast_state(
                        fsdp_state, unflat_osd_state[fqn][state_name], group=group
                    )
            fqn = fqns[0]
        if fqn in fqn_to_fsdp_param_info:
            fsdp_param_info = fqn_to_fsdp_param_info[fqn]
            if use_orig_params:
                with SimpleProfiler.profile(SimpleProfiler.Type.RESHARDING):
                    flat_state = _shard_orig_param_state(
                        fsdp_param_info,
                        fqn,
                        unflat_osd_state[fqn],
                    )
            else:
                flat_state = _flatten_optim_state(
                    fsdp_param_info,
                    unflat_osd_state,
                    fqns,
                )
            key = _OptimStateKey(tuple(fqns), True)
            # Only include non-empty states since as expected by
            # `torch.optim.Optimizer` s unless the optimizer is KeyedOptimizer
            # or NamedOptimizer.
            if flat_state:
                flat_osd_state[key] = flat_state
            elif use_orig_params:
                assert (
                    len(fqns) == 1
                ), f"use_orig_params is True but there are multiple FQNs, {fqns}."
                if optim is not None:  # NamedOptimizer or KeyedOptimizer case.
                    state = optim.state.get(param, None)  # type: ignore[call-overload]
                    if state is not None:
                        flat_osd_state[key] = copy.deepcopy(state)
                    else:
                        warnings.warn(
                            f"optim_state[{key}] is not on rank{fsdp_state.rank}."
                        )

            else:
                raise RuntimeError(
                    f"The state of {key} is empty. This should happen when "
                    "use_orig_params=True."
                )
        else:  # do not flatten non-FSDP parameters' states
            assert len(fqns) == 1
            key = _OptimStateKey(tuple(fqns), False)
            flat_osd_state[key] = copy.copy(unflat_osd_state[fqn])

        if rank0_only:
            for fqn in fqns:
                if not unflat_osd_state[fqn]:
                    continue
                for state_name, param_state in list(unflat_osd_state[fqn].items()):
                    if fsdp_state.rank > 0:
                        # Deference the tensor so that PyTorch can collect the memory.
                        del unflat_osd_state[fqn][state_name]
                    else:
                        # Move the tensor in the original osd back to CPU to make the
                        # original osd unaffected.
                        unflat_osd_state[fqn][state_name] = unflat_osd_state[fqn][
                            state_name
                        ].cpu()

    # Handle user-defined state, states that are not associated with parameters.
    for key in all_state_keys:
        user_state = unflat_osd_state[key]
        if isinstance(user_state, torch.Tensor) and rank0_only and use_orig_params:
            user_state = _broadcast_state(fsdp_state, user_state, group=group)
        flat_osd_state[key] = copy.copy(user_state)

    SimpleProfiler.dump_and_reset("FSDP _flatten_optim_state_dict() profiling: ")
    # Construct the "param_groups" part -- copy as is since it will be
    # rekeyed later according to the target rank's optimizer
    # Only copy param_groups if it exists in unflat_osd
    if "param_groups" in unflat_osd:
        flat_osd_param_groups = copy.deepcopy(unflat_osd["param_groups"])
        return {"state": flat_osd_state, "param_groups": flat_osd_param_groups}
    else:
        return {"state": flat_osd_state}


def _flatten_optim_state(
    fsdp_param_info: FSDPParamInfo,
    unflat_osd_state: Dict[str, Dict[str, Any]],
    unflat_param_names: List[str],
) -> Dict[str, Any]:
    """
    Flattens the optimizer state in ``full_optim_state_dict`` for a single
    flat parameter in ``fsdp_param_info`` corresponding to the unflattened
    parameter names in ``unflat_param_names``.

    Args:
        fsdp_param_info (FSDPParamInfo): The FSDP state, the handle, and a
            mapping from FQN to original parameter index.
        unflat_osd_state (Dict[str, Dict[str, Any]]): The "state" part of the
            optimizer state dict corresponding to the unflattened parameters.
        unflat_param_names (List[str]): A :class:`list` of unflattened
            parameter names corresponding to the flat parameter ``flat_param``.

    Returns:
        Dict[str, Any]: A :class:`dict` mapping state names to their values for
        a particular flat parameter. The sharded optimizer state dict's "state"
        part will map a key to this returned value.
    """
    fsdp_state = fsdp_param_info.state
    handle = fsdp_param_info.handle
    flat_param = handle.flat_param
    num_unflat_params = len(unflat_param_names)
    assert num_unflat_params > 0, (
        "Expects at least one unflattened parameter corresponding to the "
        "flat parameter"
    )
    unflat_param_shapes = flat_param._shapes
    num_unflat_param_shapes = len(unflat_param_shapes)
    assert (
        num_unflat_params == num_unflat_param_shapes
    ), f"Expects {num_unflat_params} shapes but got {num_unflat_param_shapes}"

    # Check if these unflattened parameters have any optimizer state
    has_state = [
        bool(unflat_param_name in unflat_osd_state)
        for unflat_param_name in unflat_param_names
    ]
    # If none of the unflattened parameters comprising this flat parameter have
    # any state, then we do not want an entry in the optimizer state dict
    if not any(has_state):
        return {}  # no need to flatten any state
    # There may still be some unflattened parameters with state and some
    # without
    unflat_param_states = [
        _gather_state_dict(
            unflat_osd_state[unflat_param_name], pg=fsdp_state.process_group
        )
        if unflat_param_name in unflat_osd_state
        else None
        for unflat_param_name in unflat_param_names
    ]
    # Check that the unflattened parameters have the same state names
    state_names = None
    for unflat_param_state in unflat_param_states:
        if unflat_param_state is None:
            continue
        if state_names is None:
            state_names = set(unflat_param_state.keys())
        else:
            if state_names != set(unflat_param_state.keys()):
                raise ValueError(
                    "Differing optimizer state names for the unflattened "
                    f"parameters: {unflat_param_names}"
                )
    assert state_names is not None

    # Flatten the state
    flat_state: Dict[str, Any] = {}
    for state_name in state_names:
        state_values = [
            unflat_param_state[state_name] if unflat_param_state is not None else None
            for unflat_param_state in unflat_param_states
        ]
        non_none_state_values = [v for v in state_values if v is not None]
        # If all ranks have None, this is a None value
        if not non_none_state_values:
            flat_state[state_name] = None
            continue
        are_pos_dim_tensors = are_zero_dim_tensors = are_non_tensors = True
        for v in non_none_state_values:
            are_pos_dim_tensors &= torch.is_tensor(v) and v.dim() > 0
            are_zero_dim_tensors &= _is_zero_dim_tensor(v)
            are_non_tensors &= not torch.is_tensor(v)
        types = {type(v) for v in non_none_state_values}
        if len(types) != 1 or not (
            are_pos_dim_tensors or are_zero_dim_tensors or are_non_tensors
        ):
            raise ValueError(
                f"Differing optimizer state types for state {state_name}, "
                f"values {non_none_state_values}, and unflattened parameter "
                f"names {unflat_param_names}"
            )
        if are_pos_dim_tensors:
            flat_tensor = _flatten_tensor_optim_state(
                state_name,
                state_values,
                unflat_param_names,
                unflat_param_shapes,
                handle,
            )
            # Shard the flattened tensor immediately to minimize max memory
            # usage
            if (
                fsdp_state.world_size != 1
                and fsdp_state.sharding_strategy != ShardingStrategy.NO_SHARD
            ):
                sharded_flat_tensor, _ = FlatParamHandle._get_shard(
                    flat_tensor,
                    fsdp_state.rank,
                    fsdp_state.world_size,
                )
            else:
                sharded_flat_tensor = flat_tensor
            flat_state[state_name] = sharded_flat_tensor
        elif are_zero_dim_tensors:
            flat_state[state_name] = _flatten_zero_dim_tensor_optim_state(
                state_name,
                state_values,
                unflat_param_names,
            )
        else:
            assert are_non_tensors
            flat_state[state_name] = _flatten_non_tensor_optim_state(
                state_name,
                state_values,
                unflat_param_names,
            )

    return flat_state


def _flatten_tensor_optim_state(
    state_name: str,
    pos_dim_tensors: List[torch.Tensor],
    unflat_param_names: List[str],
    unflat_param_shapes: Sequence[torch.Size],
    handle: FlatParamHandle,
) -> torch.Tensor:
    """
    Flattens the positive-dimension tensor optimizer state given by the values
    ``tensors`` for the state ``state_name`` for a single flat parameter
    from ``handle`` corresponding to the unflattened parameter names
    ``unflat_param_names`` and unflatted parameter shapes
    ``unflat_param_shapes``. This flattens each unflattened parameter's tensor
    state into one tensor.

    NOTE: We use zero tensors for any unflattened parameters without state
    since some value is required to fill those entries. This assumes that the
    zero tensor is mathematically equivalent to having no state, which is true
    for Adam's "exp_avg" and "exp_avg_sq" but may not be true for all
    optimizers.

    Args:
        state_name (str): Optimizer state name.
        pos_dim_tensors (List[torch.Tensor]): Positive-dimension tensor
            optimizer state values for the unflattened parameters corresponding
            to the single flat parameter.
        unflat_param_names (List[str]): A :class:`list` of unflattened
            parameter names corresponding to the single flat parameter.
        unflat_param_shapes (List[torch.Size]): Unflattened parameter shapes
            corresponding to the single flat parameter.
        handle (FlatParamHandle): The flat parameter's handle.

    Returns:
        torch.Tensor: A flat tensor containing the optimizer state
        corresponding to ``state_name`` constructed by concatenating the
        unflattened parameter tensor states in ``pos_dim_tensors`` (using zero
        tensors for any unflattened parameters without the state).
    """
    flat_param = handle.flat_param
    non_none_tensors = [t for t in pos_dim_tensors if t is not None]
    # Check that all are tensors with the same dtype
    dtypes = {t.dtype for t in non_none_tensors}
    if len(dtypes) != 1:
        raise ValueError(
            "All unflattened parameters comprising a single flat "
            "parameter must have positive-dimension tensor state with the "
            f"same dtype but got dtypes {dtypes} for state {state_name} and "
            f"unflattened parameter names {unflat_param_names}"
        )
    dtype = next(iter(dtypes))
    # Check that each tensor state matches its parameter's shape
    for tensor, shape in zip(pos_dim_tensors, unflat_param_shapes):
        if tensor is None and len(shape) == 0:
            raise ValueError("Flattening a zero-dimension parameter is not supported")
        elif tensor is not None and tensor.shape != shape:
            raise ValueError(
                "Tensor optimizer state does not have same shape as its "
                f"parameter: {tensor.shape} {shape}"
            )
    # Flatten the tensor states: we do not need to add any right-hand-side
    # padding since the flat optimizer state tensor is sharded via
    # `_get_shard()`, which pads the shard as needed (just like for the flat
    # parameter)
    cpu_device = torch.device("cpu")
    tensors_to_flatten = [
        torch.flatten(state_value.to(cpu_device))
        if state_value is not None
        else torch.flatten(
            torch.zeros(
                size=shape,
                dtype=dtype,
                device=cpu_device,
            )
        )
        for state_value, shape in zip(pos_dim_tensors, unflat_param_shapes)
    ]
    flat_tensor = handle.flatten_tensors(tensors_to_flatten, handle._aligned_numel)
    flat_param_shape = flat_param._unpadded_unsharded_size  # type: ignore[attr-defined]
    assert flat_tensor.shape == flat_param_shape, (
        f"tensor optim state: {flat_tensor.shape} "
        f"flat parameter: {flat_param_shape}"
    )
    return flat_tensor


def _flatten_zero_dim_tensor_optim_state(
    state_name: str,
    zero_dim_tensors: List[torch.Tensor],
    unflat_param_names: List[str],
) -> torch.Tensor:
    """
    Flattens the zero-dimension tensor optimizer state given by the values
    ``zero_dim_tensors`` for the state ``state_name`` for a single flat
    parameter corresponding to the unflattened parameter names
    ``unflat_param_names`` by enforcing that all tensors are the same and using
    that common value.

    NOTE: The requirement that the tensors are the same across all unflattened
    parameters comprising the flat parameter is needed to maintain the
    invariant that FSDP performs the same computation as its non-sharded
    equivalent. This means that none of the unflattened parameters can be
    missing this state since imposing a value may differ from having no value.
    For example, for Adam's "step", no value means maximum bias correction,
    while having some positive value means less bias correction.

    Args:
        state_name (str): Optimizer state name.
        zero_dim_tensors (List[torch.Tensor]): Zero-dimension optimizer state
            for the unflattened parameters corresponding to the single
            flat parameter.
        unflat_param_names (List[str]): A :class:`list` of unflattened
            parameter names corresponding to the single flat parameter.

    Returns:
        torch.Tensor: A zero-dimensional tensor giving the value of the state
        ``state_name`` for all unflattened parameters corresponding to the
        names ``unflat_param_names``.
    """
    non_none_tensors = [t for t in zero_dim_tensors if t is not None]
    # Enforce that all have the same value and dtype
    values_set = {t.item() if t is not None else None for t in zero_dim_tensors}
    dtypes = {t.dtype if t is not None else None for t in zero_dim_tensors}
    if (
        len(non_none_tensors) != len(zero_dim_tensors)
        or len(values_set) != 1
        or len(dtypes) != 1
    ):
        raise ValueError(
            "All unflattened parameters comprising a single flat "
            "parameter must have scalar state with the same value and dtype "
            f"but got values {values_set} and dtypes {dtypes} for state "
            f"{state_name} and unflattened parameter names "
            f"{unflat_param_names}"
        )
    value = next(iter(values_set))
    dtype = next(iter(dtypes))
    return torch.tensor(value, dtype=dtype, device=torch.device("cpu"))


def _flatten_non_tensor_optim_state(
    state_name: str,
    non_tensors: List[Any],
    unflat_param_names: List[str],
) -> Any:
    """
    Flattens the non-tensor optimizer state given by the values ``non_tensors``
    for the state ``state_name`` for a single flat parameter corresponding
    to the unflattened parameter names ``unflat_param_names`` by enforcing that
    all values are the same and using that common value.

    See the note in :func:`_flatten_zero_dim_tensor_optim_state`.

    Args:
        state_name (str): Optimizer state name.
        non_tensors (List[Any]): Non-tensor optimizer state for the unflattened
            parameters corresponding to the single flat parameter.
        unflat_param_names (List[str]): A :class:`list` of unflattened
            parameter names corresponding to the single flat parameter.

    Returns:
        Any: A non-tensor giving the value of the state ``state_name`` for all
        unflattened parameters corresponding to the names
        ``unflat_param_names``.
    """
    non_none_non_tensors = [nt for nt in non_tensors if nt is not None]
    # Enforce that all have the same value (same type already checked)
    non_tensor_set = set(non_tensors)
    if len(non_none_non_tensors) != len(non_tensors) or len(non_tensor_set) != 1:
        raise ValueError(
            "All unflattened parameters comprising a single flat "
            "parameter must have scalar state with the same value and dtype "
            f"but got values {non_tensor_set} for state {state_name} and  "
            f"unflattened parameter names {unflat_param_names}"
        )
    non_tensor = next(iter(non_tensor_set))
    return non_tensor


def _rekey_sharded_optim_state_dict(
    sharded_osd: Dict[str, Any],
    model: nn.Module,
    optim: torch.optim.Optimizer,
    optim_input: Optional[
        Union[
            List[Dict[str, Any]],
            Iterable[nn.Parameter],
        ]
    ],
    using_optim_input: bool,
    is_named_optimizer: bool = False,
) -> Dict[str, Any]:
    """
    Rekeys the optimizer state dict from unflattened parameter names to flat
    parameter IDs according to the calling rank's ``optim``, which may be
    different across ranks. In particular, the unflattened parameter names are
    represented as :class:`_OptimStateKey` s.
    """
    param_to_fqns = _get_param_to_fqns(model)
    flat_param_to_fqn = _get_flat_param_to_fqn(model)
    param_to_param_key: Dict[nn.Parameter, Union[int, str]] = cast(
        Dict[nn.Parameter, Union[int, str]],
        (
            _get_param_to_param_id_from_optim_input(model, optim_input)
            if using_optim_input
            else _get_param_to_param_key(
                optim, model, is_named_optimizer, param_to_fqns, flat_param_to_fqn
            )
        ),
    )
    # All parameter keys in `param_to_param_key` should be in
    # `param_to_fqns` -- strict inequality follows when not all parameters are
    # passed to the optimizer
    assert len(param_to_param_key) <= len(param_to_fqns)

    unflat_param_names_to_flat_param_key: Dict[
        Tuple[str, ...], Union[int, str]
    ] = {}  # for "state"
    unflat_param_name_to_flat_param_key: Dict[
        str, Union[int, str]
    ] = {}  # for "param_groups"
    for param, unflat_param_names in param_to_fqns.items():
        if param not in param_to_param_key:
            # This parameter was not passed to the optimizer
            continue
        flat_param_key = param_to_param_key[param]
        unflat_param_names_to_flat_param_key[tuple(unflat_param_names)] = flat_param_key
        for unflat_param_name in unflat_param_names:
            unflat_param_name_to_flat_param_key[unflat_param_name] = flat_param_key

    sharded_osd_state = sharded_osd["state"]
    rekeyed_osd_state: Dict[Union[str, int], Any] = {}
    for key, param_state in sharded_osd_state.items():
        if isinstance(key, str):
            rekeyed_osd_state[key] = param_state
            continue
        flat_param_key = unflat_param_names_to_flat_param_key.get(
            key.unflat_param_names, key.unflat_param_names
        )
        rekeyed_osd_state[flat_param_key] = param_state

    # Only process param_groups if it exists in sharded_osd
    if "param_groups" in sharded_osd:
        rekeyed_osd_param_groups: List[Dict[str, Any]] = []
        for unflat_param_group in sharded_osd["param_groups"]:
            flat_param_group = copy.deepcopy(unflat_param_group)
            flat_param_keys = sorted(
                {
                    unflat_param_name_to_flat_param_key[unflat_param_name]
                    for unflat_param_name in unflat_param_group["params"]
                }
            )
            flat_param_group["params"] = flat_param_keys
            rekeyed_osd_param_groups.append(flat_param_group)
        return {"state": rekeyed_osd_state, "param_groups": rekeyed_osd_param_groups}
    else:
        return {"state": rekeyed_osd_state}


def _get_param_id_to_param_from_optim_input(
    model: nn.Module,
    optim_input: Optional[
        Union[
            List[Dict[str, Any]],
            Iterable[nn.Parameter],
        ]
    ] = None,
) -> Dict[int, nn.Parameter]:
    """
    Constructs a mapping from parameter IDs to parameters. This may be used
    both for models with ``FlatParameter`` s and without.

    NOTE: This method is only preserved for backward compatibility. The method
    :meth:`_get_param_key_to_param` is the preferred code path that does not
    rely on ``optim_input``.

    NOTE: We critically assume that, whether the optimizer input is a list of
    parameters or a list of parameter groups, :class:`torch.optim.Optimizer`
    enumerates the parameter IDs in order. In other words, for a parameter list
    input, the parameter IDs should be in that list order, and for a parameter
    groups input, the parameter IDs should be in order within each parameter
    group and in order across parameter groups.

    Args:
        model (nn.Module): Model whose parameters are passed into the
            optimizer.
        optim_input (Optional[Union[List[Dict[str, Any]],
        Iterable[nn.Parameter]]]): Input passed into the optimizer
            representing either a :class:`list` of parameter groups or an
            iterable of parameters; if ``None``, then this method assumes the
            input was ``model.parameters()``. (Default: ``None``)

    Returns:
        List[nn.Parameter]: Mapping from parameter IDs to parameters,
        where the parameter ID is implicitly the index in the :class:`list`.
    """
    # Assume the standard case of passing `model.parameters()` to the optimizer
    # if `optim_input` is not specified
    if optim_input is None:
        return dict(enumerate(model.parameters()))
    try:
        params = cast(List[nn.Parameter], list(optim_input))
    except TypeError as e:
        raise TypeError(
            "Optimizer input should be an iterable of Tensors or dicts, "
            f"but got {optim_input}"
        ) from e
    if len(params) == 0:
        raise ValueError("Optimizer input should not be empty")

    # Check if the optimizer input represents tensors or parameter groups
    all_tensors = True
    all_dicts = True
    for param in params:
        all_tensors &= isinstance(param, torch.Tensor)
        all_dicts &= isinstance(param, dict)
    if not all_tensors and not all_dicts:
        raise TypeError("Optimizer input should be an iterable of Tensors or dicts")
    if all_tensors:
        return dict(enumerate(params))
    assert all_dicts
    param_id_to_param: List[nn.Parameter] = []
    for param_group in params:
        has_params_key = "params" in param_group  # type: ignore[operator]
        assert has_params_key, (
            'A parameter group should map "params" to a list of the '
            "parameters in the group"
        )
        for param in param_group["params"]:  # type: ignore[index]
            # Implicitly map `flat_param_id` (current length of the list) to
            # `param`
            param_id_to_param.append(param)
    return dict(enumerate(param_id_to_param))


def _get_flat_param_to_fqn(model: torch.nn.Module) -> Dict[FlatParameter, str]:
    """
    Constructs a mapping from ``FlatParameter`` to a cleaned (devoid of prefixes
    from wrappers) fully qualified name (FQN). Note that this FQN is "non-canonical"
    because ``FlatParameter``  s do not come from the original module but are
    registered only after FSDP has been applied. This function returns the FSDP-given
    name for the ``FlatParameter`` (usually module._flat_param) as opposed to the
    canonical FQNs returned for ``FlatParameter`` s in ``_common_utils._get_param_to_fqns(...)``).

    Consequently, this function will only return a non-empty mapping if FSDP was
    applied with ``use_orig_params=False`` as, otherwise, the original parameters
    are used within the module and there would be no ``FlatParameter`` s in the module.

    """

    def module_fn(module, prefix, tree_level, flat_param_to_fqn):
        for param_name, param in _named_parameters_with_duplicates(
            module, recurse=False
        ):
            if not isinstance(param, FlatParameter):
                continue
            fqn = clean_tensor_name(prefix + param_name)
            flat_param_to_fqn[param] = fqn

    def return_fn(flat_param_to_fqn):
        return flat_param_to_fqn

    flat_param_to_fqn_ret: Dict[FlatParameter, str] = {}
    return _apply_to_modules(
        model,
        module_fn,
        return_fn,
        [fqn for fqn, _ in _named_parameters_with_duplicates(model)],
        flat_param_to_fqn_ret,
    )


def _get_param_key_to_param(
    optim: torch.optim.Optimizer,
    model: Optional[nn.Module] = None,
    is_named_optimizer: bool = False,
    param_to_fqns: Optional[Dict[nn.Parameter, List[str]]] = None,
    flat_param_to_fqn: Optional[Dict[FlatParameter, str]] = None,
) -> Dict[Union[int, str], nn.Parameter]:
    """
    Constructs a mapping from parameter keys to parameters. For the regular
    optimizers, the keys are parameter IDs. For NamedOptimizer, the keys
    are FQNs. This API may be used both for models with ``FlatParameter`` s and
    without.
    """
    clean_fqn_to_curr_fqn: Dict[str, str] = {}
    if is_named_optimizer:
        assert (
            param_to_fqns is not None and flat_param_to_fqn is not None
        ), "The optimizer is a NamedOptimizer, `param_to_fqns` must not be None."
        assert model is not None
        for key, _ in _named_parameters_with_duplicates(model):
            clean_fqn_to_curr_fqn[clean_tensor_name(key)] = key

    param_key_to_param: Dict[Union[str, int], nn.Parameter] = {}
    pid = 0
    for param_group in optim.param_groups:
        if is_named_optimizer:
            for param in param_group["params"]:
                assert flat_param_to_fqn is not None
                if param in flat_param_to_fqn:
                    # FlatParameter case
                    key = flat_param_to_fqn[param]
                else:
                    assert param_to_fqns is not None
                    # use_orig_params case
                    assert len(param_to_fqns[param]) == 1
                    key = param_to_fqns[param][0]
                try:
                    key = clean_fqn_to_curr_fqn[key]
                except KeyError as e:
                    raise KeyError(
                        f"Can't find {key} from {list(clean_fqn_to_curr_fqn.keys())}."
                    ) from e
                param_key_to_param[key] = param
        else:
            for param in param_group["params"]:
                param_key_to_param[pid] = param
                pid += 1

    return param_key_to_param


def _get_param_to_param_key(
    optim: torch.optim.Optimizer,
    model: Optional[nn.Module] = None,
    is_named_optimizer: bool = False,
    param_to_fqns: Optional[Dict[nn.Parameter, List[str]]] = None,
    flat_param_to_fqn: Optional[Dict[FlatParameter, str]] = None,
) -> Dict[nn.Parameter, Union[int, str]]:
    """
    Constructs the inverse mapping of :func:`_get_param_key_to_param`. This API
    only supports the case where `optim` is a regular optimizer, not NamedOptimizer.
    So the parameter keys will be parameter ids.
    """
    param_id_to_param = _get_param_key_to_param(
        optim, model, is_named_optimizer, param_to_fqns, flat_param_to_fqn
    )
    return {param: param_id for param_id, param in param_id_to_param.items()}


def _get_param_to_param_id_from_optim_input(
    model: nn.Module,
    optim_input: Optional[
        Union[
            List[Dict[str, Any]],
            Iterable[nn.Parameter],
        ]
    ] = None,
) -> Dict[nn.Parameter, int]:
    """Constructs the inverse mapping of :func:`_get_param_id_to_param_from_optim_input`."""
    param_id_to_param = _get_param_id_to_param_from_optim_input(model, optim_input)
    return {param: param_id for param_id, param in param_id_to_param.items()}


def _check_missing_keys_on_rank(
    r0_optim_state_keys: List[_OptimStateKey],
    optim_state_key_to_param_key: Dict[_OptimStateKey, Union[str, int]],
    param_key_to_param: Dict[Union[str, int], nn.Parameter],
    group: Optional[dist.ProcessGroup],
) -> None:
    # Ensure that all ranks have at least the optimizer states needed by
    # rank 0's optimizer
    missing_keys: List[_OptimStateKey] = []
    for r0_optim_state_key in r0_optim_state_keys:
        if r0_optim_state_key not in optim_state_key_to_param_key:
            # A parameter from rank 0's optimizer does not exist for this
            # rank's optimizer
            missing_keys.append(r0_optim_state_key)
            continue
        param_key = optim_state_key_to_param_key[r0_optim_state_key]
        if isinstance(param_key, int):
            assert param_key >= 0 and param_key < len(
                param_key_to_param
            ), "Check the `param_key_to_param` construction"
    device = _get_pg_default_device(group)
    num_missing = torch.tensor([len(missing_keys)], dtype=torch.int32, device=device)
    dist.all_reduce(num_missing, group=group)
    if num_missing.item() > 0:
        obj_list = [None for _ in range(dist.get_world_size(group))]
        dist.all_gather_object(obj_list, missing_keys, group=group)
        error_msg = (
            "FSDP currently requires each rank to have at least the "
            "optimizer states needed by rank 0's optimizer but some ranks "
            "are missing some of those states"
        )
        for rank, keys in enumerate(obj_list):
            keys = cast(List[_OptimStateKey], keys)
            if len(keys) > 0:
                error_msg += (
                    f"\nRank {rank} is missing states for the parameters: "
                    f"{[key.unflat_param_names for key in keys]}"
                )
        raise RuntimeError(error_msg)


def _map_param_key_to_optim_keys(
    optim_state_dict: Dict[str, Any],
    group: Optional[dist.ProcessGroup],
    param_key_to_param: Dict[Union[int, str], nn.Parameter],
    param_to_fqns: Dict[nn.Parameter, List[str]],
    fqn_to_fsdp_param_info: Dict[str, FSDPParamInfo],
    merge_keys: bool = False,
) -> Tuple[List[_OptimStateKey], Dict[_OptimStateKey, Union[int, str]]]:
    """
    Construct the local mapping between the ``_OptimStateKey`` and parameter keys
    and all the ``_OptimStateKey`` across ranks. If ``merge_keys`` is False, rank0
    must contain all the ``_OptimStateKey``, an exception will be raised otherwise.
    Note that ``merge_keys`` should equal to ``use_orig_params``.
    """
    rank = dist.get_rank(group)
    optim_state_key_to_param_key: Dict[_OptimStateKey, Union[int, str]] = {}  # local
    all_optim_state_keys: List[_OptimStateKey] = []

    for param_key, param in param_key_to_param.items():
        # Do not include parameters without state to avoid empty mappings
        # just like in normal `torch.optim.Optimizer.state_dict()`
        if param_key not in optim_state_dict["state"]:
            continue
        fqns = param_to_fqns[param]
        is_fsdp_managed = isinstance(param, FlatParameter)
        if is_fsdp_managed:
            assert fqns[0] in fqn_to_fsdp_param_info, (
                fqns[0],
                list(fqn_to_fsdp_param_info.keys()),
            )
        is_fsdp_managed = fqns[0] in fqn_to_fsdp_param_info
        optim_state_key = _OptimStateKey(
            unflat_param_names=tuple(fqns),
            is_fsdp_managed=is_fsdp_managed,
        )
        if rank == 0 or merge_keys:
            all_optim_state_keys.append(optim_state_key)
        optim_state_key_to_param_key[optim_state_key] = param_key

    if merge_keys:
        all_keys: List[List[_OptimStateKey]] = [
            [] for _ in range(dist.get_world_size(group))
        ]
        dist.all_gather_object(all_keys, all_optim_state_keys, group=group)
        merge_all_optim_state_keys = [
            key for local_keys in all_keys for key in local_keys
        ]
        all_optim_state_keys = sorted(set(merge_all_optim_state_keys))
    else:
        key_obj_list: List[Optional[List[_OptimStateKey]]] = (
            [all_optim_state_keys] if rank == 0 else [None]
        )
        dist.broadcast_object_list(key_obj_list, src=0, group=group)
        assert key_obj_list[0] is not None
        all_optim_state_keys = key_obj_list[0]
        _check_missing_keys_on_rank(
            all_optim_state_keys,
            optim_state_key_to_param_key,
            param_key_to_param,
            group,
        )

    return all_optim_state_keys, optim_state_key_to_param_key


def _unflatten_param_groups(
    state_dict: Dict[str, Any],
    param_key_to_param: Dict[Union[int, str], nn.Parameter],
    param_to_fqns: Dict[nn.Parameter, List[str]],
) -> List[Dict[str, Any]]:
    param_groups: List[Dict[str, Any]] = []
    for flat_param_group in state_dict["param_groups"]:
        unflat_param_group = copy.deepcopy(flat_param_group)
        param_group_params = [
            param_key_to_param[flat_param_key]
            for flat_param_key in flat_param_group["params"]
        ]
        nested_unflat_param_names = [
            param_to_fqns[param] for param in param_group_params
        ]
        unflat_param_group["params"] = [
            unflat_param_name
            for unflat_param_names in nested_unflat_param_names
            for unflat_param_name in unflat_param_names
        ]  # flatten the list of lists
        param_groups.append(unflat_param_group)
    return param_groups


def _is_named_optimizer(optim_state_dict: Dict[str, Any]) -> bool:
    """
    Returns whether the state_dict is from a NamedOptimizer.
    This function checks that the keys in the state_dict['state'] are strings
    (which usually are FQNs) versus integers (which usually refer to param_ids
    from a vanilla torch.optim.Optimizer).
    """
    state = optim_state_dict.get("state", None)
    if not state:
        # If we cannot find a state, assume it is not NamedOptimizer as
        # NamedOptimizer has eager initialization.
        return False
    try:
        key = next(iter(state.keys()))
    except Exception as e:
        raise Exception(optim_state_dict) from e
    return isinstance(key, str)


@dataclass
class StateInfo:
    tensors: Dict[str, _PosDimTensorInfo]
    scalar_tensors: Dict[str, torch.Tensor]
    non_tensors: Dict[str, Any]


def _allgather_state_info(
    fsdp_state: _FSDPState,
    input_states: Dict[str, Any],
) -> List[Dict[str, StateInfo]]:
    """
    Given the ``input_states``, allgather StateInfo for each state. The function
    uses all_gather_object to gather StateInfo so no GPU tensors are sent.
    """

    processed_state_dict: Dict[str, StateInfo] = {}
    gathered_state_info: List[Dict[str, StateInfo]] = [
        {} for _ in range(fsdp_state.world_size)
    ]

    for fqn, optim_state in input_states.items():
        # Allgather the scalar tensor state, non-tensor states and tensors metadata.
        processed_state = StateInfo({}, {}, {})
        for state_name, value in sorted_items(optim_state):
            if torch.is_tensor(value):
                if value.dim() == 0:
                    # Ensure that `step` is on CPU.
                    processed_state.scalar_tensors[state_name] = value.cpu()
                else:
                    processed_state.tensors[state_name] = _PosDimTensorInfo(
                        value.shape, value.dtype
                    )
            else:
                processed_state.non_tensors[state_name] = value
        processed_state_dict[fqn] = processed_state
    dist.all_gather_object(
        gathered_state_info,
        processed_state_dict,
        group=fsdp_state.process_group,
    )
    return gathered_state_info


def _unflatten_orig_param_states(
    fsdp_param_info: FSDPParamInfo,
    output_states: Dict[str, Dict[str, Any]],
    shard_state: bool,
    state_name: str,
) -> None:
    """
    Given a output state dict, ``output_states``, which the keys are FQNs to the
    original parameters (not FlatParameters nor parmeter ID), and the values
    are gathered states, unflatten the states to the original dimensions.

    This function performs the unflattening process in-place.
    """
    if dist.get_rank() == 0:
        logger.warning(
            "CUDA Memory Summary before calling to _unflatten_orig_param_states %s",
            torch.cuda.memory_summary(),
        )
    flat_param = fsdp_param_info.handle.flat_param
    fsdp_state = fsdp_param_info.state
    for fqn, gathered_state in output_states.items():
        value = gathered_state[state_name]

        param_idx = fsdp_param_info.param_indices[fqn]
        value = value[: flat_param._numels[param_idx]].reshape(
            flat_param._shapes[param_idx]
        )
        if shard_state:
            if not fsdp_state._optim_state_dict_config.use_dtensor:
                assert fsdp_state.process_group is not None
                value = _ext_chunk_tensor(
                    value,
                    fsdp_state.rank,
                    fsdp_state.world_size,
                    fsdp_state._device_handle.device_count(),
                    fsdp_state.process_group,
                )
            else:
                assert fsdp_state._device_mesh is not None
                value = _ext_chunk_dtensor(
                    value, fsdp_state.rank, fsdp_state._device_mesh
                )
        with SimpleProfiler.profile(SimpleProfiler.Type.D2H):
            value = value.cpu()
        gathered_state[state_name] = value


def _allgather_orig_param_states(
    fsdp_param_info: FSDPParamInfo,
    gathered_state_info: List[Dict[str, StateInfo]],
    input_states: Dict[str, Any],
    shard_state: bool,
    to_save: bool,
) -> Dict[str, Dict[str, Any]]:
    """
    Given the ``gathered_state_info`` and ``input_states``, the API allgather
    all tensor states and restore non-tensor states from ``gathered_state_info``.
    """

    fsdp_state = fsdp_param_info.state
    state_buffers: Dict[str, List[Optional[torch.Tensor]]] = {}
    output_states: Dict[str, Dict[str, Any]] = {fqn: {} for fqn in input_states.keys()}

    # Loop through the all the StateInfos to get the information of GPU
    # tensors and store the local GPU states to ``state_buffers``.
    # Inside the loop, the non-GPU tensors states will be decoded and
    # stored in the ``output_states``, the result state_dict.
    for fqn, gathered_state in output_states.items():
        state_info = [s[fqn] for s in gathered_state_info]
        all_tensor_states = sorted(
            {n for state in state_info for n in state.tensors.keys()}
        )
        empty_ranks: Set[int] = set()
        for state_name in all_tensor_states:
            numels = []
            dtype = torch.float
            _empty_ranks: Set[int] = set()
            for rank, object_state in enumerate(state_info):
                numels.append(0)
                info = object_state.tensors.get(state_name, None)
                if info is not None:
                    numels[-1] = info.shape.numel()
                    dtype = info.dtype
                if numels[-1] == 0:
                    _empty_ranks.add(rank)

            assert not empty_ranks or empty_ranks == _empty_ranks
            empty_ranks = _empty_ranks
            if state_name not in state_buffers:
                state_buffers[state_name] = [None for _ in input_states]
            local_state = input_states[fqn].get(state_name, None)
            state_buffers[state_name][fsdp_param_info.param_indices[fqn]] = local_state

        for rank, object_state in enumerate(state_info):
            if rank in empty_ranks:
                continue
            for state_name, non_tensor_value in object_state.non_tensors.items():
                curr_non_tensor_value = gathered_state.get(state_name, None)
                assert (
                    curr_non_tensor_value is None
                    or curr_non_tensor_value == non_tensor_value
                ), f"Different ranks have different values for {state_name}."
                gathered_state[state_name] = non_tensor_value

            for state_name, scalar_tensor_value in object_state.scalar_tensors.items():
                curr_scalar_tensor_value = gathered_state.get(state_name, None)
                assert curr_scalar_tensor_value is None or torch.equal(
                    scalar_tensor_value, curr_scalar_tensor_value
                ), f"Different ranks have different values for {state_name}."
                gathered_state[state_name] = scalar_tensor_value

    # Loop through the ``state_buffers`` and construct the flattened, concatenated,
    # sharded states. The size of the constructed state will be the same size as
    # flat_param (also sharded).
    # Then perform an allgather_into_tensor to get the full flat_param state. The
    # full flat_param state is the result of concatenation of multiple states in
    # the order of of flat_param._fqns.
    # We then split the flat_param state into multiple ones and return the result.
    flat_param = fsdp_param_info.handle.flat_param
    empty_func = functools.partial(
        torch.empty, dtype=dtype, device=fsdp_state.compute_device
    )
    gathered_tensor = empty_func(flat_param._padded_unsharded_size)
    # Synchronize can be slow but this will be easier for us to debug.
    torch.cuda.synchronize()
    for state_name, buffers in state_buffers.items():
        begin_idx = end_idx = -1
        for idx, buffer in enumerate(buffers):
            if buffer is not None:
                begin_idx = idx if begin_idx == -1 else begin_idx
                end_idx = idx

        local_buffers: List[torch.Tensor] = []
        if begin_idx != -1:
            for tensor in buffers[begin_idx : end_idx + 1]:
                assert tensor is not None
                local_buffers.append(tensor)
        shard_numel_padded = flat_param._sharded_size.numel() - (
            sum(t.numel() for t in local_buffers)
        )
        if shard_numel_padded > 0:
            local_buffers.append(empty_func(shard_numel_padded))
        local_shard = torch.cat(local_buffers)
        assert local_shard.numel() * fsdp_state.world_size == gathered_tensor.numel()
        with SimpleProfiler.profile(SimpleProfiler.Type.ALLGATHER):
            dist.all_gather_into_tensor(
                gathered_tensor, local_shard, group=fsdp_state.process_group
            )
            # Synchronize can be slow but this will be easier for us to debug.
            torch.cuda.synchronize()
        gathered_tensor = gathered_tensor[: flat_param._unpadded_unsharded_size.numel()]
        start = 0
        paddings = zip(flat_param._is_padding_mask, flat_param._numels_with_padding)
        for fqn, idx in fsdp_param_info.param_indices.items():
            gathered_state = output_states[fqn]
            is_padding, numel = next(paddings)
            while is_padding:
                start += numel

            gathered_state[state_name] = gathered_tensor[start : start + numel]
            start += numel

        if to_save:
            _unflatten_orig_param_states(
                fsdp_param_info,
                output_states,
                shard_state,
                state_name,
            )
    del gathered_tensor

    return output_states


def _gather_all_orig_param_state(
    fsdp_param_info: FSDPParamInfo,
    input_states: Dict[str, Any],
    shard_state: bool,
    to_save: bool,
) -> Dict[str, Any]:
    """
    Given a optimizer state dict, ``input_states``, which the keys are FQNs to the
    original parameters (not FlatParameters nor parmeter ID), gather all the
    states and unflatten them to the original dimensions. Note that all the
    params refered by the ``input_states`` must be manageded by FSDP.
    """
    fsdp_state = fsdp_param_info.state
    if (
        fsdp_state.world_size == 1
        or fsdp_state.sharding_strategy == ShardingStrategy.NO_SHARD
    ):
        return input_states if to_save else {}

    with SimpleProfiler.profile(SimpleProfiler.Type.RESHARDING):
        with SimpleProfiler.profile(SimpleProfiler.Type.ALLGATHER_OBJ):
            gathered_state_info = _allgather_state_info(fsdp_state, input_states)
        output_states = _allgather_orig_param_states(
            fsdp_param_info, gathered_state_info, input_states, shard_state, to_save
        )
    if to_save:
        assert set(output_states.keys()) == set(fsdp_param_info.param_indices.keys())
        return output_states
    else:
        return {}


def _convert_state_with_orig_params(
    all_optim_state_keys: List[_OptimStateKey],
    optim_state_key_to_param_key: Dict[_OptimStateKey, Union[int, str]],
    fqn_to_fsdp_param_info: Dict[str, FSDPParamInfo],
    optim_state_dict: Dict[Union[str, int], Any],
    to_save: bool,
    shard_state: bool,
) -> Dict[str, Any]:
    fsdp_osd_state: Dict[str, Any] = {}
    all_states: Dict[int, Dict[str, Any]] = {}
    # Iterate in rank 0's flat parameter ID order to ensure aligned all-gathers
    # across ranks
    for optim_state_key in all_optim_state_keys:
        param_key: Union[str, int, None] = optim_state_key_to_param_key.get(
            optim_state_key, None
        )

        if param_key is None and not optim_state_key.is_fsdp_managed:
            continue

        if optim_state_key.is_fsdp_managed:
            fqn = optim_state_key.unflat_param_names[0]
            fsdp_param_info = fqn_to_fsdp_param_info[fqn]
            state = {} if param_key is None else optim_state_dict[param_key]
            if id(fsdp_param_info) not in all_states:
                all_states[id(fsdp_param_info)] = {}
            all_states[id(fsdp_param_info)][fqn] = state

        elif to_save:
            assert len(optim_state_key.unflat_param_names) == 1
            unflat_param_name = optim_state_key.unflat_param_names[0]
            with SimpleProfiler.profile("none_fsdp_managed_copy"):
                param_key = cast(Union[str, int], param_key)
                fsdp_osd_state[unflat_param_name] = copy.copy(
                    optim_state_dict[param_key]
                )
                for state_name, value in sorted_items(
                    fsdp_osd_state[unflat_param_name]
                ):
                    if torch.is_tensor(value):
                        fsdp_osd_state[unflat_param_name][state_name] = value.cpu()

    # Instead of gathering the state of each parameter individually, we perform
    # the gathering  all at once to speed up the process.
    for _all_states in all_states.values():
        fqn = next(iter(_all_states.keys()))
        fsdp_param_info = fqn_to_fsdp_param_info[fqn]
        assert set(fsdp_param_info.param_indices.keys()) == set(_all_states.keys())
        fsdp_osd_state.update(
            _gather_all_orig_param_state(
                fsdp_param_info,
                _all_states,
                shard_state,
                to_save,
            )
        )

    return fsdp_osd_state


def _convert_state_with_flat_params(
    all_optim_state_keys: List[_OptimStateKey],
    optim_state_key_to_param_key: Dict[_OptimStateKey, Union[int, str]],
    fqn_to_fsdp_param_info: Dict[str, FSDPParamInfo],
    optim_state_dict: Dict[Union[str, int], Any],
    to_save: bool,
    shard_state: bool,
) -> Dict[str, Any]:
    fsdp_osd_state: Dict[str, Any] = {}
    # Iterate in rank 0's flat parameter ID order to ensure aligned all-gathers
    # across ranks
    for optim_state_key in all_optim_state_keys:
        param_key: Union[str, int, None] = optim_state_key_to_param_key.get(
            optim_state_key, None
        )

        assert param_key is not None, (
            "If use_orig_params is False, we must be able to find the "
            f"corresponding param id. {optim_state_key} {param_key}"
        )

        if optim_state_key.is_fsdp_managed:
            # If there are multiple unflat_param_names (not use_orig_params),
            # they share the same FSDPParamInfo. So the first unflat_param_name
            # is sufficient to fetch the FSDPParamInfo.
            fqn = optim_state_key.unflat_param_names[0]
            fsdp_param_info = fqn_to_fsdp_param_info[fqn]
            unflat_state = _unflatten_optim_state(
                fsdp_param_info,
                optim_state_dict[param_key],
                to_save,
                shard_state,
            )
            if to_save:
                assert len(unflat_state) == len(optim_state_key.unflat_param_names)
                for unflat_param_name, unflat_param_state in zip(
                    optim_state_key.unflat_param_names,
                    unflat_state,
                ):
                    fsdp_osd_state[unflat_param_name] = unflat_param_state
        elif to_save:
            assert len(optim_state_key.unflat_param_names) == 1
            unflat_param_name = optim_state_key.unflat_param_names[0]
            fsdp_osd_state[unflat_param_name] = copy.copy(optim_state_dict[param_key])
            for state_name, value in sorted_items(fsdp_osd_state[unflat_param_name]):
                if torch.is_tensor(value):
                    fsdp_osd_state[unflat_param_name][state_name] = value.cpu()

    return fsdp_osd_state


@torch.no_grad()
def _optim_state_dict(
    model: nn.Module,
    optim: torch.optim.Optimizer,
    optim_state_dict: Dict[str, Any],
    optim_input: Optional[
        Union[
            List[Dict[str, Any]],
            Iterable[nn.Parameter],
        ]
    ],
    rank0_only: bool,
    shard_state: bool,
    group: Optional[dist.ProcessGroup],
    using_optim_input: bool,
    use_orig_params: bool = False,
) -> Dict[str, Any]:
    """
    Consolidates the optimizer state and returns it as a :class:`dict`
    following the convention of :meth:`torch.optim.Optimizer.state_dict`,
    i.e. with keys ``"state"`` and ``"param_groups"``.
    The flat parameters in ``FSDP`` modules contained in ``model`` are mapped
    back to their unflattened parameters.

    Parameter keys are not well-defined. For a regular optimizer, the optimizer
    state_dict contains a mapping from parameter IDs to parameter states.
    Parameter IDs are the order of parameters in ``optim.param_groups()`` across
    all the groups. This API also allows user to pass ``optim_input`` for the
    mapping between parameters and parameter IDs. Using ``optim_input`` is being
    deprecated.

    If the optimizer is a ``NamedOptimizer``, the optimizer state_dict does not
    contain parameter IDs mapping but a mapping from parameter FQNs to parameter
    states. This API finds the mapping from FQNs to parameters if the optimizer
    is a ``NamedOptimizer``.

    If ``use_orig_params`` is True, each rank will have all FSDP-managed
    parameters but some of these parameters may be empty due to the sharding.
    For a regular optim.Optimizer, states for those empty parameters will
    not be initialized. So, when aggregating the FQNs across ranks, no assert
    will be raised on a rank even if it does not have all the states -- it is
    valid and FSDP knows how to aggregate them. However, FSDP has to ignore
    handling those parameters that are not managed by FSDP and do not exist on
    the local rank -- those are managed by other parallelisms and FSDP does not
    know how to handle/aggregate them.

    Args:
        model (nn.Module): Root module (which may or may not be a
            :class:`FullyShardedDataParallel` instance) whose parameters
            were passed into the optimizer ``optim``.
        optim (torch.optim.Optimizer): Optimizer for ``model`` 's
            parameters.
        rank0_only (bool): If ``True``, saves the populated :class:`dict`
            only on rank 0; if ``False``, saves it on all ranks. (Default:
            ``True``)
        shard_state (bool): If ``True``, shard and distribute all
            non-zero-dimension states.

    Returns:
        Dict[str, Any]: A :class:`dict` containing the optimizer state for
        ``model`` 's original unflattened parameters and including keys
        "state" and "param_groups" following the convention of
        :meth:`torch.optim.Optimizer.state_dict`. If ``rank0_only=False``,
        then nonzero ranks return an empty :class:`dict`.
    """
    SimpleProfiler.reset()
    cm = ExitStack()
    cm.enter_context(SimpleProfiler.profile(SimpleProfiler.Type.ALL))
    _reset_flat_param_grad_info_if_needed(traversal_utils._get_fsdp_handles(model))
    to_save = not rank0_only or dist.get_rank(group) == 0 or shard_state

    with SimpleProfiler.profile("preprocessing"):
        param_to_fqns = _get_param_to_fqns(model)
        flat_param_to_fqn = _get_flat_param_to_fqn(model)
        is_named_optimizer = _is_named_optimizer(optim_state_dict)

        param_key_to_param = cast(
            Dict[Union[int, str], nn.Parameter],
            (
                _get_param_id_to_param_from_optim_input(model, optim_input)
                if using_optim_input
                else _get_param_key_to_param(
                    optim, model, is_named_optimizer, param_to_fqns, flat_param_to_fqn
                )
            ),
        )
        fqn_to_fsdp_param_info = _get_fqn_to_fsdp_param_info(model)

    with SimpleProfiler.profile("preprocessing_with_comm"):
        (
            all_optim_state_keys,
            optim_state_key_to_param_key,
        ) = _map_param_key_to_optim_keys(
            optim_state_dict,
            group,
            param_key_to_param,
            param_to_fqns,
            fqn_to_fsdp_param_info,
            merge_keys=use_orig_params,
        )

    with SimpleProfiler.profile("state_converting"):
        convert_fn = (
            _convert_state_with_orig_params
            if use_orig_params
            else _convert_state_with_flat_params
        )
        fsdp_osd_state = convert_fn(
            all_optim_state_keys,
            optim_state_key_to_param_key,
            fqn_to_fsdp_param_info,
            optim_state_dict["state"],
            to_save,
            shard_state,
        )

    # At this point, communication is complete and ranks can return early if nothing
    # will be saved on that rank.
    if not to_save:
        return {}

    fsdp_osd: Dict[str, Any] = {"state": fsdp_osd_state}

    flat_param_fqns = set(flat_param_to_fqn.values())
    for key, value in optim_state_dict["state"].items():
        if key in fsdp_osd_state:
            continue
        if key in flat_param_fqns:
            continue
        if key in param_key_to_param:
            continue
        # This key is not recognized by FSDP. It may be a user-defined state
        # or some parameters state that FSDP is unable to map from
        # ``optim.param_groups``.
        warnings.warn(
            f"Found a optim state, {key}, that FSDP cannot process. FSDP "
            "will directly copy everything to the returned state_dict. In "
            "most cases, this is a user-defined state that is not "
            "associated with any particular parameter. Another possible "
            "case is this state is managed by TorchRec. Otherwise, there may "
            " be a mismatched assumption of optim_state_dict of this mode."
        )
        fsdp_osd_state[key] = value

    if "param_groups" in optim_state_dict:
        fsdp_osd["param_groups"] = _unflatten_param_groups(
            optim_state_dict, param_key_to_param, param_to_fqns
        )

    cm.close()
    SimpleProfiler.dump_and_reset("FSDP _optim_state_dict() profiling: ")

    return fsdp_osd


def _get_fqn_to_fsdp_param_info(model: nn.Module) -> Dict[str, FSDPParamInfo]:
    """
    Construct the mapping from a param's fqn to its corresponding ``FSDPParamInfo``
    if the param is managed by FSDP. Shared parameters, or original parameters that
    are shared across multiple nn.Modules, are required to belong to one and only
    one FSDP instance and thus correspond to one ``FlatParameter``. Within the one
    ``FlatParameter``, ``FlatParameter._fqns`` only stores the first FQN of a shared
    parameter. Thus, the keys in the mapping are guaranteed to map to unique parameters.
    """

    def module_fn(module, prefix, tree_level, fqn_to_param_info):
        fsdp_state = _get_module_fsdp_state_if_fully_sharded_module(module)
        if fsdp_state is None:
            return
        _lazy_init(fsdp_state, module)
        handle = _module_handle(fsdp_state, module)
        if not handle:
            return
        flat_param = handle.flat_param
        fsdp_param_info = FSDPParamInfo(fsdp_state, handle, {})
        # NOTE: `idx` indexes into the data structures *without* padding
        # elements
        for idx, local_fqn in enumerate(flat_param._fqns):
            fqn = clean_tensor_name(prefix + local_fqn)
            if fqn in fqn_to_param_info:
                assert fqn_to_param_info[fqn].handle.flat_param is flat_param, fqn
            fqn_to_param_info[fqn] = fsdp_param_info
            fsdp_param_info.param_indices[fqn] = idx

    def return_fn(fqn_to_param_info):
        return fqn_to_param_info

    fqn_to_param_info: Dict[str, FSDPParamInfo] = {}
    # FlatParameter._fqns stores the local fqn, starting from the root of the
    # FSDP. Using _apply_to_modules() with model (may not be the FSDP root
    # module) allows us to construct the global fqn.
    return _apply_to_modules(
        model,
        module_fn,
        return_fn,
        [fqn for fqn, _ in _named_parameters_with_duplicates(model)],
        fqn_to_param_info,
    )
