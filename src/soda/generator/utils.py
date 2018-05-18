from collections import deque, namedtuple
from fractions import Fraction
from functools import reduce
import copy
import json
import logging
import math
import operator
import os
import sys

from soda.generator.dataflow import *
import soda.grammar

# constants
coords_tiled = 'xyzw'
coords_in_tile = 'ijkl'
coords_in_orig = 'pqrs'
type_width = {'uint8_t':8, 'uint16_t':16, 'uint32_t':32, 'uint64_t':64, 'int8_t':8, 'int16_t':16, 'int32_t':32, 'int64_t':64, 'float':32, 'double':64}
max_dram_bank = 4

_logger = logging.getLogger('__main__').getChild(__name__)

class InternalError(Exception):
    pass

class SemanticError(Exception):
    pass

class SemanticWarn(Exception):
    pass

# Tensor.name: str
# Tensor.type: str
# Tensor.chan: int
# Tensor.idx: (int, ...)
# Tensor.parent: Stage
# Tensor.children: {Stage, ...}
# Tensor.offset: int
# Tensor.border: ('preserve', {Stage, ...})
class Tensor(object):
    def __init__(self, node):
        self.name = node.name
        self.type = node.type
        self.chan = node.chan
        if isinstance(node, soda.grammar.Output):
            self.idx = next(iter(node.expr)).idx
            for e in node.expr:
                if e.idx != self.idx:
                    raise InternalError('Normalization went wrong')
        elif isinstance(node, soda.grammar.Local):
            self.idx = next(iter(node.expr)).idx
            for e in node.expr:
                if e.idx != self.idx:
                    raise InternalError('Normalization went wrong')
        else:
            self.idx = None
        self.parent = None
        self.children = set()
        self.offset = 0
        self.border = None

    def __str__(self):
        return '%s(%s)' % (type(self).__name__, ', '.join('%s = %s' % (k, v) for k, v in self.__dict__.items()))

    def PreserveBorderTo(self):
        return self.border[1] if self.border is not None and self.border[0] == 'preserve' else None

    def PreserveBorderFrom(self):
        return self.parent is not None and self.parent.PreserveBorderFrom()

    def IsOutput(self):
        return len(self.children)==0

    def is_output(self):
        return len(self.children)==0

    def IsInput(self):
        return self.parent is None or self.parent.IsInput()

# Stage.window: {str: [(int, ...), ...], ...}
# Stage.offset: {str: [int, ...], ...}
# Stage.delay: {str: int, ...}
# Stage.expr: [StageExpr, ...]
# Stage.inputs: {str: Tensor, ...}
# Stage.output: Tensor
# Stage.border: ('preserve', Tensor)
class Stage(object):
    def __init__(self, **kwargs):
        self.window = kwargs.pop('window')
        self.offset = kwargs.pop('offset')
        self.delay = kwargs.pop('delay', {})
        self.expr = kwargs.pop('expr')
        self.inputs = kwargs.pop('inputs')
        self.output = kwargs.pop('output')
        self.border = None

        # shortcuts
        self.name = self.output.name
        self.idx = self.output.idx

    def __str__(self):
        return '%s(%s)' % (type(self).__name__, ', '.join('%s = %s' % (k, v) for k, v in self.__dict__.items()))

    def PreserveBorderFrom(self):
        return self.border[1] if self.border is not None and self.border[0] == 'preserve' else None

    def PreserveBorderTo(self):
        return self.output.PreserveBorderFrom()

    def IsOutput(self):
        return self.output.IsOutput()

    def IsInput(self):
        return len(self.inputs)==0

