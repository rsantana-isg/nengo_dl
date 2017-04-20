from collections import OrderedDict, defaultdict
import copy
import logging

from nengo.synapses import Lowpass
from nengo.builder.operator import (SimPyFunc, ElementwiseInc, Copy,
                                    Reset)
from nengo.builder.neurons import SimNeurons
from nengo.builder.processes import SimProcess
from nengo.exceptions import BuildError
from nengo.utils.compat import iteritems
from nengo.utils.graphs import toposort, BidirectionalDAG, transitive_closure
from nengo.utils.simulator import operator_depencency_graph
import numpy as np

from nengo_dl import signals, processes, builder, tensor_node

logger = logging.getLogger(__name__)


def mergeable(op, chosen_ops):
    """Check if the given op can be merged with the candidate group

    Parameters
    ----------
    op : :class:`~nengo:nengo.builder.Operator`
        the operator to be merged
    chosen_ops : list of :class:`~nengo:nengo.builder.Operator`
        the operator group to be merged in to

    Returns
    -------
    bool
        True if ``op`` can be merged into ``chosen_ops``, else False
    """

    if len(chosen_ops) == 0:
        return True

    # note: we only need to check against the first item in the list,
    # since we know the rest all match
    c = chosen_ops[0]

    # must share the same builder
    if builder.Builder.builders[type(op)] != builder.Builder.builders[type(c)]:
        return False

    # sets/incs/reads/updates must all match
    if (len(op.sets) != len(c.sets) or len(op.incs) != len(c.incs) or
            len(op.reads) != len(c.reads) or
            len(op.updates) != len(c.updates)):
        return False

    # signals must be mergeable into the same base array
    for s0, s1 in zip(op.all_signals, c.all_signals):
        # dtype of signals must match
        if s0.dtype != s1.dtype:
            return False

        # shape of signal base must match on all axes > 0
        if s0.base.shape[1:] != s1.base.shape[1:]:
            return False

        # display shape must also match (since we need the shape to be well
        # defined when we combine the signals)
        if s0.shape[1:] != s1.shape[1:]:
            return False

        # trainable/minibatched must match
        if s0.trainable != s1.trainable or s0.minibatched != s1.minibatched:
            return False

    # operator-specific checks
    if isinstance(op, Copy):
        # can't merge incs and updates
        if op.inc != c.inc:
            return False
    elif isinstance(op, ElementwiseInc):
        # for these operations we also enforce that the first dimensions
        # match (we know all the other dimensions match due to checks above).
        # this allows us to stack all the arguments into continuous array
        # blocks, allowing for more efficient multiplication (mainly
        # because it allows us to take advantage of broadcasting)
        for s0, s1 in zip(op.all_signals, c.all_signals):
            shape0 = s0.shape[0] if s0.shape != () else 1
            shape1 = s1.shape[0] if s1.shape != () else 1
            if shape0 != shape1:
                return False
    elif isinstance(op, SimPyFunc):
        # for these we need to make a special check that the functions
        # all do/do not get time as input, otherwise we could end
        # up confusing a node that only gets a scalar float input with
        # a node that only gets time as input
        if op.t != c.t:
            return False
    elif isinstance(op, SimNeurons):
        # neuron ops must all have the same type
        if type(c.neurons) != type(op.neurons):
            return False
    elif isinstance(op, SimProcess):
        # we can merge ops if they have a custom implementation, or merge
        # generic processes, but can't mix the two

        if type(c.process) in processes.SimProcessBuilder.TF_PROCESS_IMPL:
            if type(c.process) != type(op.process):
                return False
        elif type(op.process) in processes.SimProcessBuilder.TF_PROCESS_IMPL:
            return False

        # processes must also have the same mode
        if op.mode != c.mode:
            return False
    elif isinstance(op, tensor_node.SimTensorNode):
        # not possible to merge TensorNodes, since each one can be performing
        # an entirely different function. and unlike SimPyFunc, there is no
        # point trying to execute all those functions at once, because they're
        # already integrated into the Tensorflow graph.
        return False

    return True


# TODO: implement transitive-closure based planner

