import inspect
import math
import threading
from abc import ABC, abstractmethod
from enum import Enum
from functools import partial
from typing import Any, Callable, Dict, List, Tuple

import torch
import torch.distributed.rpc as rpc
from colossalai.pipeline.pipeline_process_group import ppg
from colossalai.pipeline.rpc.utils import (get_batch_lengths, pytree_filter, pytree_map,
                                           split_batch, tensor_shape_list, type_detail)
from torch import autograd, nn, optim
from torch._C._distributed_rpc import PyRRef
from torch.futures import Future


class Phase(Enum):
    FORWARD = 0
    BACKWARD = 1
    UPDATE = 2
    INPUT = 3

class UniqueKey:
    __slots__ = ('microbatch_id', 'phase')
    microbatch_id: int
    phase: Phase

    def __init__(self, microbatch_id, phase) -> None:
        self.microbatch_id = microbatch_id
        self.phase = phase

    def __eq__(self, __o: object) -> bool:
        return (self.microbatch_id == __o.microbatch_id) and (self.phase == __o.phase)

    def __hash__(self) -> int:
        return tuple.__hash__((self.microbatch_id, self.phase))

    def __repr__(self) -> str:
        return f'Key(microbatch_id={self.microbatch_id}, phase={self.phase})'


class WorkItem:
    __slots__ = ('stage_id', 'phase', 'args', 'kwargs', 'output', 'refcount', 'microbatch_id', 'batch_id',
                 'num_microbatches', 'forward_only')

    stage_id: int
    phase: Phase
    args: Tuple[Any]
    kwargs: Dict[str, Any]
    output: Future
    microbatch_id: int
    refcount: int
    batch_id: int
    num_microbatches: int
    forward_only: bool

    def __init__(self,
                 stage_id,
                 phase,
                 args,
                 kwargs,
                 output,
                 microbatch_id,
                 batch_id,
                 num_microbatches,
                 forward_only,
                 refcount=0) -> None:
        for attr_name in self.__slots__:
            setattr(self, attr_name, locals()[attr_name])


class BackwardCache:
    __slots__ = ('checkpoint', 'stage_input_args', 'stage_input_kwargs', 'stage_outputs')
    checkpoint: bool
    stage_input_args: Tuple[Any]
    stage_input_kwargs: Dict[Any, Any]
    stage_outputs: Tuple[Any]

    def __init__(self,
                 stage_input_args: Tuple[Any],
                 stage_input_kwargs: Dict[Any, Any] = None,
                 stage_outputs: Tuple[Any] = None,
                 checkpoint: bool = False) -> None:
        for arg_name in self.__slots__:
            setattr(self, arg_name, locals()[arg_name])