class Stencil(object):
    def __init__(self, **kwargs):
        self.iterate = kwargs.pop('iterate')
        if self.iterate < 1:
            raise SemanticError('cannot iterate %d times' % self.iterate)
        self.border = kwargs.pop('border')
        self.preserve_border = self.border == 'preserve'
        self.cluster = kwargs.pop('cluster')
        # platform determined
        self.burst_width = kwargs.pop('burst_width')
        self.dram_bank = kwargs.pop('dram_bank')
        # application determined
        self.app_name = kwargs.pop('app_name')
        # parameters can be explored
        self.tile_size = kwargs.pop('tile_size')
        self.unroll_factor = kwargs.pop('unroll_factor')
        self.replication_factor = kwargs.pop('replication_factor')
        self.dram_separate = kwargs.pop('dram_separate')
        if self.dram_separate:
            if self.dram_bank%2 != 0:
                logging.getLogger(__name__).fatal('Number of DRAM banks has to be even when separated')
                sys.exit(-1)
            else:
                self.dram_bank = int(self.dram_bank/2)
        # stage-independent
        self.dim = kwargs.pop('dim')
        self.extra_params = kwargs.pop('extra_params')
        # stage-specific
        input_node = kwargs.pop('input')
        output_node = kwargs.pop('output')

        if self.iterate > 1:
            if input_node.type != output_node.type:
                raise SemanticError('input must have the same type as output if iterate > 1 times, current input has type %s but output has type %s' % (input_node.type, output_node.type))
            if input_node.chan != output_node.chan:
                raise SemanticError('input must have the same number of channel as output if iterate > 1 times, current input has %d chanels but output has %d channels' % (input_node.chan, output_node.chan))
            _logger.debug('pipeline %d iterations of %s -> %s' % (self.iterate, input_node.name, output_node.name))

        locals = kwargs.pop('locals')
        local_names = set(x.name for x in locals)

        new_locals = []
        preserved_borders = {}  # {to: {from, ...}, ...}
        NameFromIter = lambda n, i: n+'_iter%d' % i if i > 0 else n
        LocalLoadCallBack = lambda n: NameFromIter(n, iteration) if n == input_node.name else NameFromIter(n, iteration) if n in local_names else n
        OutputLoadCallBack = lambda n: NameFromIter(n, iteration-1) if n == input_node.name or n in local_names else n
        for iteration in range(1, self.iterate):
            new_local = soda.grammar.Local(output_node=output_node)
            new_local.MutateLoad(OutputLoadCallBack)
            new_local.MutateStore(lambda n: NameFromIter(input_node.name, iteration))
            if self.preserve_border:
                border_from = NameFromIter(input_node.name, iteration-1)
                new_local.PreserveBorder(border_from)
                preserved_borders.setdefault(new_local.name, set()).add(border_from)
            new_locals.append(new_local)
            for local in locals:
                new_local = copy.deepcopy(local)
                new_local.MutateLoad(LocalLoadCallBack)
                new_local.MutateStore(lambda n: NameFromIter(n, iteration))
                new_locals.append(new_local)
        if self.preserve_border:
            border_from = NameFromIter(input_node.name, self.iterate-1)
            output_node.PreserveBorder(border_from)
            preserved_borders.setdefault(output_node.name, set()).add(border_from)
        output_node.MutateLoad(lambda n: NameFromIter(n, self.iterate-1))
        locals += new_locals

        _logger.debug(input_node)
        for local in locals:
            _logger.debug('Local(')
            _logger.debug('    name: %s,' % local.name)
            _logger.debug('    type: %s,' % local.type)
            _logger.debug('    chan: %s,' % local.chan)
            _logger.debug('    border: %s,' % local.border)
            for e in local.expr:
                _logger.debug('    expr  : [')
                _logger.debug('        %s%s' % (e, '])' if e is local.expr[-1] else ''))
        _logger.debug('Output(')
        _logger.debug('    name: %s,' % output_node.name)
        _logger.debug('    type: %s,' % output_node.type)
        _logger.debug('    chan: %s,' % output_node.chan)
        _logger.debug('    border: %s,' % output_node.border)
        for e in output_node.expr:
            _logger.debug('    expr  : [')
            _logger.debug('        %s%s' % (e, '])' if e is output_node.expr[-1] else ','))

        self.tensors = {i.name: Tensor(i) for i in locals}
        if input_node.name in local_names:
            raise SemanticError('input name conflict with tensor: %s' % self.input.name)
        else:
            self.input = Tensor(input_node)
            self.tensors[self.input.name] = self.input
        if output_node.name in local_names:
            raise SemanticError('output name conflict with tensor: %s' % self.output.name)
        else:
            self.output = Tensor(output_node)
            self.tensors[self.output.name] = self.output

        self.stages = {}
        for local in locals:
            child_tensor = self.tensors[local.name]
            parent_tensors = self.GetParentTensorsFor(local)
            window = self.GetWindowFor(local)
            this_stage = Stage(window=window,
                offset={n: SerializeIterative(w, self.tile_size) for n, w in window.items()},
                delay={},
                expr=self.GetExprFor(local),
                inputs=parent_tensors,
                output=child_tensor,
                border=local.border)
            self.stages[local.name] = this_stage
            child_tensor.parent = this_stage
            for b in parent_tensors.values():
                b.children.add(this_stage)

        parent_tensors = self.GetParentTensorsFor(output_node)
        window = self.GetWindowFor(output_node)
        output_stage = Stage(window=window,
            offset={n: SerializeIterative(w, self.tile_size) for n, w in window.items()},
            delay={},
            expr=self.GetExprFor(output_node),
            inputs=parent_tensors,
            output=self.output,
            border=output_node.border)
        self.stages[output_node.name] = output_stage
        self.output.parent = output_stage
        for b in parent_tensors.values():
            b.children.add(output_stage)

        # let tensor/stage remember which stages/tensors to send/recv as borders
        for dst, srcs in preserved_borders.items():
            for src in srcs:
                _logger.debug('border from %s to %s' % (src, dst))
            if self.tensors[src].border is None:
                self.tensors[src].border = ('preserve', set())
            self.tensors[src].border[1].add(self.stages[dst])
            if self.stages[dst].border is None:
                self.stages[dst].border = ('preserve', self.tensors[src])

        # now that we have global knowledge of the tensors we can calculate the offsets of tensors
        _logger.info('calculate tensor offsets')
        processing_queue = deque([self.input.name])
        processed_tensors = {self.input.name}
        self.chronological_tensors = [self.input]
        _logger.debug('tensor %s is at offset %d' % (self.input.name, self.input.offset))
        while len(processing_queue)>0:
            b = self.tensors[processing_queue.popleft()]
            _logger.debug('inspecting tensor %s\'s children' % b.name)
            for s in b.children:
                if ({x.name for x in s.inputs.values()} <= processed_tensors
                    and s.name not in processed_tensors):
                    # good, all inputs are processed
                    # can determine offset of current tensor
                    _logger.debug(
                        'input%s for tensor %s (i.e. %s) %s processed' % (
                            '' if len(s.inputs)==1 else 's',
                            s.name,
                            ', '.join([x.name for x in s.inputs.values()]),
                            'is' if len(s.inputs)==1 else 'are'))
                    stage_offset = Serialize(s.expr[0].idx, self.tile_size)

                    # synchronization check
                    def Sync(stage, offset):
                        if stage is None:
                            return offset
                        stage_offset = Serialize(stage.expr[0].idx,
                                                 self.tile_size)
                        loads = {}
                        for e in stage.expr:
                            for l in e.loads:
                                loads.setdefault(l.name, []).append(l.idx)
                        for n in loads:
                            loads[n] = SerializeIterative(loads[n],
                                                          self.tile_size)
                        for l in loads.values():
                            l[0], l[-1] = (stage_offset - max(l),
                                           stage_offset - min(l))
                            del l[1:-1]
                        _logger.debug(
                            'loads in tensor %s: %s' % (stage.name, loads))
                        for tensor in stage.inputs.values():
                            stage_distance = stage.offset[tensor.name][-1]
                            _logger.debug(
                                'want to access tensor %s at offset [%d, %d] '
                                'to generate tensor %s at offset %d' % (
                                    tensor.name, offset+loads[tensor.name][0],
                                    offset+loads[tensor.name][-1], stage.name,
                                    offset))
                            tensor_offset = (tensor.offset +
                                             stage_distance -
                                             stage_offset)
                            if offset < tensor_offset:
                                _logger.debug(
                                    'but tensor %s won\'t be available until '
                                    'offset %d' % (stage.name, tensor_offset))
                                offset = tensor_offset
                                _logger.debug(
                                    'need to access tensor %s at offset '
                                    '[%d, %d] to generate tensor %s at offset '
                                    '%d' % (tensor.name,
                                            offset+loads[tensor.name][0],
                                            offset+loads[tensor.name][-1],
                                            stage.name, offset))
                        return offset
                    _logger.debug('intend to generate tensor %s at offset %d'
                            % (s.name, s.output.offset))
                    s.output.offset = Sync(s, s.output.offset)
                    _logger.debug('decide to generate tensor %s at offset %d'
                            % (s.name, s.output.offset))

                    # add delay
                    for x in s.inputs.values():
                        delay = s.output.offset - (x.offset +
                                                   s.offset[x.name][-1] -
                                                   stage_offset)
                        if delay>0:
                            _logger.debug(
                                'tensor %s arrives at tensor %s '
                                'at offset %d < %d; add %d delay' % (
                                    x.name, s.name,
                                    x.offset+s.offset[x.name][-1]-stage_offset,
                                    s.output.offset, delay))
                        else:
                            _logger.debug(
                                'tensor %s arrives at tensor %s '
                                'at offset %d = %d; good' % (
                                    x.name, s.name,
                                    x.offset+s.offset[x.name][-1]-stage_offset,
                                    s.output.offset))
                        s.delay[x.name] = max(delay, 0)
                        _logger.debug('set delay of %s <- %s to %d' %
                            (s.name, x.name, s.delay[x.name]))

                    processing_queue.append(s.name)
                    processed_tensors.add(s.name)
                    self.chronological_tensors.append(s.output)
                else:
                    for bb in s.inputs.values():
                        if bb.name not in processed_tensors:
                            _logger.debug(
                                'tensor %s requires tensor %s as an input' %
                                (s.name, bb.name))
                            _logger.debug(
                                'but tensor %s isn\'t processed yet' % bb.name)
                            _logger.debug(
                                'add %s to scheduling queue' % bb.name)
                            processing_queue.append(bb.name)

        # setup preserving borders, has to be done here because overall window cannot be generated before dependency graph is created
        for node in locals+[output_node]:
            if hasattr(node, 'preserve_border'):
                # preserve border from node.preserve_border to node.name
                windows = self.stages[node.name].window
                windows.setdefault(node.preserve_border, list(set(windows.get(node.preserve_border, set()))|{next(iter(node.expr)).idx}))
                stencil_window = GetOverallStencilWindow(self.tensors[node.preserve_border], self.tensors[node.name])
                self.stages[node.name].delay.setdefault(node.preserve_border, GetStencilDistance(stencil_window, self.tile_size)-Serialize(GetStencilWindowOffset(stencil_window), self.tile_size))
                _logger.debug('window for %s@%s is %s' % (node.name, ', '.join(map(str, node.expr[0].idx)), windows))
                self.stages[node.name].inputs.setdefault(node.preserve_border, self.tensors[node.preserve_border])
                self.tensors[node.preserve_border].children.add(self.stages[node.name])

        _logger.debug('tensors: '+str(list(self.tensors.keys())))
        LoadPrinter = lambda node: '%s(%s)' % (node.name, ', '.join(map(str, node.idx))) if node.name in self.extra_params else '%s[%d](%s)' % (node.name, node.chan, ', '.join(map(str, node.idx)))
        StorePrinter = lambda node: '%s[%d](%s)' % (node.name, node.chan, ', '.join(map(str, node.idx)))
        for s in self.stages.values():
            _logger.debug('stage: %s@(%s) <- [%s]' % (s.name, ', '.join(map(str, s.idx)), ', '.join('%s@%s' % (x.name, list(set(s.window[x.name]))) for x in s.inputs.values())))
        for s in self.stages.values():
            for e in s.expr:
                _logger.debug('stage.expr: %s' % e)
        for s in self.stages.values():
            for n, w in s.offset.items():
                _logger.debug('stage.offset: %s@%d <- %s@[%s]' % (s.name, Serialize(s.output.idx, self.tile_size), n, ', '.join(map(str, w))))
        for s in self.stages.values():
            for n, d in s.delay.items():
                _logger.debug('stage.delay: %s <- %s delayed %d' % (s.name, n, d))

        # parameters generated from the above parameters
        self.pixel_width_i = type_width[self.input.type]
        self.pixel_width_o = type_width[self.output.type]
        self.input_partition  = self.burst_width/self.pixel_width_i*self.dram_bank/2 if self.burst_width/self.pixel_width_i*self.dram_bank/2 > self.unroll_factor/2 else self.unroll_factor/2
        self.output_partition = self.burst_width/self.pixel_width_o*self.dram_bank/2 if self.burst_width/self.pixel_width_o*self.dram_bank/2 > self.unroll_factor/2 else self.unroll_factor/2

        self.dataflow_super_source = create_dataflow_graph(self)

    def GetProducerTensors(self):
        return [b for b in self.tensors.values() if len(b.children)>0]

    def GetConsumerTensors(self):
        return [b for b in self.tensors.values() if b.parent is not None]

    def get_producer_tensors(self):
        return [t for t in self.tensors.values() if not t.is_output()]

    def GetStagesChronologically(self):
        return [self.stages[b.name] for b in self.chronological_tensors if b.name in self.stages]

    # return [Tensor, ...]
    def GetParentTensorsFor(self, node):
        return {x: self.tensors[x] for x in {x.name for x in node.GetLoads() if x.name not in self.extra_params}}

    # return {name: [(idx, ...), ...]}
    def GetWindowFor(self, node):
        loads = node.GetLoads() # [Load, ...]
        load_names = {l.name for l in loads if l.name not in self.extra_params}
        windows = {name: sorted({l.idx for l in loads if l.name == name}, key=lambda x: Serialize(x, self.tile_size)) for name in load_names}
        _logger.debug('window for %s@(%s) is %s' % (node.name, ', '.join(map(str, node.expr[0].idx)), windows))
        return windows

    # return [StageExpr, ...]
    def GetExprFor(self, node):
        if isinstance(node, soda.grammar.Output):
            return node.expr
        if isinstance(node, soda.grammar.Local):
            return node.expr
        raise SemanticError('cannot get expression for %s' % str(type(node)))

    def GetReuseBuffers(self):
        if not hasattr(self, 'reuse_buffers'):
            unroll_factor = self.unroll_factor
            self.reuse_buffer_lengths = {}
            self.reuse_buffers = {}
            for b in self.GetProducerTensors():
                reuse_buffer = _GetBuffer(self.tile_size, b, unroll_factor)
                reuse_buffer_length = {}
                self.reuse_buffers[b.name] = reuse_buffer
                self.reuse_buffer_lengths[b.name] = reuse_buffer_length
                first = [True]*unroll_factor
                for start, end in reuse_buffer[1:]:
                    if first[start%unroll_factor]:
                        first[start%unroll_factor] = False
                        if start >= unroll_factor:
                            reuse_buffer_length[end] = end//unroll_factor
                            continue
                    reuse_buffer_length[end] = (end-start)//unroll_factor
        return self.reuse_buffers

    def GetAllPoints(self):
        if not hasattr(self, 'all_points'):
            self.all_points = {}
            for b in self.GetProducerTensors():
                self.all_points[b.name] = _GetPoints(self.tile_size, b, self.unroll_factor)
        return self.all_points

    def GetNextFIFO(self):
        if not hasattr(self, 'next_fifo'):
            self.next_fifo = {}
            for name, reuse_buffer in self.GetReuseBuffers().items():
                self.next_fifo[name] = {}
                for start, end in reuse_buffer[1:]:
                    if start<end:
                        self.next_fifo[name][start] = end
            _logger.debug('next_fifo: %s' % self.next_fifo)
        return self.next_fifo

    def GetForwarders(self):
        if not hasattr(self, 'forwarders'):
            all_points = self.GetAllPoints()
            self.forwarders = set()
            self.forwarders_with_border = set()
            next_fifo = self.GetNextFIFO()
            for src_name, dsts in all_points.items():
                for dst_name, dst_point_dicts in dsts.items():
                    for offset, points in sorted(dst_point_dicts.items()):
                        if (
                            offset<self.unroll_factor and
                            self.tensors[src_name].PreserveBorderTo() and
                            not self.tensors[src_name].IsInput()):
                            self.forwarders_with_border |= {(
                                src_name,
                                len(self.GetForwardings(src_name)[offset][1]))}
                        else:
                            self.forwarders |= {
                                len(self.GetForwardings(src_name)[offset][1])}
            self.forwarders = sorted(self.forwarders)
            self.forwarders_with_border = sorted(self.forwarders_with_border)
        return self.forwarders

    def GetForwardersWithBorder(self):
        if not hasattr(self, 'forwarders_with_border'):
            self.GetForwarders()
        return self.forwarders_with_border

    def GetReuseBufferLength(self, name, offset):
        if not hasattr(self, 'reuse_buffer_lengths'):
            self.GetReuseBuffers()
        return self.reuse_buffer_lengths[name][offset]

    def GetForwardings(self, src_name):
        if hasattr(self, 'forwardings'):
            if src_name in self.forwardings:
                return self.forwardings[src_name]
        else:
            self.forwardings = {}
        next_fifo = self.GetNextFIFO()
        unroll_factor = self.unroll_factor
        dsts = self.GetAllPoints()[src_name]
        reuse_buffer = self.GetReuseBuffers()[src_name]

        # {offset: [func_name, outputs, inputs, params, temp_param]}
        forwardings = {}

        for dst_name, dst_point_dicts in dsts.items():
            for offset, points in dst_point_dicts.items():
                forwardings.setdefault(offset, ['', [], [], [], None])
                func_name = forwardings[offset][0]
                outputs = forwardings[offset][1]
                inputs = forwardings[offset][2]
                params = forwardings[offset][3]
                for unroll_index, point_index in points.items():
                    outputs.insert(
                        0,
                        '/* output */ '
                        'from_%s_to_%s_param_%d_chan_%%d_pe_%d' %
                        (src_name, dst_name, point_index, unroll_index))

                if func_name:
                    continue
                if offset in next_fifo[src_name]:
                    outputs.append(
                        '/* output */ %s_offset_%d_chan_%%d' %
                        (src_name, next_fifo[src_name][offset]))
                inputs.append(
                    '/*  input */ %s_offset_%d_chan_%%d' %
                    (src_name, offset))
                func_name = 'forward'
                temp_param = self.GetReuseBufferLength(src_name, offset)
                forward_num = len(params)-1
                if (
                    offset<self.unroll_factor and
                    self.tensors[src_name].PreserveBorderTo() and
                    not self.tensors[src_name].IsInput()):
                    stage = self.stages[src_name]
                    if stage.PreserveBorderFrom():
                        self_window_input = stage.PreserveBorderFrom()
                    else:
                        self_window_input = self.input
                    self_window = GetOverallStencilWindow(
                        self_window_input, stage.output)
                    overall_idx = GetStencilWindowOffset(self_window)
                    self_dim = GetStencilDim(self_window)
                    iteration = 1
                    parent = stage.PreserveBorderFrom()
                    while (
                        parent is not None and
                        parent.parent is not None):
                        parent = parent.parent.PreserveBorderFrom()
                        iteration += 1
                    delay = (
                        GetStencilDistance(self_window, self.tile_size)-
                        Serialize(overall_idx, self.tile_size))*iteration

                    func_name += '_'+src_name
                    temp_param = '%d-%d' % (unroll_index, delay)
                    for d in range(self.dim-1):
                        param_offset = (
                            (self.tile_size[d]-self_dim[d]+1)*
                            reduce(
                                operator.mul,
                                [self.tile_size[dd] for dd in range(d)],
                                1))
                        param = (
                            src_name, d,
                            (unroll_index+param_offset)%self.unroll_factor)
                        inputs.append(
                            '/*  input */ border_from_%s_dim_%d_'
                            'left_chan_%%d_pe_%d' % param)
                        param = (
                            src_name, d,
                            (unroll_index-param_offset)%self.unroll_factor)
                        inputs.append(
                            '/*  input */ border_from_%s_dim_%d_'
                            'right_chan_%%d_pe_%d' % param)
                    for d in range(self.dim-1):
                        params.append('/*  param */ input_bound_dim_%d' % d)
                    for d in range(self.dim):
                        params.append('/*  param */ input_size_dim_%d' % d)

                params.append('/*  param */ epoch_num')
                forwardings[offset][0] = func_name
                forwardings[offset][4] = temp_param
        self.forwardings[src_name] = forwardings
        return forwardings

    def get_replicated_next_fifo(self):
        if not hasattr(self, 'replicated_next_fifo'):
            self.replicated_next_fifo = {}
            for name, reuse_buffer in (self.get_replicated_reuse_buffers()
                    .items()):
                self.replicated_next_fifo[name] = {}
                for start, end in reuse_buffer[1:]:
                    if start<end:
                        self.replicated_next_fifo[name][start] = end
            _logger.debug('replicated_next_fifo: %s'
                % self.replicated_next_fifo)
        return self.replicated_next_fifo

    def get_replicated_reuse_buffer_length(self, name, offset):
        if not hasattr(self, 'replicated_reuse_buffer_lengths'):
            self.get_replicated_reuse_buffers()
        return self.replicated_reuse_buffer_lengths[name][offset]

    def get_replicated_reuse_buffers(self):
        if not hasattr(self, 'replicated_reuse_buffers'):
            replication_factor = self.replication_factor
            self.replicated_reuse_buffer_lengths = {}
            self.replicated_reuse_buffers = {}
            for tensor in self.get_producer_tensors():
                reuse_buffer =  _get_replicated_reuse_buffer(self.tile_size,
                    tensor, replication_factor)
                self.replicated_reuse_buffers[tensor.name] = reuse_buffer
                self.replicated_reuse_buffer_lengths[tensor.name] = {}
                first = [True]*self.replication_factor
                for start, end in reuse_buffer[1:]:
                    if first[start%replication_factor]:
                        first[start%replication_factor] = False
                        if start >= replication_factor:
                            self.replicated_reuse_buffer_lengths[tensor.name][
                                end] = end//replication_factor
                            continue
                    self.replicated_reuse_buffer_lengths[tensor.name][end] = (
                        end-start)//replication_factor
            _logger.debug('replicated_reuse_buffers: %s' %
                self.replicated_reuse_buffers)
            _logger.debug('replicated_reuse_buffer_lengths: %s' %
                self.replicated_reuse_buffer_lengths)
        return self.replicated_reuse_buffers

    def get_replicated_all_points(self):
        if not hasattr(self, 'replicated_all_points'):
            self.replicated_all_points = {}
            for tensor in self.get_producer_tensors():
                self.replicated_all_points[tensor.name
                    ] =  _get_replicated_points(self.tile_size, tensor)
            _logger.debug('replicated_all_points: %s' %
                self.replicated_all_points)
        return self.replicated_all_points