def greedy_planner(operators):
    """Combine mergeable operators into groups that will be executed as a
    single computation.

    Parameters
    ----------
    operators : list of :class:`~nengo:nengo.builder.Operator`
        all the ``nengo`` operators in a model (unordered)

    Returns
    -------
    list of tuple of :class:`~nengo:nengo.builder.Operator`
        operators combined into mergeable groups and in execution order

    Notes
    -----
    Originally based on ``nengo_ocl`` greedy planner
    """

    dependency_graph = operator_depencency_graph(operators)

    # map unscheduled ops to their direct predecessors and successors
    predecessors_of = {}
    successors_of = {}
    for op in operators:
        predecessors_of[op] = set()
        successors_of[op] = set()
    for op, dests in iteritems(dependency_graph):
        for op2 in dests:
            predecessors_of[op2].add(op)
        successors_of[op].update(dests)

    # the ops in `available` are ready to be scheduled (all predecessors
    # have been scheduled).
    # initialize it with the ops that have no predecessors
    available = [op for op, dep in iteritems(predecessors_of) if len(dep) == 0]

    plan = []
    groups = []
    while len(predecessors_of) > 0:
        # sort the available ops into mergeable groups
        for op in available:
            for g in groups:
                if mergeable(op, g):
                    g += [op]
                    break
            else:
                groups += [[op]]

        if len(groups) == 0:
            raise BuildError("Cycle detected during graph optimization")

        # pick the group that has the largest number of available ops
        groups = sorted(groups, key=lambda x: len(x))
        chosen = groups[-1]
        groups = groups[:-1]

        plan += [tuple(chosen)]

        # update predecessors and successors of remaining ops, and check for
        # any newly available ops
        available = []
        for op in chosen:
            for op2 in successors_of[op]:
                preds = predecessors_of[op2]
                preds.remove(op)
                if len(preds) == 0:
                    available += [op2]
            del predecessors_of[op]
            del successors_of[op]

    logger.debug("GREEDY PLAN")
    logger.debug("\n" + "\n".join([str(x) for x in plan]))

    assert len(operators) == sum(len(ops) for ops in plan)

    return plan


def tree_planner(operators):
    """Create merged execution plan through exhaustive tree search.

    Unlike :func:`.graph_optimizer.greedy_planner`, this is guaranteed to find
    the shortest plan. However, depending on the structure of the operator
    graph, it can take a long time to execute.

    Parameters
    ----------
    operators : list of :class:`~nengo:nengo.builder.Operator`
        all the ``nengo`` operators in a model (unordered)

    Returns
    -------
    list of tuple of :class:`~nengo:nengo.builder.Operator`
        operators combined into mergeable groups and in execution order
    """

    def shortest_plan(ops, successors_of, predecessors_of, cache):
        logger.debug("shortest_plan")
        logger.debug(ops)

        if len(ops) <= 1:
            # normal termination
            return [ops] if len(ops) == 1 else []
        elif ops in cache:
            # we've already found the shortest path for this set of ops
            # (plans are markovian)
            return cache[ops]

        # get the groups that could be scheduled next
        free = [op for op in ops if len(predecessors_of[op]) == 0]

        logger.debug("free %s", free)

        available = []
        for op in free:
            for i, group in enumerate(available):
                if mergeable(op, group):
                    available[i] += (op,)
                    break
            else:
                available += [(op,)]

        logger.debug("available")
        logger.debug(available)

        if len(available) == 0:
            raise BuildError("Cycle detected during graph optimization")

        # check what the shortest plan is after selecting each available group
        shortest = None
        for group in available:
            pred = {k: copy.copy(v) for k, v in predecessors_of.items()}
            for op in group:
                for op2 in successors_of[op]:
                    pred[op2].remove(op)

            logger.debug("selecting %s", group)

            result = shortest_plan(
                tuple(op for op in ops if op not in group),
                successors_of, pred, cache)

            if shortest is None or len(result) + 1 < len(shortest):
                shortest = [group] + result

                logger.debug("new shortest plan detected")
                logger.debug(shortest)

        cache[ops] = shortest

        return shortest

    dependency_graph = operator_depencency_graph(operators)

    predecessors_of = {}
    successors_of = {}
    for op in operators:
        predecessors_of[op] = set()
        successors_of[op] = set()
    for op in operators:
        dests = dependency_graph[op]
        for op2 in dests:
            predecessors_of[op2].add(op)
        successors_of[op].update(dests)

    tmp = shortest_plan(tuple(operators), successors_of, predecessors_of, {})

    logger.debug("TREE PLAN")
    logger.debug("\n".join([str(x) for x in tmp]))

    return tmp


