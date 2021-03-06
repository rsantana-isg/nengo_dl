from __future__ import print_function, division

from collections import Mapping
import datetime
import logging
import os
import time
import warnings

from nengo import Process
from nengo.builder import Model
from nengo.exceptions import (ReadonlyError, SimulatorClosed, NengoWarning,
                              SimulationError)
import numpy as np
import tensorflow as tf
from tensorflow.python.client.timeline import Timeline
from tensorflow.python.ops import gradient_checker

from nengo_dl import utils, DATA_DIR
from nengo_dl.tensor_graph import TensorGraph
from nengo_dl.utils import print_and_flush

logger = logging.getLogger(__name__)


class Simulator(object):
    """Simulate network using the ``nengo_dl`` backend.

    Parameters
    ----------
    network : :class:`~nengo:nengo.Network` or None
        a network object to be built and then simulated. If None,
        then a built model must be passed to ``model`` instead
    dt : float, optional
        length of a simulator timestep, in seconds
    seed : int, optional
        seed for all stochastic operators used in this simulator
    model : :class:`~nengo:nengo.builder.Model`, optional
        pre-built model object
    dtype : ``tf.DType``, optional
        floating point precision to use for simulation
    device : None or ``"/cpu:0"`` or ``"/gpu:[0-n]"``, optional
        device on which to execute computations (if None then uses the
        default device as determined by Tensorflow)
    unroll_simulation : int, optional
        unroll simulation loop by explicitly building the given number of
        iterations into the computation graph (improves simulation speed
        but increases build time)
    minibatch_size : int, optional
        the number of simultaneous inputs that will be passed through the
        network
    tensorboard : bool, optional
        if True, save network output in the Tensorflow summary format,
        which can be loaded into Tensorboard
    """

    # unsupported unit tests
    unsupported = [
        ("nengo/tests/test_simulator.py:test_warn_on_opensim_del",
         "nengo_dl raises a different (more visible) warning (see "
         "tests/test_nengo_tests.py:test_warn_on_opensim_del"),

        ("nengo/tests/test_simulator.py:test_signal_init_values",
         "different method required to manually step simulator (see "
         "tests/test_nengo_tests.py:test_signal_init_values"),

        ("nengo/tests/test_simulator.py:test_entry_point",
         "overridden so we can pass custom test simulators (see "
         "tests/test_nengo_tests.py:test_entry_point"),

        ("nengo/tests/test_node.py:test_args",
         "time is passed as np.float32, not a float (see "
         "tests/test_nengo_tests.py:test_args"),

        ("nengo/tests/test_node.py:test_unconnected_node",
         "need to set `unroll_simulation` to ensure node runs the correct "
         "number of times (see "
         "tests/test_nengo_tests.py:test_unconnected_node"),

        # TODO: put this test back in when we bump nengo version
        ("nengo/utils/tests/test_ensemble.py:test_tuning_curves[*",
         "this test is not compatible with numpy>=1.13"),
    ]

    def __init__(self, network, dt=0.001, seed=None, model=None,
                 dtype=tf.float32, device=None, unroll_simulation=1,
                 minibatch_size=None, tensorboard=False,
                 step_blocks="deprecated"):
        self.closed = None
        self.sess = None
        self.tensorboard = tensorboard
        self.unroll = unroll_simulation
        self.minibatch_size = 1 if minibatch_size is None else minibatch_size

        if step_blocks != "deprecated" or isinstance(unroll_simulation, bool):
            # TODO: remove this in 0.5
            warnings.warn(
                "`step_blocks` has been deprecated and will be ignored; "
                "`Simulator(..., unroll_simulation=n)` is now equivalent to "
                "`Simulator(..., unroll_simulation=True, step_blocks=n).",
                DeprecationWarning)

        # TODO: allow the simulator to be called flexibly with/without
        # minibatching
        # TODO: multi-GPU support

        # build model (uses default nengo builder)
        if model is None:
            self.model = Model(dt=float(dt), label="%s, dt=%f" % (network, dt))
        else:
            if dt != model.dt:
                warnings.warn("Model dt (%g) does not match Simulator "
                              "dt (%g)" % (model.dt, dt), NengoWarning)
            self.model = model

        if network is not None:
            print_and_flush("Building network", end="")
            start = time.time()
            self.model.build(network, progress_bar=False)
            print("\rBuilding completed in %s " %
                  datetime.timedelta(seconds=int(time.time() - start)))

        # set up tensorflow graph plan
        self.tensor_graph = TensorGraph(
            self.model, self.dt, unroll_simulation, dtype, self.minibatch_size,
            device)

        self.data = ProbeDict(
            self.model.params,
            {p: (minibatch_size if self.model.sig[p]["in"].minibatched
                 else -1) for p in self.model.probes})

        if seed is None:
            seed = np.random.randint(np.iinfo(np.int32).max)
        self.reset(seed=seed)

    def reset(self, seed=None):
        """Resets the simulator to initial conditions.

        Parameters
        ----------
        seed : int, optional
            if not None, overwrite the default simulator seed with this value
            (note: this becomes the new default simulator seed)
        """

        if self.closed:
            raise SimulatorClosed("Cannot reset closed Simulator.")

        # close old session
        if self.sess is not None:
            self.close()

        if seed is not None:
            self.seed = seed

        self.rng = np.random.RandomState(self.seed)
        # TODO: why is setting the tensorflow seed necessary to make
        # gradient descent training deterministic?
        tf.set_random_seed(self.seed)

        self.input_funcs = {}

        # (re)build graph
        # TODO: do we need to rebuild the graph?
        print_and_flush("Constructing graph", end="")
        start = time.time()
        self.tensor_graph.build(self.rng)
        print("\rConstruction completed in %s " %
              datetime.timedelta(seconds=int(time.time() - start)))

        # output graph description to tensorboard summary
        if self.tensorboard:
            directory = "%s/%s" % (DATA_DIR, self.model.toplevel.label)
            if os.path.isdir(directory):
                run_number = max(
                    [int(x[4:]) for x in os.listdir(directory)
                     if x.startswith("run")]) + 1
            else:
                run_number = 0
            self.summary = tf.summary.FileWriter(
                "%s/run_%d" % (directory, run_number),
                graph=self.tensor_graph.graph)

        # start session
        # note: we need to allow soft placement when using tf.while_loop,
        # because tensorflow pins loop variables to the CPU
        # TODO: switch allow_soft_placement to False once tensorflow
        # adds the RefExit GPU kernel
        config = tf.ConfigProto(
            allow_soft_placement=True,
            log_device_placement=False,
        )
        # TODO: XLA compiling doesn't seem to provide any benefit at the
        # moment, revisit later after tensorflow has developed it further
        # config.graph_options.optimizer_options.global_jit_level = (
        #     tf.OptimizerOptions.ON_1)

        self.sess = tf.Session(graph=self.tensor_graph.graph, config=config)
        self.closed = False

        # initialize variables
        self.soft_reset(include_trainable=True, include_probes=True)

        self.n_steps = 0
        self.time = 0.0
        self.final_bases = [
            x[0] for x in self.tensor_graph.base_arrays_init.values()]

    def soft_reset(self, include_trainable=False, include_probes=False):
        """Resets the internal state of the simulation, but doesn't
        rebuild the graph.

        Parameters
        ----------
        include_trainable : bool, optional
            if True, also reset any training that has been performed on
            network parameters (e.g., connection weights)
        include_probes : bool, optional
            if True, also clear probe data
        """

        init_ops = [self.tensor_graph.local_init_op,
                    self.tensor_graph.global_init_op]
        if include_trainable:
            init_ops.append(self.tensor_graph.trainable_init_op)
        self.sess.run(init_ops)

        if include_probes:
            for p in self.model.probes:
                self.model.params[p] = []
            self.n_steps = 0

    def step(self, **kwargs):
        """Run the simulation for one time step.

        Parameters
        ----------
        kwargs : dict
            see :meth:`.run_steps`
        """

        self.run_steps(1, **kwargs)

    def run(self, time_in_seconds, **kwargs):
        """Simulate for the given length of time.

        Parameters
        ----------
        time_in_seconds : float
            amount of time to run the simulation for
        kwargs : dict
            see :meth:`.run_steps`
        """

        steps = int(np.round(float(time_in_seconds) / self.dt))
        self.run_steps(steps, **kwargs)

    def run_steps(self, n_steps, input_feeds=None, profile=False):
        """Simulate for the given number of steps.

        Parameters
        ----------
        n_steps : int
            the number of simulation steps to be executed
        input_feeds : dict of {:class:`~nengo:nengo.Node`: \
                               :class:`~numpy:numpy.ndarray`}
            override the values of input Nodes with the given data.  arrays
            should have shape ``(sim.minibatch_size, n_steps, node.size_out)``.
        profile : bool, optional
            if True, collect TensorFlow profiling information while the
            simulation is running (this will slow down the simulation)

        Notes
        -----
        If ``unroll_simulation=x`` is specified, and ``n_steps > x``, this will
        repeatedly execute ``x`` timesteps until the the number of steps
        executed is >= ``n_steps``.
        """

        if self.closed:
            raise SimulatorClosed("Simulator cannot run because it is closed.")

        actual_steps = self.unroll * int(np.ceil(n_steps / self.unroll))

        if actual_steps != n_steps:
            warnings.warn(
                "Number of steps (%d) is not an even multiple of "
                "`unroll_simulation` (%d).  Simulation will run for %d steps, "
                "which may have unintended side effects." %
                (n_steps, self.unroll, actual_steps), RuntimeWarning)

        if input_feeds is not None:
            self._check_data(input_feeds, mode="out", check_mini=True,
                             n_steps=n_steps)

        print_and_flush("Simulation started", end="")
        start = time.time()

        if profile:
            run_options = tf.RunOptions(trace_level=tf.RunOptions.FULL_TRACE)
            run_metadata = tf.RunMetadata()
        else:
            run_options = None
            run_metadata = None

        # execute the simulation loop
        try:
            steps_run, probe_data = self.sess.run(
                [self.tensor_graph.steps_run, self.tensor_graph.probe_arrays],
                feed_dict=self._fill_feed(actual_steps, input_feeds,
                                          start=self.n_steps),
                options=run_options, run_metadata=run_metadata)
        except (tf.errors.InternalError, tf.errors.UnknownError) as e:
            if e.op.type == "PyFunc":
                raise SimulationError(
                    "Function '%s' caused an error (see error log above)" %
                    e.op.name)
            else:
                raise e  # pragma: no cover

        # update probe data
        self._update_probe_data(probe_data, self.n_steps, n_steps)

        # update n_steps
        # note: we update n_steps according to the number of steps that the
        # user asked for, not the number of steps that were actually run (
        # in the case of uneven unroll_simulation)
        assert steps_run == actual_steps
        self.n_steps += n_steps
        self.time = self.n_steps * self.dt

        if profile:
            timeline = Timeline(run_metadata.step_stats)
            with open("%s/nengo_dl_profile.json" % DATA_DIR, "w") as f:
                f.write(timeline.generate_chrome_trace_format())

        print("\rSimulation completed in %s" %
              datetime.timedelta(seconds=int(time.time() - start)))

    def train(self, inputs, targets, optimizer, n_epochs=1, objective="mse",
              shuffle=True):
        """Optimize the trainable parameters of the network using the given
        optimization method, minimizing the objective value over the given
        inputs and targets.

        Parameters
        ----------
        inputs : dict of {:class:`~nengo:nengo.Node`: \
                          :class:`~numpy:numpy.ndarray`}
            input values for Nodes in the network; arrays should have shape
            ``(batch_size, n_steps, node.size_out)``
        targets : dict of {:class:`~nengo:nengo.Probe`: \
                           :class:`~numpy:numpy.ndarray`}
            desired output value at Probes, corresponding to each value in
            ``inputs``; arrays should have shape
            ``(batch_size, n_steps, probe.size_in)``
        optimizer : ``tf.train.Optimizer``
            Tensorflow optimizer, e.g.
            ``tf.train.GradientDescentOptimizer(learning_rate=0.1)``
        n_epochs : int, optional
            run training for the given number of epochs (complete passes
            through ``inputs``)
        objective : ``"mse"`` or callable, optional
            the objective to be minimized. passing ``"mse"`` will train with
            mean squared error. a custom function
            ``f(output, target) -> loss`` can be passed that consumes the
            actual output and target output for a probe in ``targets``
            and returns a ``tf.Tensor`` representing the scalar loss value for
            that Probe (loss will be averaged across Probes).
        shuffle : bool, optional
            if True, randomize the data into different minibatches each epoch

        Notes
        -----
        Most deep learning methods require the network to be differentiable,
        which means that trying to train a network with non-differentiable
        elements will result in an error.  Examples of common
        non-differentiable elements include :class:`~nengo:nengo.LIF`,
        :class:`~nengo:nengo.Direct`, or processes/neurons that don't have a
        custom TensorFlow implementation (see
        :class:`.processes.SimProcessBuilder`/
        :class:`.neurons.SimNeuronsBuilder`)
        """

        # TODO: we could save the internal state of the simulator before
        # training and then restore it afterwards, so that calling train
        # doesn't reset a run

        batch_size, n_steps = next(iter(inputs.values())).shape[:2]

        # error checking
        if self.closed:
            raise SimulatorClosed("Simulator cannot be trained because it is "
                                  "closed.")
        self._check_data(inputs, mode="out", n_steps=n_steps)
        self._check_data(targets, mode="in", n_steps=n_steps)

        # check for non-differentiable elements in graph
        # utils.find_non_differentiable(
        #     [self.tensor_graph.invariant_ph[n] for n in inputs],
        #     [self.tensor_graph.probe_arrays[self.model.probes.index(p)]
        #      for p in targets])

        # build optimizer op
        opt_op, opt_slots_init = self.tensor_graph.build_optimizer(
            optimizer, tuple(targets.keys()), objective)

        # initialize any variables that were created by the optimizer
        self.sess.run(opt_slots_init)

        progress = utils.ProgressBar(
            n_epochs * batch_size // self.minibatch_size, "Training")

        for n in range(n_epochs):
            for inp, tar in utils.minibatch_generator(
                    inputs, targets, self.minibatch_size, rng=self.rng,
                    shuffle=shuffle):
                # TODO: set up queue to feed in data more efficiently
                self.soft_reset()

                self.sess.run([opt_op], feed_dict=self._fill_feed(
                    n_steps, inp, tar))

                progress.step()

        self.soft_reset()

    def loss(self, inputs, targets, objective):
        """Compute the loss value for the given objective and inputs/targets.

        Parameters
        ----------
        inputs : dict of {:class:`~nengo:nengo.Node`: \
                          :class:`~numpy:numpy.ndarray`}
            input values for Nodes in the network; arrays should have shape
            ``(batch_size, n_steps, node.size_out)``
        targets : dict of {:class:`~nengo:nengo.Probe`: \
                           :class:`~numpy:numpy.ndarray`}
            desired output value at Probes, corresponding to each value in
            ``inputs``; arrays should have shape
            ``(batch_size, n_steps, probe.size_in)``
        objective : ``"mse"`` or callable
            the objective used to compute loss. passing ``"mse"`` will use
            mean squared error. a custom function
            ``f(output, target) -> loss`` can be passed that consumes the
            actual output and target output for a probe in ``targets``
            and returns a ``tf.Tensor`` representing the scalar loss value for
            that Probe (loss will be averaged across Probes)

        Notes
        -----
        Calling this function will reset all values in the network, so it
        should not be intermixed with calls to :meth:`.Simulator.run`.
        """

        n_steps = next(iter(inputs.values())).shape[1]

        # error checking
        if self.closed:
            raise SimulatorClosed("Loss cannot be computed after simulator is "
                                  "closed.")
        self._check_data(inputs, mode="out", n_steps=n_steps)
        self._check_data(targets, mode="in", n_steps=n_steps)

        # get loss op
        loss = self.tensor_graph.build_loss(objective, tuple(targets.keys()))

        # compute loss on data
        loss_val = 0
        for i, (inp, tar) in enumerate(utils.minibatch_generator(
                inputs, targets, self.minibatch_size, rng=self.rng)):
            self.soft_reset()
            loss_val += self.sess.run(
                loss, feed_dict=self._fill_feed(n_steps, inp, tar))
        self.soft_reset()
        loss_val /= i + 1

        return loss_val

    def _fill_feed(self, n_steps, inputs, targets=None, start=0):
        """Create a feed dictionary containing values for all the placeholder
        inputs in the network, which will be passed to ``tf.Session.run``.

        Parameters
        ----------
        n_steps : int
            the number of execution steps
        input_feeds : dict of {:class:`~nengo:nengo.Node`: \
                               :class:`~numpy:numpy.ndarray`}
            override the values of input Nodes with the given data.  arrays
            should have shape ``(sim.minibatch_size, n_steps, node.size_out)``.
        targets : dict of {:class:`~nengo:nengo.Probe`: \
                           :class:`~numpy:numpy.ndarray`}, optional
            values for target placeholders (only necessary if loss is being
            computed, e.g. when training the network)
        start : int, optional
            initial value of simulator timestep

        Returns
        -------
        dict of {``tf.Tensor``: :class:`~numpy:numpy.ndarray`}
            feed values for placeholder tensors in the network
        """

        # fill in loop variables
        feed_dict = {
            self.tensor_graph.step_var: start,
            self.tensor_graph.stop_var: start + n_steps
        }

        # fill in values for base variables from previous run
        # TODO: remove this if we're sure we're not going back to the tensor
        # approach
        feed_dict.update(
            {k: v for k, v in zip(
                self.tensor_graph.base_vars,
                self.final_bases) if k.op.type == "Placeholder"})

        # fill in input values
        tmp = self._generate_inputs(inputs, n_steps)
        feed_dict.update(tmp)

        # fill in target values
        if targets is not None:
            feed_dict.update(
                {self.tensor_graph.target_phs[p]: np.moveaxis(t, 0, -1)
                 for p, t in targets.items()})

        return feed_dict

    def _generate_inputs(self, input_feeds, n_steps):
        """Generate inputs for the network (the output values of each Node with
        no incoming connections).

        Parameters
        ----------
        input_feeds : dict of {:class:`~nengo:nengo.Node`: \
                               :class:`~numpy:numpy.ndarray`}
            override the values of input Nodes with the given data.  arrays
            should have shape ``(sim.minibatch_size, n_steps, node.size_out)``.
        n_steps : int
            number of simulation timesteps for which to generate input data
        """

        if input_feeds is None:
            input_feeds = {}

        feed_vals = {}
        for n in self.tensor_graph.invariant_inputs:
            # if the output signal is not in sig map, that means no operators
            # use the output of this node. similarly, if node.size_out is 0,
            # the node isn't producing any output values.
            using_output = (
                self.model.sig[n]["out"] in self.tensor_graph.sig_map and
                n.size_out > 0)

            if (not isinstance(n.output, np.ndarray) and
                    n.output not in self.input_funcs):
                if isinstance(n.output, Process):
                    self.input_funcs[n.output] = n.output.make_step(
                        (n.size_in,), (n.size_out,), self.dt,
                        n.output.get_rng(self.rng))
                elif n.size_out > 0:
                    self.input_funcs[n.output] = utils.align_func(
                        (n.size_out,), self.tensor_graph.dtype)(n.output)
                else:
                    self.input_funcs[n.output] = n.output

            if using_output:
                if n in input_feeds:
                    # move minibatch dimension to the end
                    feed_val = np.moveaxis(input_feeds[n], 0, -1)
                elif isinstance(n.output, np.ndarray):
                    feed_val = np.tile(n.output[None, :, None],
                                       (n_steps, 1, self.minibatch_size))
                else:
                    func = self.input_funcs[n.output]

                    feed_val = []
                    for i in range(self.n_steps + 1,
                                   self.n_steps + n_steps + 1):
                        # note: need to copy the output of func, as func
                        # may mutate its outputs in-place on subsequent calls
                        feed_val += [np.array(func(i * self.dt))]

                    feed_val = np.stack(feed_val, axis=0)
                    feed_val = np.tile(feed_val[..., None],
                                       (1, 1, self.minibatch_size))

                feed_vals[self.tensor_graph.invariant_ph[n]] = feed_val
            elif not isinstance(n.output, np.ndarray):
                # note: we still call the function even if the output
                # is not being used, because it may have side-effects
                func = self.input_funcs[n.output]
                for i in range(self.n_steps + 1, self.n_steps + n_steps + 1):
                    func(i * self.dt)

        return feed_vals

    def _update_probe_data(self, probe_data, start, n_steps):
        """Updates the stored probe data (since the last reset) with the data
        from the latest run.

        Downsamples the probe data returned from tensorflow (from every
        simulation timestep) according to probe `sample_every` and the number
        of steps run.

        Parameters
        ----------
        probe_data : list of `np.ndarray`
            probe data from every timestep
        start : int
            the simulation timestep at which probe data starts
        n_steps : int
            the number of timesteps over which we want to collect data
        """

        # remove any extra timesteps (due to `unroll_simulation` mismatch)
        probe_data = [p[:n_steps] for p in probe_data]

        for i, p in enumerate(self.model.probes):
            if p.sample_every is not None:
                # downsample probe according to `sample_every`
                period = p.sample_every / self.dt
                steps = np.arange(start, start + n_steps)
                probe_data[i] = probe_data[i][(steps + 1) % period < 1]

            # update stored probe data
            self.model.params[p] += [probe_data[i]]

    def save_params(self, path, include_local=False):
        """Save network parameters to the given ``path``.

        Parameters
        ----------
        path : str
            filepath of parameter output file
        include_local : bool, optional
            if True, also save local (non-trainable) network variables
        """
        if self.closed:
            raise SimulationError("Simulation has been closed, cannot save "
                                  "parameters")

        with self.tensor_graph.graph.as_default():
            vars = tf.global_variables()
            if include_local:
                vars.extend(tf.local_variables())

            path = tf.train.Saver(vars).save(self.sess, path)

        logger.info("Model parameters saved to %s", path)

    def load_params(self, path, include_local=False):
        """Load network parameters from the given ``path``.

        Parameters
        ----------
        path : str
            filepath of parameter input file
        include_local : bool, optional
            if True, also load local (non-trainable) network variables
        """
        if self.closed:
            raise SimulationError("Simulation has been closed, cannot load "
                                  "parameters")

        with self.tensor_graph.graph.as_default():
            vars = tf.global_variables()
            if include_local:
                vars.extend(tf.local_variables())

            tf.train.Saver(vars).restore(self.sess, path)

        logger.info("Model parameters loaded from %s", path)

    def print_params(self, msg=None):
        """Print current values of trainable network parameters.

        Parameters
        ----------
        msg : str, optional
            title for print output, useful to differentiate multiple print
            calls
        """

        if self.closed:
            raise SimulationError("Simulation has been closed, cannot print "
                                  "parameters")

        param_sigs = {k: v for k, v in self.tensor_graph.sig_map.items()
                      if k.trainable}
        keys = list(self.tensor_graph.signals.bases.keys())
        params = {v.key: self.tensor_graph.base_vars[keys.index(v.key)]
                  for v in param_sigs.values()}

        param_vals = self.sess.run(params)

        print("%s:" % "Parameters" if msg is None else msg)
        for sig, tens in param_sigs.items():
            print("-" * 10)
            print(sig)
            print(param_vals[tens.key][tens.indices])

    def close(self):
        """Close the simulation, freeing resources.

        Notes
        -----
        The simulation cannot be restarted after it is closed.  This is not a
        technical limitation, just a design decision made for all Nengo
        simulators.
        """

        if not self.closed:
            self.sess.close()
            self.closed = True
            self.sess = None

            # note: we use getattr in case it crashes before the summary
            # object is created
            if getattr(self, "summary", None) is not None:
                self.summary.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.close()

    @property
    def dt(self):
        """(float) The time step of the simulator."""
        return self.model.dt

    @dt.setter
    def dt(self, dummy):
        raise ReadonlyError(attr='dt', obj=self)

    def __del__(self):
        """Raise a RuntimeWarning if the Simulator is deallocated while open.
        """

        if self.closed is not None and not self.closed:
            warnings.warn(
                "Simulator with model=%s was deallocated while open. "
                "Simulators should be closed manually to ensure resources "
                "are properly freed." % self.model, RuntimeWarning)
            self.close()

    def trange(self, dt=None):
        """Create a vector of times matching probed data.

        Note that the range does not start at 0 as one might expect, but at
        the first timestep (i.e., ``dt``).

        Parameters
        ----------
        dt : float, optional
            the sampling period of the probe to create a range for;
            if None, the simulator's ``dt`` will be used.
        """

        dt = self.dt if dt is None else dt
        n_steps = int(self.n_steps * (self.dt / dt))
        return dt * np.arange(1, n_steps + 1)

    def check_gradients(self, outputs=None, atol=1e-5, rtol=1e-3):
        """Perform gradient checks for the network (used to verify that the
        analytic gradients are correct).

        Raises a simulation error if the difference between analytic and
        numeric gradient is greater than ``atol + rtol * numeric_grad``
        (elementwise).

        Parameters
        ----------
        outputs : ``tf.Tensor`` or list of ``tf.Tensor`` or \
                  list of :class:`~nengo:nengo.Probe`
            compute gradients wrt this output (if None, computes wrt each
            output probe)
        atol : float, optional
            absolute error tolerance
        rtol : float, optional
            relative (to numeric grad) error tolerance

        Notes
        -----
        Calling this function will reset all values in the network, so it
        should not be intermixed with calls to :meth:`.Simulator.run`.
        """

        delta = 1e-3
        n_steps = self.unroll * 2

        feed = self._fill_feed(
            n_steps, {n: np.zeros((self.minibatch_size, n_steps, n.size_out))
                      for n in self.tensor_graph.invariant_inputs},
            {p: np.zeros((self.minibatch_size, n_steps, p.size_in))
             for p in self.tensor_graph.target_phs})

        if outputs is None:
            # note: the x + 0 is necessary because `gradient_checker`
            # doesn't work properly if the output variable is a tensorarray
            outputs = [x + 0 for x in self.tensor_graph.probe_arrays]
        elif isinstance(outputs, tf.Tensor):
            outputs = [outputs]
        else:
            outputs = [
                self.tensor_graph.probe_arrays[self.model.probes.index(p)] + 0
                for p in outputs]

        # check gradient wrt inp
        for node, inp in self.tensor_graph.invariant_ph.items():
            inp_shape = inp.get_shape().as_list()
            inp_shape = [n_steps if x is None else x for x in inp_shape]
            inp_tens = self.tensor_graph.invariant_ph[node]
            feed[inp_tens] = np.ascontiguousarray(feed[inp_tens])
            inp_val = np.ravel(feed[inp_tens])
            for out in outputs:
                out_shape = out.get_shape().as_list()
                out_shape = [n_steps if x is None else x for x in out_shape]

                # we need to compute the numeric jacobian manually, to
                # correctly handle variables (tensorflow doesn't expect
                # state ops in `compute_gradient`, because it doesn't define
                # gradients for them)
                numeric = np.zeros((np.prod(inp_shape, dtype=np.int32),
                                    np.prod(out_shape, dtype=np.int32)))

                for i in range(numeric.shape[0]):
                    self.soft_reset()
                    inp_val[i] = delta
                    plus = self.sess.run(out, feed_dict=feed)

                    self.soft_reset()
                    inp_val[i] = -delta
                    minus = self.sess.run(out, feed_dict=feed)

                    numeric[i] = np.ravel((plus - minus) / (2 * delta))

                    inp_val[i] = 0

                self.soft_reset()

                dx, dy = gradient_checker._compute_dx_and_dy(
                    inp, out, out_shape)

                with self.sess.as_default():
                    analytic = gradient_checker._compute_theoretical_jacobian(
                        inp, inp_shape, np.zeros(inp_shape), dy, out_shape, dx,
                        extra_feed_dict=feed)

                if np.any(np.isnan(analytic)) or np.any(np.isnan(numeric)):
                    raise SimulationError("NaNs detected in gradient")
                fail = abs(analytic - numeric) >= atol + rtol * abs(numeric)
                if np.any(fail):
                    raise SimulationError(
                        "Gradient check failed for input %s and output %s\n"
                        "numeric values:\n%s\n"
                        "analytic values:\n%s\n" % (node, out, numeric[fail],
                                                    analytic[fail]))

        self.soft_reset()

        logger.info("Gradient check passed")

    def _check_data(self, data, mode="out", check_mini=False, n_steps=None):
        """Performs error checking on simulation data.

        Parameters
        ----------
        data : dict of {``nengo object``: :class:`~numpy:numpy.ndarray`}
            array of data associated with given object in model
        mode : "in" or "out", optional
            whether this data corresponds to the input or output of the
            nengo object
        check_mini : bool, optional
            if True, also validate minibatch_size
        n_steps : int, optional
            number of simulation steps (if None, will use sim.unroll)
        """

        # TODO: error if object not in invariant_inputs or model.probes

        for n, x in data.items():
            shape = (self.minibatch_size if check_mini else None,
                     self.unroll if n_steps is None else n_steps,
                     n.size_out if mode == "out" else n.size_in)

            if any(x is not None and x != y for x, y in zip(shape, x.shape)):
                raise SimulationError(
                    "Shape of data array %s does not match expected shape "
                    "(%s, %s, %s.size_%s) -> %s" %
                    (x.shape,
                     "sim.minibatch_size" if check_mini else "_",
                     "sim.unroll" if n_steps is None else "n_steps",
                     n, mode, shape))


class ProbeDict(Mapping):
    """Map from :class:`~nengo:nengo.Probe` -> :class:`~numpy:numpy.ndarray`,
    used to access output of the model after simulation.

    This is more like a view on the dict that the simulator manipulates.
    However, for speed reasons, the simulator uses Python lists,
    and we want to return NumPy arrays. Additionally, this mapping
    is readonly, which is more appropriate for its purpose.

    Parameters
    ----------
    raw : dict of {:class:`~nengo:nengo.Probe`: \
                   list of :class:`~numpy:numpy.ndarray`}
        the raw probe output from the simulator (a list of arrays containing
        the output from each ``run_steps`` execution segment)
    minibatches : dict of {:class:`~nengo:nengo.Probe`: int or None}
        the minibatch size for each probe in the dictionary (or -1 if the
        probed signal does not have a minibatch dimension)

    Notes
    -----
    ProbeDict should never be created/accessed directly by the user, but rather
    via ``sim.data`` (which is an instance of ProbeDict).
    """

    def __init__(self, raw, minibatches):
        self.raw = raw
        self.minibatches = minibatches

        # TODO: add cache back in?

    def __getitem__(self, key):
        rval = self.raw[key]

        if isinstance(rval, list):
            # combine data from run_steps iterations
            rval = np.concatenate(rval, axis=0)

            if self.minibatches[key] != -1:
                if self.minibatches[key] is None:
                    # get rid of batch dimension
                    rval = rval[..., 0]
                else:
                    # move batch dimension to front
                    rval = np.moveaxis(rval, -1, 0)

            rval.setflags(write=False)

        return rval

    def __iter__(self):
        return iter(self.raw)

    def __len__(self):
        return len(self.raw)
