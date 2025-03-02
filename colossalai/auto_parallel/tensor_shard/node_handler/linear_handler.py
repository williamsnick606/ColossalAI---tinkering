from typing import Dict, List, Union

import torch
import torch.nn.functional as F

from colossalai.auto_parallel.tensor_shard.utils import transpose_partition_dim, update_partition_dim
from colossalai.logging import get_dist_logger
from colossalai.tensor.sharding_spec import ShardingNotDivisibleError

from ..sharding_strategy import OperationData, OperationDataType, ShardingStrategy
from .node_handler import ModuleHandler, NodeHandler
from .registry import operator_registry
from .strategy import LinearProjectionStrategyGenerator, StrategyGenerator

__all__ = ['LinearModuleHandler', 'LinearFunctionHandler']


def _update_sharding_spec_for_transposed_weight_for_linear(strategy: ShardingStrategy,
                                                           weight_name: str) -> ShardingStrategy:
    """
    This function is a helper function used by both module node handler and function node handler. This function will
    convert the sharding spec for the transposed weight to the correct partititon spec.

    Args:
        strategy (ShardingStrategy): the strategy generated by the strategy generator.
        weight_name (str): the name of the OperationData object for the weight.
    """
    # switch the dimensions of the transposed weight
    sharding_spec = strategy.get_sharding_spec_by_name(weight_name)
    op_data = strategy.get_op_data_by_name(weight_name)
    assert op_data.logical_shape != op_data.data.shape, \
        "Expected the logical and physical shape of the linear operator's weight to be different, but found them to be the same"
    dim_size = len(op_data.logical_shape)
    transpose_partition_dim(sharding_spec, 0, dim_size - 1)
    return strategy


def _convert_logical_sharding_to_physical_sharding_spec_for_linear(strategy: ShardingStrategy, input_name: str,
                                                                   output_name: str) -> List[ShardingStrategy]:
    """
    This function converts the logical sharding spec to the physical sharding spec for both the input and output of the linear operation. The input and output
    should have the same sharding spec.

    Args:
        strategy (ShardingStrategy): the logical strategy generated by the strategy generator.
        input_name (str): the name of the OperationData object for the input.
        output_name (str): the name of the OperationData object for the output.


    """
    # the result will be a list of strategies
    sharding_strategies = []

    # get operation data
    input_op_data = strategy.get_op_data_by_name(input_name)
    output_op_data = strategy.get_op_data_by_name(output_name)
    input_sharding_spec = strategy.get_sharding_spec_by_name(input_op_data.name)
    output_sharding_spec = strategy.get_sharding_spec_by_name(output_op_data.name)

    # recover the last logical dimension to physical dimension
    last_logical_input_dims = len(input_op_data.logical_shape) - 1
    last_logical_output_dims = len(output_op_data.logical_shape) - 1
    last_physical_input_dims = input_op_data.data.dim() - 1
    last_physical_output_dims = output_op_data.data.dim() - 1

    if last_logical_input_dims in input_sharding_spec.dim_partition_dict:
        update_partition_dim(
            sharding_spec=input_sharding_spec,
            dim_mapping={last_logical_input_dims: last_physical_input_dims},
            physical_shape=input_op_data.data.shape,
            inplace=True,
        )

    if last_logical_output_dims in output_sharding_spec.dim_partition_dict:
        update_partition_dim(
            sharding_spec=output_sharding_spec,
            dim_mapping={last_logical_output_dims: last_physical_output_dims},
            physical_shape=output_op_data.data.shape,
            inplace=True,
        )

    # get logger for debug message
    logger = get_dist_logger()

    # for the input of the linear operation, it can be multi-dimensional. The sharding spec generated is only
    # 2D, where the first dimension is non-matrix dimension and the last dimension is the matrix dimension.
    # the logical non-matrix dimension can belong to the 0th to (N-1)th dimension of the physical input shape.
    # Thus, we enumerate to get all possible cases.
    if 0 in input_sharding_spec.dim_partition_dict:
        # if 0 is in the dim_partition_dict, it means that the
        # the generated sharding strategy does shard the non-matrix dimension,
        # in this case, we need to do enumeration
        num_input_dims = input_op_data.data.dim()
        for i in range(num_input_dims - 1):
            strategy_copy = strategy.clone()
            input_sharding_spec = strategy_copy.get_sharding_spec_by_name(input_op_data.name)
            output_sharding_spec = strategy_copy.get_sharding_spec_by_name(output_op_data.name)
            try:
                # replace the 0th dimension in the logical sharding with ith dimension in the physical sharding
                update_partition_dim(sharding_spec=input_sharding_spec,
                                     dim_mapping={0: i},
                                     physical_shape=input_op_data.data.shape,
                                     inplace=True)
                update_partition_dim(sharding_spec=output_sharding_spec,
                                     dim_mapping={0: i},
                                     physical_shape=output_op_data.data.shape,
                                     inplace=True)
                strategy_copy.name = f'{strategy.name}_{i}'
                sharding_strategies.append(strategy_copy)
            except ShardingNotDivisibleError as e:
                logger.debug(
                    f'Errored occurred when converting the logical sharding spec to the physical one. Error details: {e}'
                )
    else:
        # the generated sharding strategy does not shard the non-matrix dimension,
        # in this case, we don't need to do enumeration
        # but instead, we still need to convert the logical shape to physical shape
        strategy_copy = strategy.clone()
        input_sharding_spec = strategy_copy.get_sharding_spec_by_name(input_op_data.name)
        output_sharding_spec = strategy_copy.get_sharding_spec_by_name(output_op_data.name)

        # after updating, the logical shape will be replaced by the physical shape
        update_partition_dim(sharding_spec=input_sharding_spec,
                             dim_mapping={},
                             physical_shape=input_op_data.data.shape,
                             inplace=True)
        update_partition_dim(sharding_spec=output_sharding_spec,
                             dim_mapping={},
                             physical_shape=output_op_data.data.shape,
                             inplace=True)
        sharding_strategies.append(strategy_copy)
    return sharding_strategies