def noop_planner(operators):
    """Orders operators into a valid execution order, but does not perform
    any merging.

    Parameters
    ----------
    operators : list of :class:`~nengo:nengo.builder.Operator`
        all the ``nengo`` operators in a model (unordered)

    Returns
    -------
    list of tuple of :class:`~nengo:nengo.builder.Operator`
        operators in execution order
    """

    dependency_graph = operator_depencency_graph(operators)
    plan = [(op,) for op in toposort(dependency_graph)]

    logger.debug("NOOP PLAN")
    logger.debug("\n" + "\n".join([str(x) for x in plan]))

    return plan


def transitive_planner(operators):
    dg = BidirectionalDAG(operator_depencency_graph(operators))

    logger.debug("operators")
    logger.debug("\n".join(str(op) for op in operators))

    groups = []

    while len(operators) > 0:
        trans = transitive_closure(dg.forward)

        # TODO: use heuristic ordering
        group = [operators.pop()]
        for op in operators:
            if mergeable(op, group):
                for op2 in group:
                    if op2 in trans[op] or op in trans[op2]:
                        break
                else:
                    group.append(op)

        for op in group[1:]:
            operators.remove(op)

        dg.merge(group, tuple(group))

    logger.debug("merged dg")
    logger.debug("\n".join("%s:\n    %s" % (k, v)
                           for k, v in dg.forward.items()))

    plan = toposort(dg.forward)

    logger.debug("TRANSITIVE PLAN")
    logger.debug("\n" + "\n".join([str(x) for x in plan]))

    return plan


