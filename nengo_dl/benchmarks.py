import time

import matplotlib.pyplot as plt
import nengo
import nengo_ocl
import numpy as np
import tensorflow as tf

import nengo_dl
from nengo_dl import DATA_DIR


def cconv(dimensions, neurons_per_d, neuron_type):
    """Circular convolution (EnsembleArray) benchmark.

    Parameters
    ----------
    dimensions : int
        number of dimensions for vector values
    neurons_per_d : int
        number of neurons to use per vector dimension
    neuron_type : :class:`~nengo:nengo.neurons.NeuronType`
        simulation neuron type

    Returns
    -------
    nengo.Network
        benchmark network
    """

    with nengo.Network(label="cconv", seed=0) as net:
        net.config[nengo.Ensemble].neuron_type = neuron_type
        net.config[nengo.Ensemble].gain = nengo.dists.Choice([1, -1])
        net.config[nengo.Ensemble].bias = nengo.dists.Uniform(-1, 1)

        cconv = nengo.networks.CircularConvolution(neurons_per_d, dimensions)

        inp_a = nengo.Node([0] * dimensions)
        inp_b = nengo.Node([1] * dimensions)
        nengo.Connection(inp_a, cconv.A)
        nengo.Connection(inp_b, cconv.B)

        p = nengo.Probe(cconv.output)

    return net, p


def integrator(dimensions, neurons_per_d, neuron_type):
    """Single integrator ensemble benchmark.

    Parameters
    ----------
    dimensions : int
        number of dimensions for vector values
    neurons_per_d : int
        number of neurons to use per vector dimension
    neuron_type : :class:`~nengo:nengo.neurons.NeuronType`
        simulation neuron type

    Returns
    -------
    nengo.Network
        benchmark network
    """

    with nengo.Network(label="integrator", seed=0) as net:
        net.config[nengo.Ensemble].neuron_type = neuron_type
        net.config[nengo.Ensemble].gain = nengo.dists.Choice([1, -1])
        net.config[nengo.Ensemble].bias = nengo.dists.Uniform(-1, 1)

        integ = nengo.networks.Integrator(0.1, neurons_per_d * dimensions,
                                          dimensions)

        inp = nengo.Node([0] * dimensions)
        nengo.Connection(inp, integ.input)

        p = nengo.Probe(integ.ensemble)

    return net, p


def pes(dimensions, neurons_per_d, neuron_type):
    """PES learning rule benchmark.

    Parameters
    ----------
    dimensions : int
        number of dimensions for vector values
    neurons_per_d : int
        number of neurons to use per vector dimension
    neuron_type : :class:`~nengo:nengo.neurons.NeuronType`
        simulation neuron type

    Returns
    -------
    nengo.Network
        benchmark network
    """

    with nengo.Network(label="pes", seed=0) as net:
        net.config[nengo.Ensemble].neuron_type = neuron_type
        net.config[nengo.Ensemble].gain = nengo.dists.Choice([1, -1])
        net.config[nengo.Ensemble].bias = nengo.dists.Uniform(-1, 1)

        inp = nengo.Node([1] * dimensions)
        pre = nengo.Ensemble(neurons_per_d * dimensions, dimensions)
        post = nengo.Ensemble(neurons_per_d * dimensions, dimensions)
        err = nengo.Node(size_in=dimensions)
        nengo.Connection(inp, pre)
        nengo.Connection(post, err, transform=-1)
        nengo.Connection(inp, err)

        conn = nengo.Connection(pre, post, learning_rule_type=nengo.PES())
        nengo.Connection(err, conn.learning_rule)

        p = nengo.Probe(post)

    return net, p