def _GetChains(tile_size, b, unroll_factor):
    _logger.debug('get reuse chains of tensor %s' % b.name)
    A_dag = set.union(*[(lambda offsets: set.union(*[{max(offsets)+i-x+s.delay[b.name] for x in offsets} for i in range(unroll_factor)]))(SerializeIterative(s.window[b.name], tile_size)) for s in b.children])
    _logger.debug('A† of tensor %s: %s' % (b.name, A_dag))
    chains = sum(reversed([tuple([tuple(sorted(x for x in A_dag if x%unroll_factor == i))]) for i in range(unroll_factor)]), ())
    for idx, chain in enumerate(chains):
        _logger.debug('reuse chain %d of tensor %s: %s' % (idx, b.name, chain))
    return chains

def _GetPoints(tile_size, b, unroll_factor):
    all_points = {} # {name:{offset:{unroll_index:point_index}}}
    for s in b.children:
        all_points[s.name] = {}
        offsets = SerializeIterative(s.window[b.name], tile_size)
        max_offset = max(offsets)
        for unroll_index in range(unroll_factor):
            for idx, offset in enumerate(offsets):
                all_points[s.name].setdefault(max_offset-offset+s.delay[b.name]+unroll_index, {})[unroll_factor-1-unroll_index] = idx
    for s in b.children:
        for offset, points in all_points[s.name].items():
            for unroll_index, point in points.items():
                _logger.debug('%s <- %s @ offset=%d <=> (%s) @ unroll_index=%d' % (s.name, b.name, offset, ', '.join(map(str, s.window[b.name][point])), unroll_index))
    return all_points