def order_signals(plan, n_passes=10):
    """Orders signals and operators to try to structure reads in contiguous
    blocks.

    Parameters
    ----------
    plan : list of tuple of :class:`~nengo:nengo.builder.Operator`
        operator execution plan (e.g., output from ``greedy_planner``)
    n_passes : int, optional
        number of repeated passes through the operator reordering stage

    Returns
    -------
    list of :class:`~nengo:nengo.builder.Signal`
        signals organized into the order in which we want them arranged in
        memory
    list of tuple of :class:`~nengo:nengo.builder.Operator`
        input plan with operators reordered within groups to align with order
        of signals
    """

    # get all the unique base signals
    all_signals = list(set([s.base for ops in plan for op in ops
                            for s in op.all_signals]))

    # figure out all the read blocks in the plan (in theory we would like each
    # block to become a contiguous chunk in the base array)
    read_blocks = {}

    # note: reads[op] contains all the signals that are inputs to op. this is
    # generally equivalent to op.reads, but there are some ops that also
    # require their set/inc/updates as input. we don't want to modify
    # op.reads itself, because then if you pass the same model to the
    # Simulator the operators keep getting modified in-place.
    reads = {}
    for ops in plan:
        for op in ops:
            reads[op] = [x for x in op.reads]
            if type(op) == SimNeurons:
                # state signals are technically reads as well, they just aren't
                # marked as such, so we add them to the reads list
                reads[op] += op.states
            elif type(op) == SimProcess and isinstance(op.process, Lowpass):
                # the lowpass op has to read the output value as well (unless
                # we get a scatter_mul working)
                reads[op] += op.updates

        # the ith input signal for each op in the op group is one read group
        # (note that we only care about bases, since those are the things we
        # are trying to order)
        for i in range(len(reads[ops[0]])):
            read_blocks[(ops, i)] = set(reads[op][i].base for op in ops)

    if len(read_blocks) == 0:
        # no reads, so nothing to reorder
        return all_signals, plan

    # get rid of duplicate read blocks
    duplicates = [
        [y for y in read_blocks.values() if x == y]
        for x in read_blocks.values()]
    sorted_blocks = [
        (x, len(duplicates[i])) for i, x in enumerate(read_blocks.values())
        if duplicates[i][0] is x]

    # sort by the size of the block (descending order)
    # note: we multiply by the number of duplicates, since read blocks that
    # are read by multiple op groups will have a proportionally larger impact
    # on performance
    # TODO: maybe we should just care about duplicates (how much does the size
    # of the block affect gather/slice time?)
    sorted_blocks = sorted(
        sorted_blocks, key=lambda b: np.sum([s.size for s in b[0]]) * b[1])
    sorted_blocks = [sorted_blocks[i][0] for i in
                     range(len(sorted_blocks) - 1, -1, -1)]

    # figure out which read blocks each signal participates in
    signal_blocks = defaultdict(list)
    for i, b in enumerate(sorted_blocks):
        for s in b:
            signal_blocks[s].append(i)
    signal_blocks = {s: frozenset(b) for s, b in signal_blocks.items()}

    logger.debug("all signals")
    logger.debug(all_signals)
    logger.debug("sorted blocks")
    logger.debug(sorted_blocks)
    logger.debug("signal blocks")
    logger.debug(signal_blocks)

    # list of the ops in each read block, sorted by the size of that read block
    sorted_reads = sorted(
        read_blocks.keys(),
        key=lambda p: -sorted_blocks.index(read_blocks[p]))

    logger.debug("sorted reads")
    logger.debug("\n".join(str(x) for x in sorted_reads))

    # reorder the signals into contiguous blocks (giving higher priority
    # to larger groups)
    sort_idxs = hamming_sort(signal_blocks)
    all_signals = sorted(all_signals, key=lambda s: sort_idxs[s])

    logger.debug("hamming sorted signals")
    logger.debug(all_signals)

    # now we want to order the ops and signals within the blocks established
    # by the hamming sort

    # basically we're going to repeatedly iterate over two steps
    # 1) order the ops within a group according to the order of their
    #    read signals
    # 2) order/group the signals according to operator groups

    # we iterate through the groups in order of increasing size, so that
    # if later reorderings (in 2) break a previous order, we tend to leave the
    # largest blocks in order.
    # similarly, we do multiple passes through this sorting because if a
    # later group breaks the ordering of an earlier one, it is possible that
    # on the next pass we can put the first group back into a valid ordering
    # based on the order established by the later group.

    new_plan = {ops: ops for ops in plan}
    sig_idxs = {s: i for i, s in enumerate(all_signals)}

    logger.debug("plan")
    logger.debug("\n" + "\n".join([str(x) for x in new_plan.values()]))
    logger.debug("signal indices")
    logger.debug(sig_idxs)

    for n in range(n_passes):
        # TODO: every few iterations, eliminate the smallest unsatisfied block?
        logger.debug("======== pass %d ========", n)

        # save previous plan/idxs, so we can check if they change for
        # early termination
        prev_plan = {k: v for k, v in new_plan.items()}
        prev_sig_idxs = sig_idxs  # note: no copy necessary

        # reorder ops by signal order. this leaves the overall
        # hamming sort block order unchanged.
        new_plan, sig_idxs = sort_ops_by_signals(
            sorted_reads, all_signals, sig_idxs, new_plan, signal_blocks,
            reads)

        logger.debug("resorted ops")
        logger.debug("\n" + "\n".join([str(x) for x in new_plan.values()]))

        logger.debug("reordered signal indices")
        logger.debug(sig_idxs)

        if (all([x == y for ops in plan
                 for x, y in zip(new_plan[ops], prev_plan[ops])]) and
                all([sig_idxs[s] == prev_sig_idxs[s] for s in all_signals])):
            # if the plan didn't change and the signals didn't change, then
            # there is no point in continuing (they're not going to change
            # in the future)
            logger.debug("early termination")
            break

    sorted_signals = sorted(all_signals, key=lambda s: sig_idxs[s])

    # error checking
    # make sure that overall signal block order didn't change
    for s, s2 in zip(all_signals, sorted_signals):
        if s in signal_blocks or s2 in signal_blocks:
            assert signal_blocks[s] == signal_blocks[s2]

    # make sure that all ops are present
    assert len(new_plan) == len(plan)
    for ops, new_ops in new_plan.items():
        assert len(ops) == len(new_ops)
        # for op in ops:
        #     assert op in new_ops

    logger.debug("final sorted signals")
    logger.debug(sorted_signals)
    logger.debug("new plan")
    logger.debug("\n" + "\n".join([str(x) for x in new_plan.values()]))

    return sorted_signals, [new_plan[ops] for ops in plan]


