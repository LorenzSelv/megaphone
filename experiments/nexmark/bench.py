#!/usr/bin/env python3

import sys, math, os
import argparse
import shlex
from collections import namedtuple, defaultdict
from HopcroftKarp import HopcroftKarp

from patterns import InitialPattern, SuddenMigrationPattern, FluidMigrationPattern, BatchedFluidMigrationPattern, PatternGenerator
import experiments
from experiments import eprint, ensure_dir, current_commit, run_cmd, wait_all

argparser = argparse.ArgumentParser(description='Process some integers.')
argparser.add_argument("--clusterpath", help='the path of this repo on the cluster machines', required=True)
argparser.add_argument("--serverprefix", help='an ssh username@server prefix, e.g. andreal@fdr, the server number will be appended', required=True)
argparser.add_argument("--dryrun", help='don\'t actually do anything', action='store_true')
argparser.add_argument("--machineid", help='choose a machine for machine-local experiments (can be overridden per-experiment)', type=int)
argparser.add_argument("--baseid", help='choose the first machine for this experiment (can be overridden per-experiment)', type=int)
argparser.add_argument("--build-only", help='Only build the experiment\'s binary', action='store_true')
argparser.add_argument("--no-build", help='Do not build the experiment\'s binary', action='store_true')
# argparser.add_argument("-c")
args = argparser.parse_args()
experiments.cluster_src_path = args.clusterpath
experiments.cluster_server = args.serverprefix
dryrun = args.dryrun
single_machine_id = args.machineid
base_machine_id = args.baseid
run = not args.build_only
build = not args.no_build
if not run and not build:
    eprint("Cannot select --build-only and --no-build at the same time")
    sys.exit(1)
if dryrun:
    eprint("dry-run")

# - Generic interface to run benchmark with a configuration
# - Runner to run experiments remotely
# - Specific benchmarks to execute