def _GetBuffer(tile_size, b, unroll_factor):
    reuse_buffer = [None]  # [length, (start, end), (start, end), ...]
    offsets = []
    for chain in _GetChains(tile_size, b, unroll_factor):
        reuse_buffer.append((chain[0], chain[0]))
        offsets.append(chain[0])
        for j in range(len(chain)-1):
            reuse_buffer.append((chain[j], chain[j+1]))
            offsets.append(chain[j+1])
    reuse_buffer[0] = max(offsets)+1
    _logger.debug('reuse chains of tensor %s: %s' % (b.name, reuse_buffer))
    return reuse_buffer

def _get_replicated_reuse_chains(tile_size, tensor, replication_factor):
    _logger.debug('\033[1mget replicated reuse chains of tensor %s\033[0m' %
        tensor.name)
    A_dag = set()
    for stage in tensor.children:
        offsets = SerializeIterative(stage.window[tensor.name], tile_size)
        A_dag |=  {max(offsets)-offset+stage.delay[tensor.name]
            for offset in offsets}
    _logger.debug('A† of tensor %s: %s' % (tensor.name, A_dag))
    chains = sum(reversed([
        tuple([tuple(sorted(x for x in A_dag if x%replication_factor == i))])
        for i in range(replication_factor)]), ())
    for idx, chain in enumerate(chains):
        _logger.debug('reuse chain %d of tensor %s: %s' %
            (idx, tensor.name, chain))
    return chains