@operator_registry.register(torch.nn.Linear)
class LinearModuleHandler(ModuleHandler):
    """
    A LinearModuleHandler which deals with the sharding strategies for nn.Linear module.
    """

    def get_strategy_generator(self) -> List[StrategyGenerator]:
        op_data_mapping = self.get_operation_data_mapping()
        generators = []
        generators.append(
            LinearProjectionStrategyGenerator(op_data_mapping, self.device_mesh, linear_projection_type='linear'))
        return generators

    def get_operation_data_mapping(self) -> Dict[str, OperationData]:
        # use transposed shape for strategies
        # the strategies will be transformed back to its original shape in self.post_process
        input_meta_data = self.node.args[0]._meta_data
        input_logical_shape = input_meta_data.view(-1, input_meta_data.shape[-1]).shape
        physical_input_operand = OperationData(name=str(self.node.args[0]),
                                               type=OperationDataType.ARG,
                                               data=input_meta_data,
                                               logical_shape=input_logical_shape)
        physical_other_operand = OperationData(name="weight",
                                               type=OperationDataType.PARAM,
                                               data=self.named_parameters['weight'],
                                               logical_shape=self.named_parameters['weight'].shape[::-1])
        output_meta_data = self.node._meta_data
        output_logical_shape = output_meta_data.view(-1, output_meta_data.shape[-1]).shape
        physical_output = OperationData(name=str(self.node),
                                        type=OperationDataType.OUTPUT,
                                        data=output_meta_data,
                                        logical_shape=output_logical_shape)

        mapping = {"input": physical_input_operand, "other": physical_other_operand, "output": physical_output}

        if 'bias' in self.named_parameters is not None:
            physical_bias_operand = OperationData(name="bias",
                                                  type=OperationDataType.PARAM,
                                                  data=self.named_parameters['bias'])
            mapping['bias'] = physical_bias_operand
        return mapping

    def post_process(self, strategy: ShardingStrategy) -> Union[ShardingStrategy, List[ShardingStrategy]]:
        """
        Convert the sharding spec from the logical shape to the physical shape. In this function, two tasks are completed:
        1. the sharding spec is updated for the transposed weight
        2. the input and output sharding specs are updated to physical shape.
        """
        # switch the dimensions of the transposed weight
        strategy = _update_sharding_spec_for_transposed_weight_for_linear(strategy=strategy, weight_name='weight')

        # create multiple sharding strategies for the inputs
        # as input can be multi-dimensinal and the partition dim is only 2D,
        # we need to map the partition at dim 0 to one of the first few dimensions of the input
        strategies = _convert_logical_sharding_to_physical_sharding_spec_for_linear(strategy=strategy,
                                                                                    input_name=str(self.node.args[0]),
                                                                                    output_name=str(self.node))
        return strategies