def hamming_sort(blocks):
    """Reorder signals using heuristics to try to place signals that are read
    by the same operators into adjacent positions (giving priority to larger
    blocks).

    Parameters
    ----------
    blocks : dict of {:class:`~nengo:nengo.builder.Signal`: frozenset of int}
        dictionary indicating which read blocks each signal is a part of

    Returns
    -------
    dict of {:class:`~nengo:nengo.builder.Signal`: int}
        indices indicating where each signal should be in the sorted list
    """

    sorted_blocks = []
    curr_blocks = None
    active_block = None

    unique_blocks = set(blocks.values())

    n_unique = len(unique_blocks)

    logger.debug("hamming sort:")
    logger.debug("unique blocks")
    logger.debug(unique_blocks)

    while True:
        logger.debug("curr_blocks %s", curr_blocks)

        if curr_blocks is None:
            # first pass through loop, initialize with default first block
            # (the rest of the loop will figure out what the actual first
            # block will be)
            curr_blocks = frozenset([0])
        else:
            # add the selected block to the sorted list
            sorted_blocks.append(curr_blocks)
            unique_blocks.remove(curr_blocks)

        if len(sorted_blocks) == n_unique:
            break

        # pick which block to go to next

        # start by picking all the blocks that are a continuation of the
        # active block (this is to give us some persistence, so it doesn't
        # jump around too much)
        if active_block is None:
            # pick the largest block in the current block to become the new
            # active block (note: the blocks are sorted from largest to
            # smallest, so the smallest value in curr_blocks is the largest
            # block. this ordering is used in several places)
            active_block = min(curr_blocks)

        next_blocks = [b for b in unique_blocks if active_block in b]
        if len(next_blocks) == 0:
            # there are no remaining blocks that are a continuation of the
            # current block, so they're all up for grabs
            next_blocks = unique_blocks
            active_block = None

        logger.debug("active block %s", active_block)
        logger.debug("next blocks")
        logger.debug(next_blocks)

        # then within all the blocks that are a potential continuation,
        # pick the ones with the smallest hamming distance
        # TODO: we should set this up so it prefers to add new blocks
        # rather than discontinuing a block
        next_dists = [len(curr_blocks ^ b) for b in next_blocks]
        min_dist = min(next_dists)
        next_blocks = [b for i, b in enumerate(next_blocks)
                       if next_dists[i] == min_dist]

        logger.debug("hamming filter")
        logger.debug(next_blocks)

        # within all the blocks that have the same hamming distance, pick the
        # next block that matches along the largest blocks
        for i in sorted(curr_blocks):
            if len(next_blocks) == 1:
                break

            if any(i in b for b in next_blocks):
                next_blocks = [b for b in next_blocks if i in b]

        # within the blocks that match curr_block equally, pick the next block
        # containing the largest read blocks
        if len(next_blocks) > 1:
            next_blocks = [frozenset(min(sorted(b) for b in next_blocks))]

        curr_blocks = next_blocks[0]

    # the sort index for each signal is just the position of its block in
    # the sorted block list (since we don't care about the order of
    # signals within each block). signals that aren't part of any read block
    # get a default value of -1.
    block_idxs = {b: i for i, b in enumerate(sorted_blocks)}
    sort_idxs = defaultdict(
        lambda: -1, [(s, block_idxs[b]) for s, b in blocks.items()])

    return sort_idxs