def _get_replicated_points(tile_size, tensor):
    all_points = {} # {name:{offset:point_index}}
    for stage in tensor.children:
        all_points[stage.name] = {}
        offsets = SerializeIterative(stage.window[tensor.name], tile_size)
        max_offset = max(offsets)
        for idx, offset in enumerate(offsets):
            all_points[stage.name][
                max_offset-offset+stage.delay[tensor.name]] = idx
    for stage in tensor.children:
        for offset, points in all_points[stage.name].items():
            _logger.debug('%s <- %s @ offset=%d <=> (%s)' % (
                stage.name, tensor.name, offset,
                ', '.join(map(str, stage.window[tensor.name][points]))))
    return all_points

def _get_replicated_reuse_buffer(tile_size, tensor, replication_factor):
    reuse_buffer = [None] # [length, (start, end), (start, end), ...]
    offsets = []
    for chain in _get_replicated_reuse_chains(tile_size, tensor,
            replication_factor):
        if len(chain) > 0:
            reuse_buffer.append((chain[0], chain[0]))
            offsets.append(chain[0])
            for j in range(len(chain)-1):
                reuse_buffer.append((chain[j], chain[j+1]))
                offsets.append(chain[j+1])
    reuse_buffer[0] = max(offsets)+1
    _logger.debug('reuse chains of tensor %s: %s' %
        (tensor.name, reuse_buffer))
    return reuse_buffer