class Experiment(object):

    def __init__(self, name, **config):
        self._name = name
        self._directory_name = self.compute_directory_name(config)
        self._migration = config.pop("migration")
        self._bin_shift = config.pop("bin_shift")
        self._workers = config.pop("workers")
        self._processes = config.pop("processes")
        self._rate = config.pop("rate")
        self._duration = config.get("duration")
        self._initial_config = config.pop("initial_config")
        self._final_config = config.pop("final_config")
        self._machine_local = config.pop("machine_local")
        self._fake_stateful = config.pop("fake_stateful", False)

        self.single_machine_id = single_machine_id
        self.base_machine_id = base_machine_id

        self.binary = config.pop("binary")

        self.other_args = " ".join(map(lambda kv: "--{} {}".format(kv[0], shlex.quote(str(kv[1]))), iter(config.items())))

    def compute_directory_name(self, config):
        keys = sorted(config.keys())
        kv_pairs = []
        for key in keys:

            value = config[key]
            if isinstance(value, (str, int)):
                kv_pairs.append((key, value))
            else:
                kv_pairs.append((key, "|".join(value)))
        configuration = "+".join(map(lambda p: "{}={}".format(p[0], p[1]), kv_pairs))
        return "{}/{}".format(current_commit, configuration)

    def get_directory_name(self):
        return self._directory_name

    def get_features(self):
        features = ["dynamic_scaling_mechanism/bin-{}".format(self._bin_shift)]
        if self._fake_stateful:
            features.append("fake_stateful")
        return features

    def get_features_encoded(self):
        return "+".join(map(lambda s: s.replace("/", "@"), sorted(self.get_features())))

    def get_setup_directory_name(self):
        return "{}/{}".format("setups", self.get_directory_name())

    def get_result_directory_name(self):
        return "{}/{}".format("results", self.get_directory_name())

    def get_build_directory_name(self):
        return "{}/{}".format("build", self.get_features_encoded())

    def get_setup_file_name(self, name):
        return "{}/{}".format(self.get_setup_directory_name(), name)

    def get_result_file_name(self, name, process):
        return "{}/{}.{}".format(self.get_result_directory_name(), name, process)

    def get_result_done_marker(self):
        return "{}/done".format(self.get_result_directory_name())

    def commands(self):
        print(vars(self))
        migration_pattern_file_name = self.get_setup_file_name("migration_pattern")

        initial_pattern = InitialPattern(self._bin_shift, self._workers * self._processes)

        if self._migration == "sudden":
            pattern = SuddenMigrationPattern
        elif self._migration == "fluid":
            pattern = FluidMigrationPattern
        elif self._migration == "batched":
            pattern = BatchedFluidMigrationPattern
        else:
            raise ValueError("Unknown migration pattern: {}".format(self._migration))

        if self._initial_config == "uniform":
            initial_config = initial_pattern.generate_uniform()
        elif self._initial_config == "uniform_skew":
            initial_config = initial_pattern.generate_uniform_skew()
        elif self._initial_config == "half":
            initial_config = initial_pattern.generate_half()

        if self._final_config == "uniform":
            final_config = initial_pattern.generate_uniform()
        elif self._final_config == "uniform_skew":
            final_config = initial_pattern.generate_uniform_skew()
        elif self._final_config == "half":
            final_config = initial_pattern.generate_half()

        ensure_dir(self.get_setup_directory_name())

        with open(migration_pattern_file_name, "w") as f:
            eprint("writing migration pattern to {}".format(migration_pattern_file_name))
            pattern_generator = PatternGenerator(pattern, initial_config, final_config)
            pattern_generator.write_pattern(f, pattern_generator._initial_pattern, 0)
            pattern_generator.write(f, self._duration * 1000000000)

        hostfile_file_name = self.get_setup_file_name("hostfile")
        with open(hostfile_file_name, 'w') as f:
            eprint("writing hostfile to {}".format(hostfile_file_name))
            if self._machine_local:
                assert(self.single_machine_id is not None)
                for p in range(0, self._processes):
                    f.write("{}{}.ethz.ch:{}\n".format(experiments.cluster_server.split('@')[1], self.single_machine_id, 3210 + p))
            else:
                assert(self.base_machine_id is not None)
                for p in range(0, self._processes):
                    f.write("{}{}.ethz.ch:3210\n".format(experiments.cluster_server.split('@')[1], self.base_machine_id + p))


        if self._machine_local:
            assert(self.single_machine_id is not None)
            def make_command(p):
                params = {
                    'binary': self.binary,
                    'dir': self.get_build_directory_name(),
                    'rate': self._rate // (self._processes * self._workers),
                    'cwd': "../experiments/nexmark",
                    'migration': migration_pattern_file_name,
                    'hostfile': hostfile_file_name,
                    'processes': self._processes,
                    'p': p,
                    'workers': self._workers,
                    'ARGS': self.other_args,
                }
                return "RUST_BACKTRACE=1 /mnt/SG/strymon/local/bin/hwloc-bind socket:{p}.pu:even -- ./{dir}/release/{binary} --migration {cwd}/{migration} --rate {rate} {ARGS} -- --hostfile {cwd}/{hostfile} -n {processes} -p {p} -w {workers}".format(**params)
            commands = [(self.single_machine_id, make_command(p), self.get_result_file_name("stdout", p), self.get_result_file_name("stderr", p)) for p in range(0, self._processes)]
            return commands
        else:
            def make_command(p):
                params = {
                    'binary': self.binary,
                    'dir': self.get_build_directory_name(),
                    'rate': self._rate // (self._processes * self._workers),
                    'cwd': "../experiments/nexmark",
                    'migration': migration_pattern_file_name,
                    'hostfile': hostfile_file_name,
                    'processes': self._processes,
                    'p': p,
                    'workers': self._workers,
                    'ARGS': self.other_args,
                }
                # return "RUST_BACKTRACE=1 perf record -o perf.{p} --switch-output=2s -- /mnt/SG/strymon/local/bin/hwloc-bind socket:0.pu:even -- ./{dir}/release/{binary} --migration {cwd}/{migration} --rate {rate} {ARGS} -- --hostfile {cwd}/{hostfile} -n {processes} -p {p} -w {workers}".format(**params)
                return "RUST_BACKTRACE=1 /mnt/SG/strymon/local/bin/hwloc-bind socket:0.pu:even -- ./{dir}/release/{binary} --migration {cwd}/{migration} --rate {rate} {ARGS} -- --hostfile {cwd}/{hostfile} -n {processes} -p {p} -w {workers}".format(**params)
            commands = [(self.base_machine_id + p, make_command(p), self.get_result_file_name("stdout", p), self.get_result_file_name("stderr", p)) for p in range(0, self._processes)]
            return commands

    def run_commands(self, run=True, build=True):
        eprint("running experiment, results in {}".format(self.get_result_directory_name()), level="info")
        marker_file = self.get_result_done_marker()
        if os.path.exists(marker_file):
            eprint("not running experiment")
            return
        if not dryrun:
            ensure_dir(self.get_result_directory_name())
        if build:
            build_cmd = ". ~/eth_proxy.sh && cargo rustc --target-dir {} --bin {} --release --no-default-features --features {}".format(
                shlex.quote(self.get_build_directory_name()), self.binary, shlex.quote(" ".join(self.get_features())))
            if self._machine_local:
                run_cmd(build_cmd, node=self.single_machine_id, dryrun=dryrun)
            else:
                run_cmd(build_cmd, node=self.base_machine_id, dryrun=dryrun)
        if run:
            wait_all([run_cmd(c, redirect=r, stderr=stderr, background=True, node=p, dryrun=dryrun) for p, c, r, stderr in self.commands()])
            if not dryrun:
                open(marker_file, 'a').close()