def sort_ops_by_signals(sorted_reads, sigs, sig_idxs, new_plan, blocks, reads):
    """Rearrange operators to match the order of signals.

    Note: the same operators can be associated with multiple read blocks if
    they have multiple inputs, so rearranging the operators according to one
    of those blocks could mess up the order with respect to the other read
    block.  We iterate through the read blocks in increasing size so
    that the largest blocks win out.

    Parameters
    ----------
    sorted_reads : list of tuple of (:class:`~nengo:nengo.builder.Operator`, \
                                     int)
        the operators that form each read block, sorted by increasing size of
        the read block. in the case that a group of operators participate in
        multiple read blocks, the integer distinguishes which one of those
        inputs this block is associated with.
    sigs : list of :class:`~nengo:nengo.builder.Signal`
        signals that have been arranged into a given order by other parts
        of the algorithm
    sig_idxs : dict of {:class:`~nengo:nengo.builder.Signal`: int}
        sorted indices of signals
    new_plan : dict of {tuple of :class:`~nengo:nengo.builder.Operator`: \
                        tuple of :class:`~nengo:nengo.builder.Operator`}
        mapping from original operator group to the sorted operators
    blocks : dict of {:class:`~nengo:nengo.builder.Signal`: frozenset of int}
        indicates which read blocks each signal participates in
    reads : dict of {:class:`~nengo:nengo.builder.Operator`: \
                     list of :class:`~nengo:nengo.builder.Signal`}
        the signals read by each operator

    Returns
    -------
    new_plan : dict of {tuple of :class:`~nengo:nengo.builder.Operator`: \
                        tuple of :class:`~nengo:nengo.builder.Operator`}
        mapping from original operator group to the sorted operators
    sig_idxs : dict of {:class:`~nengo:nengo.builder.Signal`: int}
        signal indices, possibly updated to match new op order
    """

    logger.log(logging.DEBUG - 1, "sort ops by signals")

    for old_ops, read_block in sorted_reads:
        logger.log(logging.DEBUG - 1, "-" * 30)
        logger.log(logging.DEBUG - 1, "sorting ops %s", new_plan[old_ops])
        logger.log(logging.DEBUG - 1, "by %s",
                   [reads[op][read_block] for op in new_plan[old_ops]])

        if len(old_ops) == 1:
            # then we have nothing to sort
            continue

        ops = new_plan[old_ops]

        # note: the key is (signal index, view offset), so ops will be
        # sorted first by the order of the signals in the list, then by
        # the order of the views within each signal
        sorted_ops = sorted(
            ops, key=lambda op: (sig_idxs[reads[op][read_block].base],
                                 reads[op][read_block].elemoffset))

        new_plan[old_ops] = tuple(sorted_ops)

        logger.log(logging.DEBUG - 1, "sorted ops")
        logger.log(logging.DEBUG - 1, new_plan[old_ops])

        # after sorting the operators, we then rearrange all the read
        # blocks associated with this group of operators to match the new
        # order. note that this could make smaller (earlier) blocks out
        # of order, which will hopefully be fixed on future passes. however,
        # it means that larger (later) blocks will align themselves to this
        # order if possible
        # note2: we include the current read block in the groups to be sorted,
        # because while we know that these ops are in the same relative order
        # as the signals, the signals may not be adjacent (sorting will try
        # to make them adjacent)
        sig_idxs = sort_signals_by_ops(
            [x for x in sorted_reads if x[0] == old_ops],
            sigs, sig_idxs, new_plan, blocks, reads)

    return new_plan, sig_idxs