class Printer(object):
    def __init__(self, out):
        self.out = out
        self.indent = 0
        self.assign = 0
        self.comments = []

    def PrintLine(self, line = '', local_indent = -1):
        if local_indent < 0:
            local_indent = self.indent
        if line:
            self.out.write('%s%s\n' % (' '*local_indent*4, line))
        else:
            self.out.write('\n')

    def DoIndent(self):
        self.indent += 1

    def UnIndent(self):
        self.indent -= 1

    def DoScope(self, comment=''):
        self.PrintLine('{')
        self.DoIndent()
        self.comments.append(comment)

    def UnScope(self, comment=''):
        self.UnIndent()
        popped_comment = self.comments.pop()
        if comment:
            self.PrintLine('} // %s' % comment)
        else:
            if popped_comment:
                self.PrintLine('} // %s' % popped_comment)
            else:
                self.PrintLine('}')

    def NewVar(self):
        self.assign += 1
        return self.LastVar()

    def LastVar(self, offset=-1):
        return 'assign_%d' % (self.assign+offset)

    def PrintFunc(self, name, params, suffix='', align=80):
        lines = [name+'(']
        for param in params:
            if ((self.indent + min(1, len(lines)-1))*4+
                    len(lines[-1])+len(param+', ')) > align:
                lines.append(param+', ')
            else:
                lines[-1] += param+', '
        if lines[-1][-2:] == ', ':
            lines[-1] = lines[-1][:-2]+')'+suffix
        line = lines.pop(0)
        self.PrintLine(line)
        if lines:
            self.DoIndent()
            for line in lines:
                self.PrintLine(line)
            self.UnIndent()