def compare_backends(raw=False):
    """Compare the run time of different backends across benchmarks and
    a range of parameters.

    Parameters
    ----------
    raw : bool
        if True, run the benchmarks to collect data, otherwise load data from
        file
    """

    benchmarks = [pes, integrator, cconv]
    n_range = [32]
    d_range = [64, 128, 256]
    neuron_types = [nengo.RectifiedLinear, nengo.LIF]
    backends = [nengo_dl, nengo_ocl, nengo]

    if raw:
        data = np.zeros((len(benchmarks), len(n_range), len(d_range),
                         len(neuron_types), len(backends)))

        for i, bench in enumerate(benchmarks):
            for j, neurons in enumerate(n_range):
                for k, dimensions in enumerate(d_range):
                    for l, neuron_type in enumerate(neuron_types):
                        print("-" * 30)
                        print(bench, neurons, dimensions, neuron_type)

                        net, p = bench(dimensions, neurons, neuron_type())
                        model = nengo.builder.Model()
                        model.build(net)

                        for m, backend in enumerate(backends):
                            print(backend)

                            if backend is None:
                                continue
                            elif backend == nengo_dl:
                                kwargs = {"unroll_simulation": 25,
                                          "minibatch_size": None,
                                          "device": "/gpu:0",
                                          "dtype": tf.float32,
                                          }
                            elif backend == nengo:
                                kwargs = {"progress_bar": None,
                                          "optimize": True}
                            elif backend == nengo_ocl:
                                kwargs = {"progress_bar": None}

                            try:
                                # with backend.Simulator(net, **kwargs) as sim:
                                with backend.Simulator(None, model=model,
                                                       **kwargs) as sim:
                                    start = time.time()
                                    sim.run(5)
                                    # reps = 1 if backend == nengo_dl else 50
                                    # for r in range(reps):
                                    #     sim.run(1.0)
                                    data[i, j, k, l, m] = time.time() - start
                                    print("time", data[i, j, k, l, m])
                            except Exception as e:
                                print(backend, "CRASHED")
                                print(e)
                                data[i, j, k, l, m] = np.nan

                            # if backend == nengo:
                            #     canonical = sim.data[p]
                            # else:
                            #     assert np.allclose(canonical, sim.data[p],
                            #                        atol=1e-3)

        np.savez("%s/benchmark_data.npz" % DATA_DIR, data)
    else:
        data = np.load("%s/benchmark_data.npz" % DATA_DIR)["arr_0"]

    bench_names = ["pes", "integrator", "cconv"]
    neuron_names = ["relu", "lif"]

    for j in range(len(neuron_types)):
        f, axes = plt.subplots(1, 3)
        for i in range(len(benchmarks)):
            plt.figure()
            plt.title("%s (%s)" % (bench_names[i], neuron_names[j]))
            plt.plot(d_range, data[i, 0, :, j, 0] / data[i, 0, :, j, 2])
            plt.xlabel("dimensions")
            plt.ylabel("nengo_dl / nengo")

            plt.figure()
            plt.title("%s (%s)" % (bench_names[i], neuron_names[j]))
            plt.plot(d_range, data[i, 0, :, j, 0] / data[i, 0, :, j, 1])
            plt.xlabel("dimensions")
            plt.ylabel("nengo_dl / nengo_ocl")

            axes[i].set_title("%s (%s)" % (bench_names[i], neuron_names[j]))
            axes[i].plot(d_range, data[i, 0, :, j, :])
            axes[i].set_xlabel("dimensions")
            axes[i].set_ylabel("seconds")
            axes[i].legend(["nengo_dl", "nengo_ocl", "nengo"])
            axes[i].set_ylim([0, 100])

    plt.show()


def profiling():
    """Run profiler on one of the benchmarks."""

    # note: in order for GPU profiling to work, you have to manually add
    # ...\CUDA\v8.0\extras\CUPTI\libx64 to your path
    net, p = pes(128, 32, nengo.RectifiedLinear())
    with nengo_dl.Simulator(net, tensorboard=False, unroll_simulation=50,
                            device="/gpu:0") as sim:
        sim.run_steps(150, profile=True)


if __name__ == "__main__":
    # compare_backends(raw=True)
    profiling()
