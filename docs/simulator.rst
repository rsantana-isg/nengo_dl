Simulator
=========

This is the class that allows users to access the ``nengo_deeplearning``
backend.  This can be used as a drop-in replacement for ``nengo.Simulator``
(i.e., simply replace any instance of ``nengo.Simulator`` with
``nengo_deeplearning.Simulator`` and everything will continue to function as
normal).  For example:

.. code-block:: python

    import nengo
    import nengo_deeplearning as nengo_dl
    import numpy as np

    with nengo.Network() as net:
        inp = nengo.Node(output=np.sin)
        ens = nengo.Ensemble(50, 1, neuron_type=nengo.LIF())
        nengo.Connection(inp, ens, synapse=0.1)
        p = nengo.Probe(ens)

    with nengo_dl.Simulator(net) as sim:
        sim.run(1.0)

    print(sim.data[p])

In addition, the Simulator exposes features unique to the
``nengo_deeplearning`` backend, such as :meth:`.Simulator.train`.

.. autoclass:: nengo_deeplearning.simulator.Simulator
    :private-members:
    :exclude-members: unsupported, _generate_inputs, _update_probe_data, dt