def sort_signals_by_ops(sorted_reads, sigs, sig_idxs, new_plan, blocks, reads):
    """Attempts to rearrange ``sigs`` so that it is in the same order as
    operator reads, without changing the overall block order.

    Parameters
    ----------
    sorted_reads : list of tuple of (:class:`~nengo:nengo.builder.Operator`, \
                                     int)
        the operators that form each read block, sorted by increasing size of
        the read block. in the case that a group of operators participate in
        multiple read blocks, the integer distinguishes which one of those
        inputs this block is associated with.
    sigs : list of :class:`~nengo:nengo.builder.Signal`
        signals to be sorted
    sig_idxs : dict of {:class:`~nengo:nengo.builder.Signal`: int}
        sorted indices of signals
    new_plan : dict of {tuple of :class:`~nengo:nengo.builder.Operator`: \
                        tuple of :class:`~nengo:nengo.builder.Operator`}
        mapping from original operator group to the sorted operators
    blocks : dict of {:class:`~nengo:nengo.builder.Signal`: frozenset of int}
        indicates which read blocks each signal participates in
    reads : dict of {:class:`~nengo:nengo.builder.Operator`: \
                     list of :class:`~nengo:nengo.builder.Signal`}
        the signals read by each operator

    Returns
    -------
    sig_idxs : dict of {:class:`~nengo:nengo.builder.Signal`: int}
        sorted indices of signals
    """

    logger.log(logging.DEBUG - 1, "-" * 10)
    logger.log(logging.DEBUG - 1, "sort signals by ops")

    for old_ops, read_block in sorted_reads:
        logger.log(logging.DEBUG - 1, "sorting signals %s",
                   [reads[op][read_block] for op in new_plan[old_ops]])
        logger.log(logging.DEBUG - 1, "%d %s", read_block, new_plan[old_ops])

        ops = new_plan[old_ops]

        sort_vals = {s: i for i, s in
                     enumerate(reads[op][read_block].base for op in ops)}

        if len(sort_vals) == 1:
            # only one read signal, so nothing to sort
            continue

        first_block = True
        last_block = False
        curr_block = None
        pre = []
        post = []
        curr_max = -1
        sortable = True
        sort_idxs = [sig_idxs[s] for s in sort_vals]
        min_index = min(sort_idxs)
        max_index = max(sort_idxs)

        for i, s in enumerate(sigs[min_index:max_index + 1]):
            # we try to sort things into everything <= the first read block
            # in op_reads and everything after, with the op_reads signals in
            # the middle (ordered to match op_reads)
            sort_item = s in sort_vals

            if blocks[s] != curr_block:
                if last_block:
                    # if the block changes after the last block, that means
                    # that there are still sortable items in the new block,
                    # but there were `post` items in the previous block,
                    # so the list is not sortable
                    sortable = False
                    break

                first_block = False
                prev_max = curr_max
                curr_max = -1
                curr_block = blocks[s]

            if sort_item:
                idx = sort_vals[s]
                if idx < prev_max:
                    # if the sort position for this signal is less than the
                    # end of the previous sorted block, then the list is not
                    # sortable
                    sortable = False
                    break

                # update the max sort index in this block
                curr_max = max(curr_max, idx)
            elif first_block:
                # only for the first block, we want to add non-sort items to
                # the beginning of the list instead of the end
                pre.append(s)
            else:
                # s is not in sort_vals, and this is not the first block,
                # so this must be the last block (we can't have items
                # not in sort_vals in a middle block, or they will be
                # unsortable)
                last_block = True
                post.append(s)

        if sortable:
            for i, s in enumerate(pre):
                sig_idxs[s] = min_index + i

            offset = min_index + len(pre)
            for i, s in enumerate(sorted(sort_vals,
                                         key=lambda s: sort_vals[s])):
                sig_idxs[s] = offset + i

            offset += len(sort_vals)
            for i, s in enumerate(post):
                sig_idxs[s] = offset + i

            logger.log(logging.DEBUG - 1, "sorted indices %s", sig_idxs)

    return sig_idxs


def noop_order_signals(plan, **kwargs):
    """A version of :func:`.graph_optimizer.order_signals` that doesn't do any
    reordering, for debugging."""

    all_signals = list(set([s.base for ops in plan for op in ops
                            for s in op.all_signals]))
    return all_signals, plan


