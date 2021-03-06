NengoDL Simulator
=================

This is the class that allows users to access the ``nengo_dl``
backend.  This can be used as a drop-in replacement for ``nengo.Simulator``
(i.e., simply replace any instance of ``nengo.Simulator`` with
``nengo_dl.Simulator`` and everything will continue to function as
normal).

In addition, the Simulator exposes features unique to the
``nengo_dl`` backend, such as :meth:`.Simulator.train`.

Simulator arguments
-------------------


The ``nengo_dl`` :class:`.Simulator` has a number of optional arguments, beyond
those in :class:`nengo:nengo.Simulator`, which control features specific to
the ``nengo_dl`` backend.  The full class documentation can be viewed
:ref:`below <sim-doc>`; here we will explain the practical usage of these
parameters.

dtype
^^^^^

This specifies the floating point precision to be used for the simulator's
internal computations.  It can be either ``tf.float32`` or ``tf.float64``,
for 32 or 64-bit precision, respectively.  32-bit precision is the default,
as it is faster, will use less memory, and in most cases will not make a
difference in the results of the simulation.  However, if very precise outputs
are required then this can be changed to ``tf.float64``.

device
^^^^^^

This specifies the computational device on which the simulation will
run.  The default is ``None``, which means that operations will be assigned
according to TensorFlow's internal logic (generally speaking, this means that
things will be assigned to the GPU if ``tensorflow-gpu`` is installed,
otherwise everything will be assigned to the CPU).  The device can be set
manually by passing the `TensorFlow device specification
<https://www.tensorflow.org/api_docs/python/tf/Graph#device>`_ to this
parameter.  For example, setting ``device="/cpu:0"`` will force everything
to run on the CPU.  This may be worthwhile for small models, where the extra
overhead of communicating with the GPU outweighs the actual computations.  On
systems with multiple GPUs, ``device="/gpu:0"``/``"/gpu:1"``/etc. will select
which one to use.

unroll_simulation
^^^^^^^^^^^^^^^^^

This controls how many simulation iterations are executed each time through
the outer simulation loop.  That is, we could run 20 timesteps as

.. code-block:: python

    for i in range(20):
        <run 1 step>

or

.. code-block:: python

    for i in range(5):
        <run 1 step>
        <run 1 step>
        <run 1 step>
        <run 1 step>

This is an optimization process known as "loop unrolling", and
``unroll_simulation`` controls how many simulation steps are unrolled.  The
first example above would correspond to ``unroll_simulation=1``, and the
second would be ``unroll_simulation=4``.

Unrolling the simulation will result in faster simulation speed, but increased
build time and memory usage.

In general, unrolling the simulation will have no impact on the output of a
simulation.  The only case in which unrolling may have an impact is if
the number of simulation steps is not evenly divisible by
``unroll_simulation``.  In that case extra simulation steps will be executed,
and then data will be truncated to the correct number of steps.  However, those
extra steps could still change the internal state of the simulation, which
will affect any subsequent calls to ``sim.run``.  So it is recommended that the
number of steps always be evenly divisible by ``unroll_simulation``.


.. _minibatch_size:

minibatch_size
^^^^^^^^^^^^^^

``nengo_dl`` allows a model to be simulated with multiple simultaneous inputs,
processing those values in parallel through the network.  For example, instead
of executing a model three times with three different inputs, the model can
be executed once with those three inputs in parallel.  ``minibatch_size``
specifies how many inputs will be processed at a time.  The default is
``None``, meaning that this feature is not used and only one input will be
processed at a time (as in standard Nengo simulators).

In order to take advantage of the parallel inputs, multiple inputs need to
be passed to :meth:`.Simulator.run` via the ``input_feeds`` argument.  This
is discussed in more detail :ref:`below <sim-run>`.

When using :meth:`.Simulator.train`, this parameter controls how many items
from the training data will be used for each optimization iteration.

tensorboard
^^^^^^^^^^^

If set to ``True``, ``nengo_dl`` will save the structure of the internal
simulation graph so that it can be visualized in `TensorBoard
<https://www.tensorflow.org/get_started/graph_viz>`_.  This is mainly useful
to developers trying to debug the simulator.  This data is stored in the
``<nengo_dl>/data`` folder, and can be loaded via