class WorkerBase(ABC):

    def __init__(self,
                 partition_fn: Callable,
                 partition_args: tuple,
                 pp_rank: int,
                 actual_stage_num: int,
                 num_microbatches: int,
                 device: str,
                 criterion: Callable = None,
                 metric: Callable = None,
                 checkpoint: bool = False,
                 data_process_func: Callable = None) -> None:
        super().__init__()

        self.pp_rank = pp_rank
        self.actual_stage_num = actual_stage_num
        self.num_microbatches = num_microbatches
        self.checkpoint = checkpoint

        if data_process_func is not None:
            self.data_process_func = partial(data_process_func, pp_rank)

        self.device = device
        self._initialize_outstanding_range()

        # variable and const for context managment
        self.outstanding = 0
        self.forward_times = 0
        self.backward_times = 0
        self.reset_key = UniqueKey(0, Phase.FORWARD)

        # rref of other workers
        self.pp_rank_to_worker_rref: Dict[int, PyRRef] = None

        # lock for the list
        self._initialize_lock()

        # topology info
        self.producer_stage_ids: List[int] = None
        self.consumer_stage_ids: List[int] = None
        self.input_consumer_stage_ids: List[int] = None

        # module partitions
        self.partition_fn = partition_fn
        self.partition_args = partition_args
        self.criterion = criterion
        self.metric = metric

        # middleware info
        self._is_input = False
        self._is_output = False
        self._producer_consumer_initialized = False

        # context to maintain loop
        self._initialize_context_container()

        # main loop
        self.main_loop_thread = threading.Thread(target=self._work_loop, name=f'rank_{pp_rank}', daemon=True)
        self.main_loop_thread.start()

    def _get_future_by_device(self):
        return torch.futures.Future(devices=None if self.device in (None, 'cpu') else [self.device])

    def _initialize_outstanding_range(self):
        outstanding_range = None
        if self.pp_rank == self.actual_stage_num - 1:
            outstanding_range = (0, 1)
        else:
            outstanding_range = (self.actual_stage_num, self.actual_stage_num)
        self.outstanding_range = outstanding_range

    def _initialize_context_container(self):
        self.microbatch_id_to_backward_cache: Dict[int, BackwardCache] = dict()
        self.microbatch_id_to_labels: Dict[int, Any] = dict()
        self.work_list: Dict[UniqueKey, WorkItem] = dict()
        self.output_list: Dict[UniqueKey, WorkItem] = dict()

    def _initialize_lock(self):
        self.partition_condition_lock = threading.Condition(threading.Lock())
        self.work_list_condition_lock = threading.Condition(threading.Lock())
        self.output_list_condition_lock = threading.Condition(threading.Lock())
        self.label_lock = threading.Condition(threading.Lock())
        self.producer_consumer_init_lock = threading.Condition(threading.Lock())

    def _initialize_partition(self):
        partition_fn = self.partition_fn
        partition_args = self.partition_args
        device = self.device
        with self.partition_condition_lock:
            self.module_partition: nn.Module = partition_fn(*partition_args).to(device)
            self.partition_condition_lock.notify_all()

    def sync_global_worker_rrefs(self, pp_rank_to_worker_rref: Dict[int, PyRRef]) -> None:
        assert self.pp_rank_to_worker_rref is None, f"in rank {self.pp_rank}, worker has sync global workers rrefs"
        assert pp_rank_to_worker_rref is not None, "stage_to_workers must be a dict instead of None"
        self.pp_rank_to_worker_rref = pp_rank_to_worker_rref

        # for some schedule need the other worker's info to initialise partition (like Chimera)
        # construction of partition is executed after the registion of pp_rank_to_worker_rref
        self._initialize_partition()

    def get_output_by_key(self, key: UniqueKey, recv_rank=None) -> Any:
        with self.output_list_condition_lock:
            self.output_list_condition_lock.wait_for(lambda: key in self.output_list)
            output_work_item = self.output_list[key]

        output = output_work_item.output
        if isinstance(output, Future):
            output = output.wait()

        # output_work_item.refcount += 1

        # TODO(jiangziyue) redesign lifecycle management for DAG scheduler
        # all consumers have been satisfied, the work_item can be released
        with self.output_list_condition_lock:
            if output_work_item.refcount >= len(self.consumer_stage_ids):
                self.output_list.pop(key)
        return output

    def get_parameters(self) -> List[torch.Tensor]:
        return [p for p in self.module_partition.parameters()]

    def get_parameter_gradients(self) -> List[torch.Tensor]:
        return [p.grad for p in self.module_partition.parameters()]

    def get_partition(self):
        with self.partition_condition_lock:
            self.partition_condition_lock.wait_for(lambda: hasattr(self, 'module_partition'))
            return self.module_partition

    def get_partition_state_dict(self):
        with self.partition_condition_lock:
            self.partition_condition_lock.wait_for(lambda: hasattr(self, 'module_partition'))
            return self.module_partition.state_dict()

    def _make_args_kwargs(self, microbatch, merge=False):
        if isinstance(microbatch, dict):
            if merge:
                return list(microbatch.values()), {}
            return [], microbatch
        elif isinstance(microbatch, torch.Tensor):
            return [microbatch], {}
        elif isinstance(microbatch, (tuple, list)):
            args = []
            kwargs = {}
            for arg in microbatch:
                if isinstance(arg, dict):
                    kwargs.update(arg)
                else:
                    args.append(arg)
            if merge:
                arg_lst = args
                for arg in kwargs.values():
                    arg_lst.append(arg)
                return arg_lst, {}
            return args, kwargs
        else:
            raise TypeError(f"Input batch can be only dict, list, tuple or tensor, but receive {type(microbatch)}")

    # just for first pp_rank
    # TODO(jiangziyue) Consider whether this function should be protected by Lock in DAG env.
    # TODO(jiangziyue) Define a Class for DAG.
    def set_input(self, microbatch_id: int, microbatch: Tuple[Any], forward_only: bool):
        assert self.consumer_stage_ids is not None
        key = UniqueKey(microbatch_id, Phase.FORWARD)
        output = self._get_future_by_device()
        
        if not self.use_middleware():
            # make args and kwargs
            args, kwargs = self._make_args_kwargs(microbatch)

            work_item = WorkItem(self.pp_rank, Phase.FORWARD, args, kwargs, output, microbatch_id, None,
                                self.num_microbatches, forward_only)
            with self.work_list_condition_lock:
                self.work_list[key] = work_item
                self.work_list_condition_lock.notify_all()
        else:
            # make args and kwargs
            arg_lst, _ = self._make_args_kwargs(microbatch, merge=True)

            # first stage assign correct input into other stages
            DAG = self.get_DAG()
            DAG_node = DAG['input_partition']
            self_input_offsets = []
            recv_input_key = UniqueKey(microbatch_id, Phase.INPUT)
            # notify rank which should receive extra input
            offset = 0
            for details in DAG_node.values():
                for partition_name in details['output'].keys():
                    recv_rank = self.partition_name_to_pp_rank(partition_name)
                    if recv_rank == self.pp_rank:
                        self_input_offsets.append(offset)
                    elif recv_rank not in self.input_consumer_stage_ids:
                        self.input_consumer_stage_ids.append(recv_rank)
                offset += 1

            # set input for self rank
            self_arg_lst = []
            for off in self_input_offsets:
                self_arg_lst.append(arg_lst[off])

            work_item = WorkItem(self.pp_rank, Phase.FORWARD, self_arg_lst, {}, output, microbatch_id, None,
                                self.num_microbatches, forward_only)
            with self.work_list_condition_lock:
                self.work_list[key] = work_item
                self.work_list_condition_lock.notify_all()

            # put input tensor which other nodes need into output_list
            work_item_remote = WorkItem(self.pp_rank, Phase.INPUT, [], {}, arg_lst, microbatch_id, None,
                                self.num_microbatches, forward_only)

            with self.output_list_condition_lock:
                self.output_list[recv_input_key] = work_item_remote
                self.output_list_condition_lock.notify_all()

    # just for last pp_rank
    def set_labels(self, microbatch_id: int, microlabels: Any):
        with self.label_lock:
            self.microbatch_id_to_labels[microbatch_id] = microlabels
            self.label_lock.notify_all()

    # just for last pp_rank
    def _begin_backward(self, microbatch_id: int):
        with self.work_list_condition_lock:
            assert self.producer_stage_ids is not None

            key = UniqueKey(microbatch_id, Phase.BACKWARD)
            output = self._get_future_by_device()
            grad_wrt_loss = None

            work_item = WorkItem(self.pp_rank, Phase.BACKWARD, grad_wrt_loss, {}, output, microbatch_id, None,
                                 self.num_microbatches, False)

            self.work_list[key] = work_item
            self.work_list_condition_lock.notify_all()

    # TODO(jiangziyue) Consider whether this function should be protected by Lock in DAG env.
    def subscribe_producer(self, microbatch_id: int, forward_only: bool):
        """
        You should call this function asynchronously
        """
        stage_id = self.pp_rank
        output = self._get_future_by_device()
        if not self.use_middleware():
            producer_num = len(self.producer_stage_ids)
            subscribe_forward_futures: List[Future] = [None] * producer_num
            for i in range(producer_num):
                producer_stage_id = self.producer_stage_ids[i]
                producer_output_key = UniqueKey(microbatch_id, Phase.FORWARD)
                producer_worker_rref = self.pp_rank_to_worker_rref[producer_stage_id]
                subscribe_forward_futures[i] = producer_worker_rref.rpc_async().get_output_by_key(producer_output_key)
        else:
            with self.work_list_condition_lock:
                key = UniqueKey(microbatch_id, Phase.FORWARD)
                if key in self.work_list:
                    return

            producer_stage_ids = []
            with self.producer_consumer_init_lock:
                self.producer_consumer_init_lock.wait_for(lambda: self._producer_consumer_initialized)
                producer_stage_ids = self.producer_stage_ids
            producer_num = len(producer_stage_ids)
            
            # TODO(jiangziyue) get single value instead of the whole output
            if self.need_model_input():
                producer_num += 1 # extra one(the last one) for input_tensor
            subscribe_forward_futures: List[Future] = [None] * producer_num

            # TODO(jiangziyue) get single value instead of the whole output
            if self.need_model_input():
                producer_stage_id = 0
                producer_output_key = UniqueKey(microbatch_id, Phase.INPUT)
                producer_worker_rref = self.pp_rank_to_worker_rref[producer_stage_id]
                subscribe_forward_futures[0] = producer_worker_rref.rpc_async().get_output_by_key(producer_output_key, self.pp_rank)

                for i in range(0, producer_num-1):
                    producer_stage_id = producer_stage_ids[i]
                    producer_output_key = UniqueKey(microbatch_id, Phase.FORWARD)
                    producer_worker_rref = self.pp_rank_to_worker_rref[producer_stage_id]
                    subscribe_forward_futures[i+1] = producer_worker_rref.rpc_async().get_output_by_key(producer_output_key, self.pp_rank)

            else:
                for i in range(producer_num):
                    producer_stage_id = producer_stage_ids[i]
                    producer_output_key = UniqueKey(microbatch_id, Phase.FORWARD)
                    producer_worker_rref = self.pp_rank_to_worker_rref[producer_stage_id]
                    #producer_partition_name = self.pp_rank_to_partition_name[producer_stage_id]
                    subscribe_forward_futures[i] = producer_worker_rref.rpc_async().get_output_by_key(producer_output_key, self.pp_rank)

        work_item_from_producer = WorkItem(stage_id, Phase.FORWARD, subscribe_forward_futures, {}, output,
                                        microbatch_id, None, self.num_microbatches, forward_only)

        # add work_item to work_list
        with self.work_list_condition_lock:
            key = UniqueKey(microbatch_id, Phase.FORWARD)
            if key not in self.work_list:
                self.work_list[key] = work_item_from_producer
                self.work_list_condition_lock.notify_all()

    def subscribe_consumer(self, microbatch_id: int):
        """
        You should call this function asynchronously
        """
        assert self.producer_stage_ids is not None
        consumer_num = len(self.consumer_stage_ids)
        assert consumer_num > 0, "only stage that has consumers can subscribe comsumers"

        stage_id = self.pp_rank
        subscribe_backward_futures: List[Future] = [None] * consumer_num
        output = self._get_future_by_device()

        for i in range(consumer_num):
            consumer_stage_id = self.consumer_stage_ids[i]
            consumer_output_key = UniqueKey(microbatch_id, Phase.BACKWARD)
            consumer_worker_rref = self.pp_rank_to_worker_rref[consumer_stage_id]
            subscribe_backward_futures[i] = consumer_worker_rref.rpc_async().get_output_by_key(consumer_output_key)

        # flatten args
        work_item_from_consumer = WorkItem(stage_id, Phase.BACKWARD, subscribe_backward_futures, {}, output,
                                           microbatch_id, None, self.num_microbatches, False)

        # add work_item to work_list
        with self.work_list_condition_lock:
            key = UniqueKey(microbatch_id, Phase.BACKWARD)
            assert key not in self.work_list
            self.work_list[key] = work_item_from_consumer
            self.work_list_condition_lock.notify_all()

    def _get_producer_consumer(self) -> None:
        rank = self.pp_rank
        assert self.producer_stage_ids is None, f"all the producers of rank {rank} has been subscribed"
        assert self.consumer_stage_ids is None, f"all the consumers of rank {rank} has been subscribed"

        # should be aranged in order, the order of the input of current forward
        self.producer_stage_ids = []
        self.consumer_stage_ids = []

        if not self.use_middleware():
            # Just for demo
            prev_rank = rank - 1
            next_rank = rank + 1
            if prev_rank >= 0:
                self.producer_stage_ids.append(prev_rank)
            if next_rank <= self.actual_stage_num - 1:
                self.consumer_stage_ids.append(next_rank)
        else:
            self.input_consumer_stage_ids = []
            DAG = self.get_DAG()
            DAG_node_name = self.pp_rank_to_partition_name(rank)
            DAG_node = DAG[DAG_node_name]
            for partition_name in DAG_node['input'].keys():
                if partition_name == 'MODEL_INPUT':
                    self._is_input = True
                else:
                    prev_rank = self.partition_name_to_pp_rank(partition_name)
                    self.producer_stage_ids.append(prev_rank)

            for partition_name in DAG_node['output'].keys():
                if partition_name == 'MODEL_OUTPUT':
                    self._is_output = True
                else:
                    next_rank = self.partition_name_to_pp_rank(partition_name)
                    self.consumer_stage_ids.append(next_rank)
                    
            # TODO(jiangziyue) Consider whether this function should be protected by Lock in DAG env.
            with self.producer_consumer_init_lock:
                self._producer_consumer_initialized = True
                self.producer_consumer_init_lock.notify_all()

    # TODO(jiangziyue) Define a Class for DAG.
    def pp_rank_to_partition_name(self, pp_rank: int):
        prefix = 'submod_'
        partition_name = prefix + str(pp_rank)
        return partition_name

    # TODO(jiangziyue) Define a Class for DAG.
    def partition_name_to_pp_rank(self, partition_name: str) -> int:
        prefix = 'submod_'
        pp_rank = int(partition_name.split(prefix)[-1])
        return pp_rank
    
    def get_DAG(self):
        with self.partition_condition_lock:
            self.partition_condition_lock.wait_for(lambda: hasattr(self, 'module_partition'))
            if hasattr(self.module_partition, '_DAG'):
                return self.module_partition._DAG
            else:
                return None
    
    def use_middleware(self):
        DAG = self.get_DAG()
        return DAG is not None

    # TODO(jiangziyue) get single value instead of the whole output
    def _get_real_args_kwargs(self, args_or_kwargs):
        if not self.use_middleware():
            args_or_kwargs = pytree_map(args_or_kwargs, fn=lambda x: x.wait(), process_types=Future)
            if args_or_kwargs is not None:
                if isinstance(args_or_kwargs, dict):
                    pass
                else:
                    flatten_args = []
                    pytree_map(args_or_kwargs, fn=lambda x: flatten_args.append(x), map_all=True)
                    args_or_kwargs = flatten_args
        else:
            args_or_kwargs = pytree_map(args_or_kwargs, fn=lambda x: x.wait(), process_types=Future)
            if args_or_kwargs is not None:
                if isinstance(args_or_kwargs, dict):
                    pass
                else:
                    flatten_args = []     
                    if self.is_first_stage():
                        pytree_map(args_or_kwargs, fn=lambda x: flatten_args.append(x), map_all=True)
                    # TODO get by offset
                    else:
                        DAG = self.get_DAG()
                        producer_outputs = {}
                        cur_DAG_node_name = self.pp_rank_to_partition_name(self.pp_rank)
                        #cur_DAG_node = DAG[self.pp_rank_to_partition_name(self.pp_rank)]
                        for i, args_from_one_mod in enumerate(args_or_kwargs):
                            producer_output_offsets = []
                            if self.need_model_input():
                                if i == 0:
                                    producer_DAG_node = DAG['input_partition']
                                    producer_partition_name = 'MODEL_INPUT'
                                    offset = 0
                                    for arg_info in producer_DAG_node.values():
                                        if cur_DAG_node_name in arg_info['output']:
                                            producer_output_offsets.append(offset)
                                        offset += 1
                                else:
                                    producer_rank = self.producer_stage_ids[i-1]
                                    producer_partition_name = self.pp_rank_to_partition_name(producer_rank)
                                    producer_DAG_node = DAG[producer_partition_name]
                                    producer_output_offsets = producer_DAG_node['output'][cur_DAG_node_name]

                            else:
                                producer_rank = self.producer_stage_ids[i]
                                producer_partition_name = self.pp_rank_to_partition_name(producer_rank)
                                producer_DAG_node = DAG[producer_partition_name]
                                producer_output_offsets = producer_DAG_node['output'][cur_DAG_node_name]

                            if producer_partition_name != 'MODEL_INPUT' and DAG[producer_partition_name]['output_len'] == 1:
                                producer_outputs[producer_partition_name] = [args_from_one_mod]
                            else:
                                producer_outputs[producer_partition_name] = [args_from_one_mod[offset] for offset in producer_output_offsets]

                        cur_DAG_node_input = DAG[cur_DAG_node_name]['input']

                        def get_input_len(DAG_node_input):
                            res = 0
                            for offsets in DAG_node_input.values():
                                res += len(offsets)
                            return res

                        input_len = get_input_len(cur_DAG_node_input)
                        flatten_args = [None] * input_len
                        for producer_partition_name, args_input_offsets in cur_DAG_node_input.items():
                            for i, args_input_offset in enumerate(args_input_offsets):
                                flatten_args[args_input_offset] = producer_outputs[producer_partition_name][i]

                    args_or_kwargs = flatten_args
        return args_or_kwargs

    @abstractmethod
    def _get_work_item_key(self) -> UniqueKey:
        """
            this method control the order of the microbatch to consume
        """

    def is_first_stage(self):
        return self.pp_rank == 0

    def is_last_stage(self):
        return self.pp_rank == self.actual_stage_num - 1
    
    def need_model_input(self):
        return not self.is_first_stage() and self._is_input

    def _default_data_process_func(self, args_kwargs):
        if self.is_first_stage():
            args = args_kwargs[0]
            kwargs = args_kwargs[1]
        else:
            args = args_kwargs
            kwargs = {}

        return args, kwargs

    def _consume_work_item_by_phase(self, work_item: WorkItem):
        phase = work_item.phase
        args = work_item.args
        kwargs = work_item.kwargs
        microbatch_id = work_item.microbatch_id
        forward_only = work_item.forward_only
        data_process_func = getattr(self, 'data_process_func', self._default_data_process_func)
        consume_result = None

        is_first_stage = self.is_first_stage()
        is_last_stage = self.is_last_stage()

        if phase == Phase.FORWARD:
            # remind its consumer to get data before forward
            if not is_last_stage:
                for stage_id in self.consumer_stage_ids:
                    consumer_worker_rref = self.pp_rank_to_worker_rref[stage_id]
                    consumer_worker_rref.remote().subscribe_producer(microbatch_id, forward_only)

            # sustain pipeline context
            self.forward_times += 1
            if not forward_only:
                self.outstanding += 1

            # parse and integrate args and kwargs
            if is_first_stage:
                args = self._get_real_args_kwargs(args)
                kwargs = self._get_real_args_kwargs(kwargs)
                args_kwargs = (args, kwargs)
            else:
                args_kwargs = self._get_real_args_kwargs(args)

            args, kwargs = data_process_func(args_kwargs)

            stage_outputs = None
            stage_input_args = args
            stage_input_kwargs = kwargs
            use_checkpoint = None

            if forward_only:
                with torch.no_grad():
                    consume_result = self.module_partition(*args, **kwargs)

                if is_last_stage and self.criterion:
                    with self.label_lock:
                        self.label_lock.wait_for(lambda: microbatch_id in self.microbatch_id_to_labels)
                    labels = self.microbatch_id_to_labels.pop(microbatch_id)
                    loss: torch.Tensor = self.criterion(consume_result, labels)
                    if self.metric is not None:
                        metric_result = self.metric(consume_result, labels)
                        if isinstance(metric_result, torch.Tensor):
                            metric_result = metric_result.item()
                    else:
                        metric_result = None
                    consume_result = [loss.item(), metric_result]

                # last stage doesn't need to do checkpoint, for it will do backward instantly
                stage_input_args = None
                stage_input_kwargs = None
                stage_outputs = consume_result

            elif self.checkpoint and not is_last_stage:
                with torch.no_grad():
                    consume_result = self.module_partition(*args, **kwargs)

                stage_outputs = consume_result
                use_checkpoint = True

            else:
                consume_result = self.module_partition(*args, **kwargs)

                if is_last_stage and self.criterion:
                    with self.label_lock:
                        self.label_lock.wait_for(lambda: microbatch_id in self.microbatch_id_to_labels)
                    labels = self.microbatch_id_to_labels.pop(microbatch_id)
                    loss: torch.Tensor = self.criterion(consume_result, labels)
                    if self.metric is not None:
                        metric_result = self.metric(consume_result, labels)
                        if isinstance(metric_result, torch.Tensor):
                            metric_result = metric_result.item()
                    else:
                        metric_result = None

                    consume_result = [loss.item(), metric_result]
                else:
                    loss = consume_result

                stage_outputs = loss
                use_checkpoint = False

            if not forward_only:
                self.microbatch_id_to_backward_cache[microbatch_id] = BackwardCache(stage_input_args,
                                                                                    stage_input_kwargs,
                                                                                    stage_outputs,
                                                                                    checkpoint=use_checkpoint)
            # if not forward_only, do the backward
            if not forward_only:
                if is_last_stage:    # if it is the last stage, trigger backward automatic
                    self._begin_backward(microbatch_id)

        elif phase == Phase.BACKWARD:
            # remind its producer to get data before backward
            if not is_first_stage:
                for stage_id in self.producer_stage_ids:
                    producer_worker_rref = self.pp_rank_to_worker_rref[stage_id]
                    producer_worker_rref.remote().subscribe_consumer(microbatch_id)
            self.backward_times += 1
            self.outstanding -= 1

            assert microbatch_id in self.microbatch_id_to_backward_cache, f"microbatch_id {microbatch_id} not in backward cache"
            backward_cache = self.microbatch_id_to_backward_cache.pop(microbatch_id)

            stage_outputs = backward_cache.stage_outputs
            stage_input_args = backward_cache.stage_input_args
            stage_input_kwargs = backward_cache.stage_input_kwargs
            use_checkpoint = backward_cache.checkpoint

            if use_checkpoint:
                stage_outputs = [self.module_partition(*stage_input_args, **stage_input_kwargs)]

            # overlap recompute and future.wait
            if not is_last_stage:
                grad_tensors = self._get_real_args_kwargs(args)
            else:
                grad_tensors = None

            # take tensor only (for only tensor can do backward)
            stage_outputs = pytree_filter(lambda x: x.requires_grad, stage_outputs, process_types=torch.Tensor)
            grad_tensors = pytree_filter(lambda x: x is not None, grad_tensors, process_types=torch.Tensor)

            autograd.backward(stage_outputs, grad_tensors=grad_tensors)

            # collect grad of input tensor
            consume_result = []
            if not is_first_stage:
                pytree_map(stage_input_args, lambda x: consume_result.append(x.grad), process_types=torch.Tensor)
                pytree_map(stage_input_kwargs, lambda x: consume_result.append(x.grad), process_types=torch.Tensor)

        else:
            raise TypeError(f"Unknown phase appears in _consume_work_item_by_phase {phase}")

        return consume_result

    def _get_store_len(self):
        return f'work_list:{len(self.work_list)} output_list:{len(self.output_list)} backward_cache:{len(self.microbatch_id_to_backward_cache)} label_cache:{len(self.microbatch_id_to_labels)}'

    def _get_parameter_grad_sum(self):
        grad_sum = 0
        for p in self.module_partition.parameters():
            if p.grad is not None:
                grad_sum += p.grad.sum()
        return grad_sum

    def _is_first_step(self, work_item: WorkItem) -> bool:
        return work_item.phase == Phase.FORWARD and work_item.microbatch_id == 0

    def _is_last_step(self, work_item: WorkItem) -> bool:
        if work_item.forward_only:
            last_phase = Phase.FORWARD
        else:
            last_phase = Phase.BACKWARD
        is_last_phase = work_item.phase == last_phase
        is_last_microbatch = work_item.microbatch_id == self.num_microbatches - 1
        return is_last_phase and is_last_microbatch

    def _hook_before_step(self):
        pass

    def _reset_context(self):
        self.forward_times = 0
        self.backward_times = 0
        self.outstanding = 0
        self._initialize_outstanding_range()

    # do the main loop to consume ready_list
    def _work_loop(self):
        # for init
        self._get_producer_consumer()
        torch.cuda.set_device(ppg.get_local_pp_rank())

        # main loop
        while True:
            work_item_key = self._get_work_item_key()

            # move current work item to output_list to activate subscribe in advance
            with self.work_list_condition_lock:
                work_item = self.work_list.pop(work_item_key)

            with self.output_list_condition_lock:
                # assert work_item_key not in self.output_list
                self.output_list[work_item_key] = work_item
                self.output_list_condition_lock.notify_all()

            consume_result = self._consume_work_item_by_phase(work_item)

            work_item.output.set_result(consume_result)

            # if is last step in one batch reset context and do step
            if self._is_last_step(work_item):
                self._hook_before_step()
                if hasattr(self, 'optimizer') and not work_item.forward_only:
                    self.step()
                self._reset_context()

    def initialize_optimizer(self, optimizer_class: type, **kwargs):
        # TODO(jiangziyue) it's temporary code to deal with empty module partition.
        # After tracer fixed, remove this part.
        if len(list(self.module_partition.parameters())) > 0:
            self.optimizer: optim.Optimizer = optimizer_class(self.module_partition.parameters(), **kwargs)
        self.step_lock = threading.Lock()
        self.step_lock.acquire()

    def wait_for_step(self):
        self.step_lock.acquire()

    def step(self):
        # TODO(jiangziyue) it's temporary code to deal with empty module partition.
        # After tracer fixed, remove this part.
        if len(list(self.module_partition.parameters())) > 0:
            self.optimizer.step()
            self.optimizer.zero_grad()
        self.step_lock.release()