def create_signals(sigs, plan, float_type, minibatch_size):
    """Groups signal data together into larger arrays, and represent each
    individual signal as a slice into that array.

    Parameters
    ----------
    sigs : list of :class:`~nengo:nengo.builder.Signal`
        base signals arranged into the order in which they should reside in
        memory (e.g., output from ``order_signals``)
    plan : list of tuple of :class:`~nengo:nengo.builder.Operator`
        operator execution plan (only used to get a list of all the operators)
    float_type : ``np.float32`` or ``np.float64``
        floating point precision to use for signals
    minibatch_size : int
        number of items in each minibatch

    Returns
    -------
    base_arrays : dict of {object : :class:`~numpy:numpy.ndarray`}
        combined arrays, containing the initial values for all signals
    sig_map : dict of {:class:`~nengo:nengo.builder.Signal`: \
                       :class:`.signals.TensorSignal`}
        mapping from ``nengo`` Signals to ``nengo_dl`` TensorSignals (views
        into the base arrays)
    """

    base_arrays = OrderedDict()
    curr_keys = {}
    sig_map = {}
    sig_idxs = {s: i for i, s in enumerate(sigs)}

    # find the non-overlapping partitions of the signals
    breaks = []
    diff = defaultdict(int)
    for ops in plan:
        # note: we don't include Resets, otherwise the big reset block
        # overrides most of the partitioning
        if not isinstance(ops[0], Reset):
            for i in range(len(ops[0].all_signals)):
                op_sigs = [op.all_signals[i].base for op in ops]
                idxs = [sig_idxs[s] for s in op_sigs]
                diff[op_sigs[np.argmin(idxs)]] += 1
                diff[op_sigs[np.argmax(idxs)]] -= 1

    # find the partition points in signal list
    open = 0
    for i, s in enumerate(sigs):
        if s in diff:
            open += diff[s]

        if open == 0:
            breaks += [i + 1]

    # create all the base signals
    for i, sig in enumerate(sigs):
        assert sig not in sig_map
        assert not sig.is_view

        if i in breaks:
            # start a new array for all current bases
            for k in curr_keys:
                curr_keys[k] = object()

        # convert to appropriate dtype
        if sig.dtype in (np.float32, np.float64):
            dtype = float_type
        elif sig.dtype in (np.int32, np.int64):
            dtype = np.int32
        else:
            raise NotImplementedError

        # resize scalars to length 1 vectors
        shape = sig.shape if sig.shape != () else (1,)

        # parameters of signal that affect the base array
        array_params = (dtype, shape[1:], sig.trainable, sig.minibatched)

        # key used to map signals to base arrays
        if array_params not in curr_keys:
            curr_keys[array_params] = object()
        key = curr_keys[array_params]

        initial_value = sig.initial_value.astype(dtype, copy=False)

        # broadcast scalars up to full size
        if initial_value.shape != shape:
            initial_value = np.resize(initial_value, shape)

        if sig.minibatched:
            # duplicate along minibatch dimension
            initial_value = np.tile(
                initial_value[..., None],
                tuple(1 for _ in shape) + (minibatch_size,))

        if key in base_arrays:
            base_arrays[key][0].append(initial_value)
            base_arrays[key][2] += shape[0]
        else:
            base_arrays[key] = [[initial_value], sig.trainable, shape[0]]

        n = base_arrays[key][-1]
        indices = np.arange(n - shape[0], n)

        sig_map[sig] = signals.TensorSignal(
            indices, key, dtype, shape, sig.minibatched, label=sig.name)

        logger.debug("created base signal")
        logger.debug(sig)
        logger.debug(sig_map[sig])

    for key in base_arrays:
        arrs, t, _ = base_arrays[key]
        base_arrays[key] = (np.concatenate(arrs, axis=0), t)

    # add any signal views to the sig_map
    all_views = [sig for ops in plan for op in ops for sig in op.all_signals
                 if sig.is_view]
    for sig in all_views:
        if sig.size == sig.base.size:
            # reshape view
            sig_map[sig] = sig_map[sig.base].reshape(sig.shape)
        else:
            if sig.shape[1:] != sig.base.shape[1:]:
                raise NotImplementedError(
                    "Slicing and reshaping the same signal is not "
                    "supported")

            # slice view
            assert np.all([x == 1 for x in sig.elemstrides[1:]])

            start = sig.elemoffset
            stride = sig.elemstrides[0]
            stop = start + sig.size * stride
            if stop < 0:
                stop = None

            sig_map[sig] = sig_map[sig.base][slice(start, stop, stride)]

    # error checking
    for sig, tensor_sig in sig_map.items():
        if tensor_sig.shape != (sig.shape if sig.shape != () else (1,)):
            raise BuildError("TensorSignal shape %s does not match Signal "
                             "shape %s" % (tensor_sig.shape, sig.shape))

        initial_value = sig.initial_value
        if sig.minibatched:
            initial_value = initial_value[..., None]

        if not np.allclose(base_arrays[tensor_sig.key][0][tensor_sig.indices],
                           initial_value):
            raise BuildError("TensorSignal values don't match Signal values")

    logger.debug("base arrays")
    logger.debug("\n".join([str((k, v[0].dtype, v[0].shape, v[1]))
                            for k, v in base_arrays.items()]))

    return base_arrays, sig_map