def GetCType(soda_type):
    if soda_type in {'uint8', 'uint16', 'uint32', 'uint64', 'int8', 'int16', 'int32', 'int64'}:
        return soda_type+'_t'
    return soda_type

def GetSodaType(c_type):
    return c_type[:-2] if c_type[-2:] == '_t' else c_type

def IsFloat(soda_type):
    return soda_type in {'float', 'double'}

def PrintGuard(printer, var, val):
    printer.PrintLine('#if %s != %d' % (var, val))
    printer.PrintLine('#error %s != %d' % (var, val))
    printer.PrintLine('#endif//%s != %d' % (var, val))

def PrintDefine(printer, var, val):
    printer.PrintLine('#ifndef %s' % var)
    printer.PrintLine('#define %s %d' % (var, val))
    printer.PrintLine('#endif//%s' % var)

def GetIndicesId(indices):
    return '_'.join(str(idx).replace('-', 'm') for idx in indices)

def Serialize(vec, tile_size):
    return sum((vec[i]*reduce(operator.mul, tile_size[:i]) for i in range(1, len(tile_size))), next(iter(vec)))

def SerializeIterative(iterative, tile_size):
    return [Serialize(x, tile_size) for x in iterative]

def GetStencilDistance(stencil_window, tile_size):
    return max(SerializeIterative(stencil_window, tile_size))+Serialize(GetStencilWindowOffset(stencil_window), tile_size)