class PipelineEngineBase(ABC, nn.Module):

    def __init__(self,
                 worker_type,
                 partition_fn: Callable,
                 stage_num,
                 num_microbatches,
                 device: str,
                 use_1F1B=False,
                 chunk: int = 1,
                 criterion: Callable = None,
                 metric: Callable = None,
                 checkpoint: bool = False,
                 data_process_func: Callable = None) -> None:
        super().__init__()
        self.worker_type = worker_type
        self.partition_fn: Callable = partition_fn
        self.chunk = chunk
        self.criterion = criterion
        self.metric = metric
        self.num_microbatches = num_microbatches
        self.device = device
        self.use_1F1B = use_1F1B
        self.stage_num = stage_num
        self.checkpoint = checkpoint
        self.data_process_func = data_process_func

        self.pp_rank_to_worker_rref: Dict[int, PyRRef] = dict()

        self.step_futs: List[Future] = []

        self._check_argument()
        self._create_pp_rank_to_rpc_worker_id()
        self._create_pp_rank_to_module_partition_id()
        self._init_worker()

    def _check_argument(self) -> None:
        # make virtual stage num
        self.virtual_stage_num = self.stage_num * self.chunk
        assert self.stage_num <= torch.cuda.device_count(), "stage_num must be smaller than device count!"

        # check data_process_func
        data_process_func = self.data_process_func
        if data_process_func is not None:
            assert callable(data_process_func), "data_process_func must be a function"
            assert '<locals>' not in data_process_func.__repr__(), "data_process_func must be a global function"
            assert '<lambda>' not in data_process_func.__repr__(), "data_process_func cannot be a lambda expression"
            sig = inspect.signature(data_process_func)
            assert len(
                sig.parameters
            ) == 2, f"length of data_process_func' arguments must be 2, receive {len(sig.parameters)} arguments instead"

    def _get_actual_stage_num(self) -> int:
        return self.stage_num if self.chunk == 1 else self.virtual_stage_num

    def _create_pp_rank_to_rpc_worker_id(self) -> None:
        """create a map from model partition to stage_id, which is useful when use_interleave is True.
        e.g. If a model is splited into 4 parts, which means stage_num is 2, chunk is 2, then 
        pp_rank_to_rpc_worker_id = [0, 1, 0, 1], that means first and third part
        of partitions will be moved to device 0 and the others to device 1
        """
        stage_num = self.stage_num
        actual_stage_num = self._get_actual_stage_num()
        self.pp_rank_to_rpc_worker_id = [0] * actual_stage_num
        for pp_rank in range(actual_stage_num):
            self.pp_rank_to_rpc_worker_id[pp_rank] = pp_rank % stage_num

    def _create_pp_rank_to_module_partition_id(self) -> None:
        """By default(both fill drain and 1F1B), length of model partitions equal to
        actual_stage_num, so allocate model partition to corresponding stage
        """
        actual_stage_num = self._get_actual_stage_num()
        self.pp_rank_to_module_partition_id = [0] * actual_stage_num
        for pp_rank in range(actual_stage_num):
            self.pp_rank_to_module_partition_id[pp_rank] = pp_rank

    def _init_worker(self) -> None:
        actual_stage_num = self._get_actual_stage_num()

        worker_type = self.worker_type
        checkpoint = self.checkpoint
        num_microbatches = self.num_microbatches
        device = self.device
        criterion = self.criterion
        metric = self.metric
        partition_fn = self.partition_fn
        chunk = self.chunk
        data_process_func = self.data_process_func

        for pp_rank in range(len(self.pp_rank_to_rpc_worker_id)):
            partition_id = self.pp_rank_to_module_partition_id[pp_rank]
            partition_args = (partition_id, chunk, actual_stage_num)
            rpc_worker_id = self.pp_rank_to_rpc_worker_id[pp_rank]
            if device[:4] == 'cuda':
                device = f'cuda:{rpc_worker_id}'
            self.pp_rank_to_worker_rref[pp_rank] = rpc.remote(rpc_worker_id,
                                                              worker_type,
                                                              args=(partition_fn, partition_args, pp_rank,
                                                                    actual_stage_num, num_microbatches, device,
                                                                    criterion, metric, checkpoint, data_process_func))

        # let each worker know global worker rref (include itself)
        sync_futs = []
        for pp_rank in self.pp_rank_to_worker_rref:
            fut = self.pp_rank_to_worker_rref[pp_rank].rpc_async().sync_global_worker_rrefs(self.pp_rank_to_worker_rref)
            sync_futs.append(fut)

        for fut in sync_futs:
            fut.wait()

    def remote_parameters(self) -> Dict[int, List[torch.Tensor]]:
        parameters = {}
        actual_stage_num = self._get_actual_stage_num()
        for stage_id in range(actual_stage_num):
            parameters[stage_id] = []
            worker_rref = self.pp_rank_to_worker_rref[stage_id]
            for p in worker_rref.rpc_sync().get_parameters():
                parameters[stage_id].append(p)
        return parameters

    def remote_grad(self) -> Dict[int, List[torch.Tensor]]:
        grads = {}
        actual_stage_num = self._get_actual_stage_num()
        for stage_id in range(actual_stage_num):
            grads[stage_id] = []
            worker_rref = self.pp_rank_to_worker_rref[stage_id]
            for grad in worker_rref.rpc_sync().get_parameter_gradients():
                grads[stage_id].append(grad)
        return grads

    def get_input_pp_ranks(self) -> List[int]:
        return [0]

    def get_output_pp_ranks(self) -> List[int]:
        return [self._get_actual_stage_num() - 1]

    def _consume_constraint(self, microbatch_id: int, forward_only: bool, input_pp_ranks: List[int],
                            output_pp_ranks: List[int], ret_future):
        actual_stage_num = self._get_actual_stage_num()
        use_1F1B = self.use_1F1B
        if microbatch_id >= actual_stage_num:
            if forward_only or not use_1F1B:
                for pp_rank in output_pp_ranks:
                    ret_future[pp_rank][microbatch_id - actual_stage_num].wait()
            else:
                key = UniqueKey(microbatch_id - actual_stage_num, Phase.BACKWARD)
                for pp_rank in input_pp_ranks:
                    worker_rref = self.pp_rank_to_worker_rref[pp_rank]
                    worker_rref.rpc_sync().get_output_by_key(key)

    def _create_ret_future(self, output_pp_ranks: List[int]) -> Dict[int, List[Future]]:
        num_microbatches = self.num_microbatches
        return {pp_rank: [None] * num_microbatches for pp_rank in output_pp_ranks}

    def _set_input(self, input_pp_ranks: List[int], microbatch_id: int, microbatch, forward_only: bool):
        for pp_rank in input_pp_ranks:
            worker_rref = self.pp_rank_to_worker_rref[pp_rank]
            # TODO : add relationship between input_pp_ranks and parts of microbatch
            worker_rref.remote().set_input(microbatch_id, microbatch, forward_only)

    def _set_labels(self, output_pp_ranks: List[int], microbatch_id: int, microlabels):
        for pp_rank in output_pp_ranks:
            worker_rref = self.pp_rank_to_worker_rref[pp_rank]
            # TODO : add relationship between output_pp_ranks and parts of microlabels
            worker_rref.remote().set_labels(microbatch_id, microlabels)

    def _subscribe_forward(self, microbatch_id: int, output_pp_ranks: List[int], ret_future: Dict[int, List[Future]]):
        key = UniqueKey(microbatch_id, Phase.FORWARD)
        for pp_rank in output_pp_ranks:
            worker_rref = self.pp_rank_to_worker_rref[pp_rank]
            ret_future[pp_rank][microbatch_id] = worker_rref.rpc_async().get_output_by_key(key)

    def _ensure_backward(self, forward_only: bool, input_pp_ranks: List[int]):
        if not forward_only:
            for pp_rank in input_pp_ranks:
                worker_rref = self.pp_rank_to_worker_rref[pp_rank]
                key = UniqueKey(self.num_microbatches - 1, Phase.BACKWARD)
                worker_rref.rpc_sync().get_output_by_key(key)

    def _collect_forward_result(self, output_pp_ranks: List[int], ret_future: Dict[int, List[Future]]):
        forward_result = []
        for pp_rank in output_pp_ranks:
            worker_forward_result = [None] * self.num_microbatches
            for microbatch_id in range(self.num_microbatches):
                ret = ret_future[pp_rank][microbatch_id].wait()
                # TODO : more stable format
                ret = [ret] if isinstance(ret, torch.Tensor) else ret
                worker_forward_result[microbatch_id] = ret

            worker_forward_result = list(zip(*worker_forward_result))
            forward_result.extend(worker_forward_result)

        return forward_result

    def forward_backward(self, batch: torch.Tensor, labels: torch.Tensor = None, forward_only: bool = False):
        batch_lengths = get_batch_lengths(batch)
        batch_length = batch_lengths[0]

        if labels is not None and not forward_only:
            assert hasattr(
                self, 'optimizer_class'), "call `initialize_optimizer` to initialize optimizer before forward_backward"

        num_microbatches = self.num_microbatches

        assert batch_length >= num_microbatches, "num_microbatches is greater than the size of a batch, which is illegal"
        microbatch_size = math.ceil(batch_length / num_microbatches)
        device = self.device

        # If Chimera mode is used, then rank of down pipeline is excluded from 'input_pp_ranks' or 'output_pp_ranks'
        input_pp_ranks = self.get_input_pp_ranks()
        output_pp_ranks = self.get_output_pp_ranks()

        # a cache to collect data and control flow
        ret_future = self._create_ret_future(output_pp_ranks)

        for microbatch_id in range(num_microbatches):
            # control data input  speed
            # to prevent exceed of wait limitations
            self._consume_constraint(microbatch_id, forward_only, input_pp_ranks, output_pp_ranks, ret_future)
            batch_start = microbatch_size * microbatch_id
            batch_end = min(batch_start + microbatch_size, batch_length)

            # set input
            microbatch = split_batch(batch, batch_start, batch_end, device)
            self._set_input(input_pp_ranks, microbatch_id, microbatch, forward_only)

            # set labels
            if labels is not None:
                # microlabels = labels[microbatch_size * microbatch_id:microbatch_size * (microbatch_id + 1)]
                microlabels = split_batch(labels, batch_start, batch_end, device)
                self._set_labels(output_pp_ranks, microbatch_id, microlabels)

            # get data asynchronously
            self._subscribe_forward(microbatch_id, output_pp_ranks, ret_future)

        # wait for first rank to ensure all backwards are done
        self._ensure_backward(forward_only, input_pp_ranks)

        # collect forward result
        forward_result = self._collect_forward_result(output_pp_ranks, ret_future)

        if not forward_only and hasattr(self, 'optimizer_class'):
            # wait for all step
            for pp_rank in self.pp_rank_to_worker_rref:
                worker_rref = self.pp_rank_to_worker_rref[pp_rank]
                worker_rref.rpc_sync().wait_for_step()

        return forward_result

    def initialize_optimizer(self, optimizer_class: type, **kwargs):
        self.optimizer_class = optimizer_class
        for pp_rank in self.pp_rank_to_worker_rref:
            worker_rref = self.pp_rank_to_worker_rref[pp_rank]
            worker_rref.remote().initialize_optimizer(optimizer_class, **kwargs)

    def step(self):
        actual_stage_num = self._get_actual_stage_num()
        for pp_rank in range(actual_stage_num):
            worker_rref = self.pp_rank_to_worker_rref[pp_rank]
            fut = worker_rref.rpc_async().step()
            self.step_futs.append(fut)

        for fut in self.step_futs:
            fut.wait()