duration=300

default_bin_shift = 8

def non_migrating(group, groups=4):
    workers = 8
    all_queries = ["q0", "q1", "q2", "q3", "q4", "q5", "q6", "q7", "q8"]
    queries = all_queries[group * len(all_queries) // groups:(group + 1) * len(all_queries) // groups]
    for rate in [x * 200000 for x in [1, 2, 4, 8, 16, 32]]:
        for query in queries:
            experiment = Experiment(
                    "non_migrating",
                    binary="timely",
                    duration=duration,
                    rate=rate,
                    queries=[query,],
                    migration="fluid",
                    bin_shift=default_bin_shift,
                    workers=workers,
                    processes=2,
                    initial_config="uniform",
                    final_config="uniform",
                    fake_stateful=False,
                    machine_local=True,
                    time_dilation=1)
            experiment.single_machine_id = group + 1
            experiment.run_commands(run, build)

def exploratory_migrating(group, groups=4):
    workers = 8
    all_queries = ["q0-flex", "q1-flex", "q2-flex", "q3-flex", "q4-flex", "q5-flex", "q6-flex", "q7-flex", "q8-flex"]
    queries = all_queries[group * len(all_queries) // groups:(group + 1) * len(all_queries) // groups]
    for rate in [x * 200000 for x in [1, 2, 4, 8, 32]]:
        for migration in ["sudden", "fluid", "batched"]:
            for query in queries:
                experiment = Experiment(
                    "migrating-mp",
                    binary="timely",
                    duration=duration,
                    rate=rate,
                    queries=query,
                    migration=migration,
                    bin_shift=default_bin_shift,
                    workers=workers,
                    processes=2,
                    initial_config="uniform",
                    final_config="uniform_skew",
                    fake_stateful=False,
                    machine_local=True,
                    time_dilation=1)
                experiment.single_machine_id = group + 1
                experiment.run_commands(run, build)

def exploratory_baseline(group, groups=4):
    workers = 8
    all_queries = ["q0-flex", "q1-flex", "q2-flex", "q3-flex", "q4-flex", "q5-flex", "q6-flex", "q7-flex", "q8-flex"]
    queries = all_queries[group * len(all_queries) // groups:(group + 1) * len(all_queries) // groups]
    for rate in [x * 200000 for x in [1, 2, 4, 8, 16, 32]]:
        for migration in ["sudden"]:
            for query in queries:
                experiment = Experiment(
                    "migrating-bl",
                    binary="timely",
                    duration=duration,
                    rate=rate,
                    queries=query,
                    migration=migration,
                    bin_shift=default_bin_shift,
                    workers=workers,
                    processes=2,
                    initial_config="uniform",
                    final_config="uniform_skew",
                    fake_stateful=True,
                    machine_local=True,
                    time_dilation=1)
                experiment.single_machine_id = group + 1
                experiment.run_commands(run, build)

def exploratory_migrating_single_process(group, groups=4):
    workers = 8
    all_queries = ["q0-flex", "q1-flex", "q2-flex", "q3-flex", "q4-flex", "q5-flex", "q6-flex", "q7-flex", "q8-flex"]
    queries = all_queries[group * len(all_queries) // groups:(group + 1) * len(all_queries) // groups]
    for rate in [x * 25000 for x in [1, 2, 4, 8]]:
        for migration in ["sudden", "fluid", "batched"]:
            for query in queries:
                experiment = Experiment(
                        "migrating-sp",
                        binary="timely",
                        duration=duration,
                        rate=rate,
                        queries=query,
                        migration=migration,
                        bin_shift=default_bin_shift,
                        workers=workers,
                        processes=1,
                        initial_config="uniform",
                        final_config="uniform_skew",
                        fake_stateful=False,
                        machine_local=True,
                        time_dilation=1)
                experiment.single_machine_id = group + 1
                experiment.run_commands(run, build)

def exploratory_bin_shift(group, groups=4):
    workers = 8
    processes = 2
    all_queries = ["q0-flex", "q1-flex", "q2-flex", "q3-flex", "q4-flex", "q5-flex", "q6-flex", "q7-flex", "q8-flex"]
    queries = all_queries[group * len(all_queries) // groups:(group + 1) * len(all_queries) // groups]
    for rate in [x * 200000 for x in [1, 2, 4, 8, 16, 32]]:
        for bin_shift in range(int(math.log2(workers * processes)), 21):
            for query in queries:
                experiment = Experiment(
                    "bin_shift",
                    binary="timely",
                    duration=duration,
                    rate=rate,
                    queries=query,
                    migration="fluid",
                    bin_shift=bin_shift,
                    workers=workers,
                    processes=processes,
                    initial_config="uniform",
                    final_config="uniform_skew",
                    fake_stateful=False,
                    machine_local=True,
                    time_dilation=1)
                experiment.single_machine_id = group + 1
                experiment.run_commands(run, build)



def exploratory_migrating_mm(group, groups=1):
    workers = 4
    all_queries = ["q0-flex", "q1-flex", "q2-flex", "q3-flex", "q4-flex", "q5-flex", "q6-flex", "q7-flex", "q8-flex"]
    queries = all_queries[group * len(all_queries) // groups:(group + 1) * len(all_queries) // groups]
    for rate in [x * 200000 for x in [1, 2, 4, 8, 16, 32]]:
        for migration in ["sudden", "fluid", "batched"]:
            for query in queries:
                experiment = Experiment(
                    "migrating-mm",
                    binary="timely",
                    duration=duration,
                    rate=rate,
                    queries=query,
                    migration=migration,
                    bin_shift=default_bin_shift,
                    workers=workers,
                    processes=4,
                    initial_config="uniform",
                    final_config="uniform_skew",
                    fake_stateful=False,
                    machine_local=False,
                    time_dilation=1)
                experiment.base_machine_id = group*groups + 1
                experiment.run_commands(run, build)

def migrating_time_dilation(group, groups=1):
    workers = 4
    # Time dilation: Two 12h-windows in duration seconds plus padding
    time_dilation = int(12*60*60/(duration * 2)*1.1)
    base_rate = int(200000 / time_dilation)
    all_queries = ["q0-flex", "q1-flex", "q2-flex", "q3-flex", "q4-flex", "q5-flex", "q6-flex", "q7-flex", "q8-flex"]
    queries = all_queries[group * len(all_queries) // groups:(group + 1) * len(all_queries) // groups]
    for rate in [x * base_rate for x in [1, 2, 4, 8, 16, 32]]:
        for migration in ["sudden", "fluid", "batched"]:
            for query in queries:
                experiment = Experiment(
                    "migrating-td",
                    binary="timely",
                    duration=duration,
                    rate=rate,
                    queries=query,
                    migration=migration,
                    bin_shift=default_bin_shift,
                    workers=workers,
                    processes=4,
                    initial_config="uniform",
                    final_config="uniform_skew",
                    fake_stateful=False,
                    machine_local=True,
                    time_dilation=time_dilation)
                experiment.single_machine_id = group + 1
                experiment.run_commands(run, build)

# Word count experiments
duration = 60

def wc_bin_shift(group, groups=1):
    workers = 4
    processes = 4
    all_rates = [x * 100000 for x in [1, 4, 16, 64, 256, 512]]
    rates = all_rates[group * len(all_rates) // groups:(group + 1) * len(all_rates) // groups]
    for rate in rates:
        for domain in [1000000 * x for x in [1, 4, 16, 64]]:
            for bin_shift in range(int(math.log2(workers * processes)), 21):
                    experiment = Experiment(
                        "wc-migrating-bin_shift-mm",
                        binary="word_count",
                        duration=duration,
                        rate=rate,
                        migration="sudden",
                        bin_shift=bin_shift,
                        workers=workers,
                        processes=processes,
                        initial_config="uniform",
                        final_config="uniform",
                        fake_stateful=False,
                        machine_local=False,
                        backend="hashmap",
                        domain=domain)
                    experiment.base_machine_id = group*groups + 1
                    experiment.run_commands(run, build)

            # Fake stateful
            experiment = Experiment(
                "wc-migrating-fake-mp",
                binary="word_count",
                duration=duration,
                rate=rate,
                migration="sudden",
                bin_shift=default_bin_shift,
                workers=workers,
                processes=processes,
                initial_config="uniform",
                final_config="uniform",
                fake_stateful=True,
                machine_local=False,
                backend="hashmap",
                domain=domain)
            experiment.base_machine_id = group*groups + 1
            experiment.run_commands(run, build)

    all_rates = [x * 100000 for x in [16]]
    rates = all_rates[group * len(all_rates) // groups:(group + 1) * len(all_rates) // groups]
    for rate in rates:
        for domain in [1000000 * x for x in [1, 4, 16, 64]]:
            for bin_shift in range(int(math.log2(workers * processes)), 15, 2):
                for migration in ["sudden", "fluid", "batched"]:
                    experiment = Experiment(
                        "wc-migrating-mp",
                        binary="word_count",
                        duration=duration,
                        rate=rate,
                        migration=migration,
                        bin_shift=bin_shift,
                        workers=workers,
                        processes=processes,
                        initial_config="uniform",
                        final_config="uniform_skew",
                        fake_stateful=False,
                        machine_local=False,
                        backend="hashmap",
                        domain=domain)
                    experiment.base_machine_id = group*groups + 1
                    experiment.run_commands(run, build)

def wc_bin_shift_vec(group, groups=1):
    workers = 4
    processes = 4
    all_rates = [x * 1000000 for x in [1, 4, 16, 64, 256]]
    rates = all_rates[group * len(all_rates) // groups:(group + 1) * len(all_rates) // groups]
    for rate in rates:
        for domain in [1000000 * x for x in [1, 4, 16, 64, 256, 1024, 2048]]:
            for bin_shift in range(int(math.log2(workers * processes)), 21):
                experiment = Experiment(
                    "wc-migrating-mp",
                    binary="word_count",
                    duration=duration,
                    rate=rate,
                    migration="sudden",
                    bin_shift=bin_shift,
                    workers=workers,
                    processes=processes,
                    initial_config="uniform",
                    final_config="uniform",
                    fake_stateful=False,
                    machine_local=False,
                    domain=domain,
                    backend="vec")
                experiment.base_machine_id = group*groups + 1
                experiment.run_commands(run, build)

            # Fake stateful
            experiment = Experiment(
                "wc-migrating-mp",
                binary="word_count",
                duration=duration,
                rate=rate,
                migration="sudden",
                bin_shift=default_bin_shift,
                workers=workers,
                processes=processes,
                initial_config="uniform",
                final_config="uniform",
                fake_stateful=True,
                machine_local=False,
                domain=domain,
                backend="vec")
            experiment.base_machine_id = group*groups + 1
            experiment.run_commands(run, build)

    all_rates = [x * 100000 for x in [16]]
    rates = all_rates[group * len(all_rates) // groups:(group + 1) * len(all_rates) // groups]
    for rate in rates:
        for domain in [1000000 * x for x in [1, 4, 16, 64]]:
            for bin_shift in range(int(math.log2(workers * processes)), 15, 2):
                for migration in ["sudden", "fluid", "batched"]:
                    experiment = Experiment(
                        "wc-migrating-mp",
                        binary="word_count",
                        duration=duration,
                        rate=rate,
                        migration=migration,
                        bin_shift=bin_shift,
                        workers=workers,
                        processes=processes,
                        initial_config="uniform",
                        final_config="uniform_skew",
                        fake_stateful=False,
                        machine_local=False,
                        domain=domain,
                        backend="vec")
                    experiment.base_machine_id = group*groups + 1
                    experiment.run_commands(run, build)

def sigmod_micro_no_migr(group, groups=1):
    workers = 4
    processes = 4
    duration = 30
    migration = "sudden"

    # VEC
    rate = 4 * 1000000
    for domain in [1000000 * x for x in [256, 8192]]:
        for bin_shift in range(int(math.log2(workers * processes)), 21, 2):
            experiment = Experiment(
                "sigmod_migro_no_migr_vec",
                binary="word_count",
                duration=duration,
                rate=rate,
                migration=migration,
                bin_shift=bin_shift,
                workers=workers,
                processes=processes,
                initial_config="uniform",
                final_config="uniform",
                fake_stateful=False,
                machine_local=False,
                domain=domain,
                backend="vec")
            experiment.base_machine_id = group*groups + 1
            experiment.run_commands(run, build)

        experiment = Experiment(
            "sigmod_migro_no_migr_vec_fake",
            binary="word_count",
            duration=duration,
            rate=rate,
            migration=migration,
            bin_shift=int(math.log2(workers * processes),
            workers=workers,
            processes=processes,
            initial_config="uniform",
            final_config="uniform",
            fake_stateful=True,
            machine_local=False,
            domain=domain,
            backend="vec")
        experiment.base_machine_id = group*groups + 1
        experiment.run_commands(run, build)

        experiment = Experiment(
            "sigmod_migro_no_migr_vec_native",
            binary="word_count",
            duration=duration,
            rate=rate,
            migration=migration,
            bin_shift=int(math.log2(workers * processes),
            workers=workers,
            processes=processes,
            initial_config="uniform",
            final_config="uniform",
            fake_stateful=True,
            machine_local=False,
            domain=domain,
            backend="vecnative")
        experiment.base_machine_id = group*groups + 1
        experiment.run_commands(run, build)

    # HASHMAP
    rate = 2 * 1000000
    for domain in [1000000 * x for x in [64]]:
        for bin_shift in range(int(math.log2(workers * processes)), 21, 2):
            experiment = Experiment(
                "sigmod_migro_no_migr_hashmap",
                binary="word_count",
                duration=duration,
                rate=rate,
                migration=migration,
                bin_shift=bin_shift,
                workers=workers,
                processes=processes,
                initial_config="uniform",
                final_config="uniform",
                fake_stateful=False,
                machine_local=False,
                domain=domain,
                backend="hashmap")
            experiment.base_machine_id = group*groups + 1
            experiment.run_commands(run, build)

        experiment = Experiment(
            "sigmod_migro_no_migr_hashmap_fake",
            binary="word_count",
            duration=duration,
            rate=rate,
            migration=migration,
            bin_shift=int(math.log2(workers * processes),
            workers=workers,
            processes=processes,
            initial_config="uniform",
            final_config="uniform",
            fake_stateful=True,
            machine_local=False,
            domain=domain,
            backend="hashmap")
        experiment.base_machine_id = group*groups + 1
        experiment.run_commands(run, build)


        experiment = Experiment(
            "sigmod_migro_no_migr_hashmap_native",
            binary="word_count",
            duration=duration,
            rate=rate,
            migration=migration,
            bin_shift=int(math.log2(workers * processes),
            workers=workers,
            processes=processes,
            initial_config="uniform",
            final_config="uniform",
            fake_stateful=True,
            machine_local=False,
            domain=domain,
            backend="hashmapnative")
        experiment.base_machine_id = group*groups + 1
        experiment.run_commands(run, build)

def sigmod_micro_migr(group, groups=1):
    workers = 4
    processes = 4
    duration = 120
    bin_shift = 12

    # VEC
    rate = 4 * 1000000
    for domain in [1000000 * x for x in [256, 512, 1024, 2048, 4096, 8192]]:
        for migration in ["sudden", "fluid", "batched"]:
            experiment = Experiment(
                "sigmod_migro_migr_vec",
                binary="word_count",
                duration=duration,
                rate=rate,
                migration=migration,
                bin_shift=bin_shift,
                workers=workers,
                processes=processes,
                initial_config="uniform",
                final_config="uniform_skew",
                fake_stateful=False,
                machine_local=False,
                domain=domain,
                backend="vec")
            experiment.base_machine_id = group*groups + 1
            experiment.run_commands(run, build)

    # HASHMAP
    rate = 2 * 1000000
    for domain in [1000000 * x for x in [8, 16, 32, 64, 128]]:
        for migration in ["sudden", "fluid", "batched"]:
            experiment = Experiment(
                "sigmod_migro_migr_hashmap",
                binary="word_count",
                duration=duration,
                rate=rate,
                migration=migration,
                bin_shift=bin_shift,
                workers=workers,
                processes=processes,
                initial_config="uniform",
                final_config="uniform_skew",
                fake_stateful=False,
                machine_local=False,
                domain=domain,
                backend="hashmap")
            experiment.base_machine_id = group*groups + 1
            experiment.run_commands(run, build)

    # VEC
    rate = 4 * 1000000
    domain = 1000000 * 4096
    for bin_shift in range(int(math.log2(workers * processes)), 15, 2):
        for migration in ["sudden", "fluid", "batched"]:
            experiment = Experiment(
                "sigmod_migro_migr_vec",
                binary="word_count",
                duration=duration,
                rate=rate,
                migration=migration,
                bin_shift=bin_shift,
                workers=workers,
                processes=processes,
                initial_config="uniform",
                final_config="uniform_skew",
                fake_stateful=False,
                machine_local=False,
                domain=domain,
                backend="vec")
            experiment.base_machine_id = group*groups + 1
            experiment.run_commands(run, build)

    # HASHMAP
    rate = 2 * 1000000
    domain = 64 * 1000000
    for bin_shift in range(int(math.log2(workers * processes)), 15, 2):
        for migration in ["sudden", "fluid", "batched"]:
            experiment = Experiment(
                "sigmod_migro_migr_hashmap",
                binary="word_count",
                duration=duration,
                rate=rate,
                migration=migration,
                bin_shift=bin_shift,
                workers=workers,
                processes=processes,
                initial_config="uniform",
                final_config="uniform_skew",
                fake_stateful=False,
                machine_local=False,
                domain=domain,
                backend="hashmap")
            experiment.base_machine_id = group*groups + 1
            experiment.run_commands(run, build)

def sigmod_nx(group, groups=1):
    workers = 4
    processes = 4
    duration = 120
    bin_shift = 12

    queries = ["q0-flex", "q1-flex", "q2-flex", "q3-flex", "q4-flex", "q5-flex", "q6-flex", "q7-flex", "q8-flex"]

    rate = 1000000
    migration = "batched"
    for query in queries:
        experiment = Experiment(
            "sigmod_nx",
            binary="timely",
            duration=duration,
            rate=rate,
            queries=query,
            migration=migration,
            bin_shift=default_bin_shift,
            workers=workers,
            processes=4,
            initial_config="uniform",
            final_config="uniform_skew",
            fake_stateful=False,
            machine_local=False,
            time_dilation=1)
        experiment.base_machine_id = group*groups + 1
        experiment.run_commands(run, build)

    time_dilation = int(12*60*60/(duration * 2)*1.1)
    dilated_rate = int(rate / time_dilation)
    for query in queries:
        experiment = Experiment(
            "sigmod_nx_td",
            binary="timely",
            duration=duration,
            rate=dilated_rate,
            queries=query,
            migration=migration,
            bin_shift=default_bin_shift,
            workers=workers,
            processes=4,
            initial_config="uniform",
            final_config="uniform_skew",
            fake_stateful=False,
            machine_local=False,
            time_dilation=time_dilation)
        experiment.base_machine_id = group*groups + 1
        experiment.run_commands(run, build)