@operator_registry.register(F.linear)
class LinearFunctionHandler(NodeHandler):
    """
    A LinearFunctionHandler which deals with the sharding strategies for F.Linear.
    """

    def get_strategy_generator(self) -> List[StrategyGenerator]:
        op_data_mapping = self.get_operation_data_mapping()
        generators = []
        generators.append(
            LinearProjectionStrategyGenerator(op_data_mapping, self.device_mesh, linear_projection_type='linear'))
        return generators

    def get_operation_data_mapping(self) -> Dict[str, OperationData]:
        # use transposed shape for strategies
        # the strategies will be transformed back to its original shape in self.post_process
        input_meta_data = self.node.args[0]._meta_data
        input_logical_shape = input_meta_data.view(-1, input_meta_data.shape[-1]).shape
        physical_input_operand = OperationData(name=str(self.node.args[0]),
                                               type=OperationDataType.ARG,
                                               data=self.node.args[0]._meta_data,
                                               logical_shape=input_logical_shape)

        # check if the other operand is a parameter
        if isinstance(self.node.args[1]._meta_data, torch.nn.parameter.Parameter):
            data_type = OperationDataType.PARAM
        else:
            data_type = OperationDataType.ARG

        physical_other_operand = OperationData(name=str(self.node.args[1]),
                                               type=data_type,
                                               data=self.node.args[1]._meta_data,
                                               logical_shape=self.node.args[1]._meta_data.shape[::-1])
        output_meta_data = self.node._meta_data
        output_logical_shape = output_meta_data.view(-1, output_meta_data.shape[-1]).shape
        physical_output = OperationData(
            name=str(self.node),
            type=OperationDataType.OUTPUT,
            data=self.node._meta_data,
            logical_shape=output_logical_shape,
        )

        mapping = {"input": physical_input_operand, "other": physical_other_operand, "output": physical_output}

        if 'bias' in self.node.kwargs and self.node.kwargs['bias'] is not None:
            # check if the other operand is a parameter
            if isinstance(self.node.kwargs["bias"]._meta_data, torch.nn.parameter.Parameter):
                data_type = OperationDataType.PARAM
            else:
                data_type = OperationDataType.ARG
            physical_bias_operand = OperationData(name=str(self.node.kwargs["bias"]),
                                                  type=data_type,
                                                  data=self.node.kwargs["bias"]._meta_data)
            mapping['bias'] = physical_bias_operand

        return mapping

    def post_process(self, strategy: ShardingStrategy):
        # switch the dimensions of the transposed weight
        strategy = _update_sharding_spec_for_transposed_weight_for_linear(strategy=strategy,
                                                                          weight_name=str(self.node.args[1]))
        # create multiple sharding strategies for the inputs
        # as input can be multi-dimensinal and the partition dim is only 2D,
        # we need to map the partition at dim 0 to one of the first few dimensions of the input
        strategies = _convert_logical_sharding_to_physical_sharding_spec_for_linear(strategy=strategy,
                                                                                    input_name=str(self.node.args[0]),
                                                                                    output_name=str(self.node))
        return strategies