.. code-block:: bash

    tensorboard --logdir <path/to/nengo_dl>

Data will be organized according to the :class:`~nengo:nengo.Network` label
and run number.

.. _sim-run:

Simulator.run arguments
-----------------------

:meth:`.Simulator.run` (and its variations :meth:`.Simulator.step`/
:meth:`.Simulator.run_steps`) also have some optional parameters beyond those
in the standard Nengo simulator.

input_feeds
^^^^^^^^^^^

This parameter can be used to override the value of any
input :class:`~nengo:nengo.Node` in a model (an input node is defined as
a node with no incoming connections).  For example

.. code-block:: python

    n_steps = 5

    with nengo.Network() as net:
        node = nengo.Node([0])
        p = nengo.Probe(node)

    with nengo_dl.Simulator(net) as sim:
        sim.run_steps(n_steps)

will execute the model in the standard way, and if we check the output of
``node``

.. code-block:: python

    print(sim.data[p])
    >>> [[ 0.] [ 0.] [ 0.] [ 0.] [ 0.]]

we see that it is all zero, as defined.


``input_feeds`` is specified as a
dictionary of ``{my_node: override_value}`` pairs, where ``my_node`` is the
Node to be overridden and ``override_value`` is a numpy array with shape
``(minibatch_size, n_steps, my_node.size_out)`` that gives the Node output
value on each simulation step. For example, if we instead run the model via

.. code-block:: python

    sim.run_steps(n_steps, input_feeds={node: np.ones((1, n_steps, 1))})
    print(sim.data[p])
    >>> [[ 1.] [ 1.] [ 1.] [ 1.] [ 1.]]

we see that the output of ``node`` is all ones, which is the override
value we specified.

``input_feeds`` are usually used in concert with the minibatching feature of
``nengo_dl`` (:ref:`see above <minibatch_size>`).  ``nengo_dl`` allows multiple
inputs to be processed simultaneously, but when we construct a
:class:`~nengo:nengo.Node` we can only specify one value.  For example, if we
use minibatching on the above network

.. code-block:: python

    mini = 3
    with nengo_dl.Simulator(net, minibatch_size=mini) as sim:
        sim.run_steps(n_steps)
        print(sim.data[p])
    >>> [[[ 0.] [ 0.] [ 0.] [ 0.] [ 0.]]
         [[ 0.] [ 0.] [ 0.] [ 0.] [ 0.]]
         [[ 0.] [ 0.] [ 0.] [ 0.] [ 0.]]]

we see that the output is an array of zeros with size
``(mini, n_steps, 1)``.  That is, we simulated 3 inputs
simultaneously, but those inputs all had the same value (the one we defined
when the Node was constructed) so it wasn't very
useful.  To take full advantage of the minibatching we need to override the
node values, so that we can specify a different value for each item in the
minibatch:

.. code-block:: python

    with nengo_dl.Simulator(net, minibatch_size=mini) as sim:
        sim.run_steps(n_steps, input_feeds={
            node: np.zeros((mini, n_steps, 1)) + np.arange(mini)[:, None, None]})
        print(sim.data[p])
    >>> [[[ 0.] [ 0.] [ 0.] [ 0.] [ 0.]]
         [[ 1.] [ 1.] [ 1.] [ 1.] [ 1.]]
         [[ 2.] [ 2.] [ 2.] [ 2.] [ 2.]]]

Here we can see that 3 independent inputs have been processed during the
simulation. In a simple network such as this, minibatching will not make much
difference. But for larger models it will be much more efficient to process
multiple inputs in parallel rather than one at a time.

profile
^^^^^^^

If set to ``True``, profiling data will be collected while the simulation
runs.  This will significantly slow down the simulation, so it should be left
on ``False`` (the default) in most cases.  It is mainly used by developers,
in order to help identify simulation bottlenecks.

Profiling data will be saved to ``<nengo_dl>/data/nengo_dl_profile.json``.  It
can be viewed by opening a Chrome browser, navigating to
`<chrome://tracing>`_ and loading the ``nengo_dl_profile.json`` file.

.. _sim-doc:

Documentation
-------------

.. autoclass:: nengo_dl.simulator.Simulator
    :exclude-members: unsupported, dt