def GetStencilDim(A):
    return [max_index-min_index+1 for max_index, min_index in zip([max([point[dim] for point in A]) for dim in range(len(next(iter(A))))], [min([point[dim] for point in A]) for dim in range(len(next(iter(A))))])]

_overall_stencil_window_cache = {}
def GetOverallStencilWindow(input_tensor, output_tensor):
    # normalize store index to 0
    if (id(input_tensor), id(output_tensor)) in _overall_stencil_window_cache:
        return _overall_stencil_window_cache[(id(input_tensor), id(output_tensor))]
    _logger.debug('get overall stencil window of %s <- %s' % (output_tensor.name, input_tensor.name))
    all_points = set()
    if output_tensor.parent is not None:
        for name, points in output_tensor.parent.window.items():
            if name != input_tensor.name:
                recursive_points = GetOverallStencilWindow(input_tensor, output_tensor.parent.inputs[name])
                all_points |= set.union(*[{tuple(map(lambda a, b, c: a + b - c, p, point, output_tensor.idx)) for p in recursive_points} for point in points])
            else:
                all_points |= set(tuple(map(operator.sub, point, output_tensor.idx)) for point in points)
    _logger.debug('overall stencil window of %s (%s) <- %s is %s (%d points)' % (output_tensor.name, ', '.join(['0']*len(output_tensor.idx)), input_tensor.name, all_points, len(all_points)))
    _overall_stencil_window_cache[(id(input_tensor), id(output_tensor))] = all_points
    return all_points

def GetStencilWindowOffset(stencil_window):
    # only works if window is normalized to store at 0
    return tuple(-min(p[d] for p in stencil_window) for d in range(len(next(iter(stencil_window)))))
